"""验证 Context 策展 Patrol 的耐久观察、Attempt 重试、持续发布与启动对账。"""

import asyncio
import os
from pathlib import Path
from typing import Any
import uuid

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from sqlalchemy import delete, select


os.environ.setdefault("OPENAI_API_KEY", "context-curator-test")
pytestmark = pytest.mark.usefixtures("isolated_postgres_database")

from backend.app.desktop.context_curator import (  # noqa: E402
    CurationEngineError,
    CurationEngineResult,
    CuratedContextPlan,
)
from backend.app.desktop.models import (  # noqa: E402
    DesktopRun,
    DesktopWorkspace,
    PatrolContextAttempt,
    PatrolContextBinding,
    PatrolContextRevision,
)
from backend.app.gateway.app import app  # noqa: E402


SESSION = {"X-Focus-Session": "focus-dev-session"}


async def _successful_curate(_self, _model_name: str | None, payload: dict[str, Any]):
    source_ids = [
        item["source_message_id"]
        for item in payload["source_snapshot"]["messages"]
    ]
    plan = CuratedContextPlan.model_validate({
        "outcome": "replace",
        "items": ([{
            "type": "compose_message",
            "role": "human",
            "content": "已策展的核心目标",
            "source_message_ids": source_ids,
        }] if source_ids else []),
    })
    return CurationEngineResult(plan=plan, raw_response={"content": "test"})


async def _seed_root(service, thread_id: str, text: str) -> str:
    graph = await service.contexts._make_state_graph()
    updated = await graph.aupdate_state(
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
        {"messages": [HumanMessage(content=text)]},
        as_node="model",
    )
    state = await graph.aget_state(updated)
    return state.config["configurable"]["checkpoint_id"]


async def _binding_snapshot(service, agent_id: str) -> dict[str, Any]:
    async with service.session_factory() as session:
        binding = await session.scalar(
            select(PatrolContextBinding).where(PatrolContextBinding.agent_id == agent_id)
        )
        revisions = list((await session.scalars(
            select(PatrolContextRevision)
            .where(PatrolContextRevision.binding_id == binding.binding_id)
            .order_by(PatrolContextRevision.created_at)
        )).all())
        attempts: dict[str, list[PatrolContextAttempt]] = {}
        for revision in revisions:
            attempts[revision.revision_id] = list((await session.scalars(
                select(PatrolContextAttempt)
                .where(PatrolContextAttempt.revision_id == revision.revision_id)
                .order_by(PatrolContextAttempt.attempt_number)
            )).all())
        return {
            "control": binding.control_state,
            "health": binding.health_state,
            "managed_context_id": binding.managed_context_id,
            "observed": binding.observed_checkpoint_id,
            "desired": binding.desired_checkpoint_id,
            "prepared": binding.prepared_checkpoint_id,
            "published": binding.published_checkpoint_id,
            "revisions": [{
                "id": revision.revision_id,
                "source": revision.source_checkpoint_id,
                "status": revision.status,
                "payload": revision.source_payload,
                "attempts": [{
                    "number": attempt.attempt_number,
                    "status": attempt.status,
                    "kind": attempt.error_kind,
                } for attempt in attempts[revision.revision_id]],
            } for revision in revisions],
        }


async def _ensure_managed_context_runnable(service, context_id: str) -> str | None:
    async with service.session_factory() as session:
        checkpoint_id = await service.contexts.ensure_runnable(session, context_id)
        await session.commit()
        return checkpoint_id


async def _record_main_run(service, context_id: str, status: str) -> str:
    run_id = uuid.uuid4().hex
    async with service.session_factory() as session:
        session.add(DesktopRun(
            run_id=run_id,
            task_id=context_id,
            agent_id=f"main:{context_id}",
            kind="main",
            status=status,
            input_messages=[],
        ))
        await session.commit()
    return run_id


async def _set_run_status(service, run_id: str, status: str) -> None:
    async with service.session_factory() as session:
        run = await session.get(DesktopRun, run_id)
        run.status = status
        await session.commit()


async def _cleanup(service, root_context_id: str, workspace_id: str) -> None:
    await service.contexts.delete(root_context_id, cascade=True)
    async with service.session_factory() as session:
        await session.execute(
            delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace_id)
        )
        await session.commit()


def _create_root_and_curator(client: TestClient, tmp_path: Path, suffix: str):
    workspace_path = tmp_path / f"workspace-{suffix}"
    workspace_path.mkdir()
    workspace = client.post(
        "/desktop/api/workspaces", headers=SESSION, json={"path": str(workspace_path)}
    ).json()
    task = client.post(
        f"/desktop/api/workspaces/{workspace['workspace_id']}/threads",
        headers=SESSION,
        json={"thread_id": f"curator-{suffix}-{uuid.uuid4().hex}", "title": f"root-{suffix}"},
    ).json()
    service = app.state.desktop_service
    client.portal.call(_seed_root, service, task["thread_id"], "C0 核心目标")
    draft = client.post(
        f"/desktop/api/tasks/{task['task_id']}/drafts/open", headers=SESSION
    ).json()
    updated = client.put(
        f"/desktop/api/drafts/{draft['draft_id']}",
        headers=SESSION,
        json={
            "mode": "context_curator",
            "curation_policy": {"instructions": "保留目标与权威结论"},
            "equipment": {
                "model_name": service.app_config.resolve_default_model_name(),
                "permissions": ["read"],
                "skills": [],
            },
        },
    )
    assert updated.status_code == 200, updated.text
    run_response = client.post(
        f"/desktop/api/drafts/{draft['draft_id']}/deploy",
        headers=SESSION,
        json={"deployment_id": f"deploy-{uuid.uuid4().hex}"},
    )
    assert run_response.status_code == 200, run_response.text
    return workspace, task, service, run_response.json()


def test_direct_curator_deploys_publishes_audits_and_keeps_following(
    tmp_path, monkeypatch, wait_until
):
    import backend.app.desktop.context_curator.engine as engine_module

    monkeypatch.setattr(engine_module.CurationEngine, "curate", _successful_curate)
    with TestClient(app, client=("127.0.0.1", 51000)) as client:
        workspace, task, service, run = _create_root_and_curator(client, tmp_path, "direct")
        agent_id = run["agent_id"]
        assert service.run_manager.get(run["run_id"]) is not None
        wait_until(
            lambda: client.portal.call(_binding_snapshot, service, agent_id)["published"] is not None,
            timeout=15,
            interval=0.05,
        )
        state = client.portal.call(_binding_snapshot, service, agent_id)
        assert state["control"] == "following"
        assert state["health"] == "idle"
        assert state["observed"] == state["prepared"] == state["published"]
        assert state["revisions"][-1]["attempts"] == [{
            "number": 1, "status": "success", "kind": None
        }]
        detail = client.get(
            f"/desktop/api/agents/{agent_id}/context-curation", headers=SESSION
        ).json()
        assert detail["control_state"] == "following"
        assert detail["health_state"] == "idle"
        assert detail["latest_revision"]["attempts"][0]["output_method"] == "prompt_json"
        assert detail["latest_revision"]["source_payload"]["type"] == "focus.context_curator.input"
        assert detail["latest_revision"]["source_payload"]["source_snapshot"]["messages"]
        assert "attempt_checkpoint_ns" not in detail["latest_revision"]

        managed_id = state["managed_context_id"]
        snapshot = client.get(
            f"/desktop/api/contexts/{managed_id}/snapshot", headers=SESSION
        ).json()
        assert snapshot["messages"][0]["content"] == "已策展的核心目标"
        initial_checkpoint_id = snapshot["checkpoint_id"]
        assert client.portal.call(
            _ensure_managed_context_runnable, service, managed_id
        ) == initial_checkpoint_id
        assert client.portal.call(_binding_snapshot, service, agent_id)["control"] == "following"
        next_source = client.portal.call(_seed_root, service, task["thread_id"], "持续策展消息")
        client.portal.call(service.context_patrol.notify_stable_context_checkpoint, task["task_id"])
        wait_until(
            lambda: client.portal.call(_binding_snapshot, service, agent_id)["published"]
            == next_source,
            timeout=15,
            interval=0.05,
        )
        initial = client.get(
            f"/desktop/api/contexts/{managed_id}/snapshot",
            headers=SESSION,
            params={"checkpoint_id": initial_checkpoint_id},
        ).json()
        assert [
            (message["id"], message["role"], message["content"])
            for message in initial["messages"]
        ] == [
            (message["id"], message["role"], message["content"])
            for message in snapshot["messages"]
        ]
        assert client.portal.call(_binding_snapshot, service, agent_id)["control"] == "following"
        client.portal.call(_cleanup, service, task["task_id"], workspace["workspace_id"])


def test_running_managed_context_does_not_stop_following_patrol(
    tmp_path, monkeypatch, wait_until
):
    import backend.app.desktop.context_curator.engine as engine_module

    monkeypatch.setattr(engine_module.CurationEngine, "curate", _successful_curate)
    with TestClient(app, client=("127.0.0.1", 51011)) as client:
        workspace, task, service, run = _create_root_and_curator(
            client, tmp_path, "keep-following"
        )
        agent_id = run["agent_id"]
        wait_until(
            lambda: client.portal.call(_binding_snapshot, service, agent_id)["published"]
            is not None,
            timeout=15,
            interval=0.05,
        )
        state = client.portal.call(_binding_snapshot, service, agent_id)

        assert client.portal.call(
            _ensure_managed_context_runnable,
            service,
            state["managed_context_id"],
        ) is not None
        assert client.portal.call(_binding_snapshot, service, agent_id)["control"] == "following"

        managed_task = client.get(
            f"/desktop/api/tasks/{state['managed_context_id']}", headers=SESSION
        ).json()
        managed_run_id = client.portal.call(
            _record_main_run, service, state["managed_context_id"], "running"
        )
        client.portal.call(
            _seed_root,
            service,
            managed_task["thread_id"],
            "派生会话续写",
        )
        next_source = client.portal.call(_seed_root, service, task["thread_id"], "C1 新事实")
        client.portal.call(service.context_patrol.notify_stable_context_checkpoint, task["task_id"])
        observed = client.portal.call(_binding_snapshot, service, agent_id)
        assert observed["desired"] == next_source
        assert observed["published"] != next_source

        client.portal.call(_set_run_status, service, managed_run_id, "success")
        client.portal.call(
            service.context_patrol.notify_stable_context_checkpoint,
            state["managed_context_id"],
        )
        wait_until(
            lambda: client.portal.call(_binding_snapshot, service, agent_id)["published"]
            == next_source,
            timeout=15,
            interval=0.05,
        )
        updated = client.get(
            f"/desktop/api/contexts/{state['managed_context_id']}/snapshot", headers=SESSION
        ).json()
        assert [message["content"] for message in updated["messages"]] == [
            "已策展的核心目标",
            "派生会话续写",
        ]
        assert client.portal.call(_binding_snapshot, service, agent_id)["control"] == "following"

        client.portal.call(_cleanup, service, task["task_id"], workspace["workspace_id"])


def test_quick_curator_uses_domain_defaults_and_is_idempotent(
    tmp_path, monkeypatch, wait_until
):
    import backend.app.desktop.context_curator.engine as engine_module

    monkeypatch.setattr(engine_module.CurationEngine, "curate", _successful_curate)
    with TestClient(app, client=("127.0.0.1", 51010)) as client:
        workspace_path = tmp_path / "workspace-quick"
        workspace_path.mkdir()
        workspace = client.post(
            "/desktop/api/workspaces", headers=SESSION, json={"path": str(workspace_path)}
        ).json()
        task = client.post(
            f"/desktop/api/workspaces/{workspace['workspace_id']}/threads",
            headers=SESSION,
            json={"thread_id": f"curator-quick-{uuid.uuid4().hex}", "title": "root-quick"},
        ).json()
        service = app.state.desktop_service
        client.portal.call(_seed_root, service, task["thread_id"], "C0 快捷策展目标")
        deployment_id = f"quick-{uuid.uuid4().hex}"
        endpoint = f"/desktop/api/tasks/{task['task_id']}/context-curation/quick-deploy"
        first = client.post(
            endpoint, headers=SESSION, json={"deployment_id": deployment_id}
        )
        assert first.status_code == 200, first.text
        second = client.post(
            endpoint, headers=SESSION, json={"deployment_id": deployment_id}
        )
        assert second.status_code == 200, second.text
        assert second.json()["run_id"] == first.json()["run_id"]
        assert first.json()["model_name"] == service._default_curation_model_name()

        agent_id = first.json()["agent_id"]
        wait_until(
            lambda: client.portal.call(_binding_snapshot, service, agent_id)["published"] is not None,
            timeout=15,
            interval=0.05,
        )
        agents = client.get(
            f"/desktop/api/tasks/{task['task_id']}/agents", headers=SESSION
        ).json()
        assert len(agents) == 1
        assert agents[0]["mode"] == "context_curator"
        assert "当前目标" in agents[0]["curation_policy"]["instructions"]
        assert any(
            "失败工具调用" in rule
            for rule in agents[0]["curation_policy"]["discard_rules"]
        )
        client.portal.call(_cleanup, service, task["task_id"], workspace["workspace_id"])


def test_observation_commits_before_nul_safe_projection(
    tmp_path, monkeypatch, wait_until
):
    import backend.app.desktop.context_curator.engine as engine_module

    monkeypatch.setattr(engine_module.CurationEngine, "curate", _successful_curate)
    with TestClient(app, client=("127.0.0.1", 51001)) as client:
        workspace, task, service, run = _create_root_and_curator(client, tmp_path, "nul")
        agent_id = run["agent_id"]
        wait_until(
            lambda: client.portal.call(_binding_snapshot, service, agent_id)["published"] is not None,
            timeout=15,
        )
        checkpoint_id = client.portal.call(_seed_root, service, task["thread_id"], "C1")
        original_snapshot = service.contexts.snapshot

        async def snapshot_with_nul(context_id, requested_checkpoint=None):
            if context_id == task["task_id"] and requested_checkpoint == checkpoint_id:
                return {"messages": [{
                    "id": "nul-source", "role": "human", "content": "事实\x00仍有效",
                    "reasoning_content": "不可保存的内部字段\x00",
                }]}
            return await original_snapshot(context_id, requested_checkpoint)

        monkeypatch.setattr(service.contexts, "snapshot", snapshot_with_nul)
        client.portal.call(service.context_patrol.notify_stable_context_checkpoint, task["task_id"])
        observed = client.portal.call(_binding_snapshot, service, agent_id)
        assert observed["observed"] == observed["desired"] == checkpoint_id
        wait_until(
            lambda: client.portal.call(_binding_snapshot, service, agent_id)["published"] == checkpoint_id,
            timeout=15,
            interval=0.05,
        )
        finished = client.portal.call(_binding_snapshot, service, agent_id)
        payload = next(item for item in finished["revisions"] if item["source"] == checkpoint_id)["payload"]
        assert payload["source_snapshot"]["messages"][0]["content"] == "事实�仍有效"
        assert "reasoning_content" not in payload["source_snapshot"]["messages"][0]
        client.portal.call(_cleanup, service, task["task_id"], workspace["workspace_id"])


def test_projection_failure_does_not_roll_back_observed_target(
    tmp_path, monkeypatch, wait_until
):
    import backend.app.desktop.context_curator.engine as engine_module

    monkeypatch.setattr(engine_module.CurationEngine, "curate", _successful_curate)
    with TestClient(app, client=("127.0.0.1", 51005)) as client:
        workspace, task, service, run = _create_root_and_curator(client, tmp_path, "projection")
        agent_id = run["agent_id"]
        wait_until(
            lambda: client.portal.call(_binding_snapshot, service, agent_id)["published"] is not None,
            timeout=15,
        )
        previous = client.portal.call(_binding_snapshot, service, agent_id)["published"]
        checkpoint_id = client.portal.call(_seed_root, service, task["thread_id"], "C1")

        def fail_projection(_checkpoint_id, _messages):
            raise ValueError("projection failed after observation")

        monkeypatch.setattr(service.context_patrol.projector, "project", fail_projection)
        client.portal.call(service.context_patrol.notify_stable_context_checkpoint, task["task_id"])
        wait_until(
            lambda: client.portal.call(_binding_snapshot, service, agent_id)["health"] == "degraded",
            timeout=15,
            interval=0.05,
        )
        failed = client.portal.call(_binding_snapshot, service, agent_id)
        assert failed["observed"] == failed["desired"] == checkpoint_id
        assert failed["published"] == previous
        revision = next(item for item in failed["revisions"] if item["source"] == checkpoint_id)
        assert revision["status"] == "error"
        assert revision["payload"] == {}
        assert revision["attempts"] == [{
            "number": 1, "status": "error", "kind": "projection"
        }]
        client.portal.call(_cleanup, service, task["task_id"], workspace["workspace_id"])


def test_paused_observation_coalesces_to_latest_checkpoint_on_resume(
    tmp_path, monkeypatch, wait_until
):
    import backend.app.desktop.context_curator.engine as engine_module

    monkeypatch.setattr(engine_module.CurationEngine, "curate", _successful_curate)
    with TestClient(app, client=("127.0.0.1", 51006)) as client:
        workspace, task, service, run = _create_root_and_curator(client, tmp_path, "coalesce")
        agent_id = run["agent_id"]
        wait_until(
            lambda: client.portal.call(_binding_snapshot, service, agent_id)["published"] is not None,
            timeout=15,
        )
        response = client.put(
            f"/desktop/api/agents/{agent_id}/context-curation/state",
            headers=SESSION,
            json={"state": "paused"},
        )
        assert response.status_code == 200, response.text
        c1 = client.portal.call(_seed_root, service, task["thread_id"], "C1")
        client.portal.call(service.context_patrol.notify_stable_context_checkpoint, task["task_id"])
        c2 = client.portal.call(_seed_root, service, task["thread_id"], "C2 authoritative")
        client.portal.call(service.context_patrol.notify_stable_context_checkpoint, task["task_id"])
        paused = client.portal.call(_binding_snapshot, service, agent_id)
        assert paused["control"] == "paused"
        assert paused["observed"] == paused["desired"] == c2
        assert next(item for item in paused["revisions"] if item["source"] == c1)["attempts"] == []
        assert next(item for item in paused["revisions"] if item["source"] == c2)["attempts"] == []

        response = client.put(
            f"/desktop/api/agents/{agent_id}/context-curation/state",
            headers=SESSION,
            json={"state": "following"},
        )
        assert response.status_code == 200, response.text
        wait_until(
            lambda: client.portal.call(_binding_snapshot, service, agent_id)["published"] == c2,
            timeout=15,
            interval=0.05,
        )
        resumed = client.portal.call(_binding_snapshot, service, agent_id)
        assert next(item for item in resumed["revisions"] if item["source"] == c1)["status"] == "superseded"
        assert next(item for item in resumed["revisions"] if item["source"] == c2)["attempts"] == [{
            "number": 1, "status": "success", "kind": None
        }]
        client.portal.call(_cleanup, service, task["task_id"], workspace["workspace_id"])


def test_capability_failure_can_retry_same_revision_after_configuration_fix(
    tmp_path, monkeypatch, wait_until
):
    import backend.app.desktop.context_curator.engine as engine_module

    calls = 0

    async def fail_then_succeed(self, model_name, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise CurationEngineError("capability", "thinking mode 不支持 forced tool choice")
        return await _successful_curate(self, model_name, payload)

    monkeypatch.setattr(engine_module.CurationEngine, "curate", fail_then_succeed)
    with TestClient(app, client=("127.0.0.1", 51002)) as client:
        workspace, task, service, run = _create_root_and_curator(client, tmp_path, "retry")
        agent_id = run["agent_id"]
        wait_until(
            lambda: client.portal.call(_binding_snapshot, service, agent_id)["health"] == "blocked",
            timeout=15,
            interval=0.05,
        )
        failed = client.portal.call(_binding_snapshot, service, agent_id)
        assert failed["control"] == "following"
        assert failed["published"] is None
        response = client.put(
            f"/desktop/api/agents/{agent_id}/context-curation/state",
            headers=SESSION,
            json={"state": "following"},
        )
        assert response.status_code == 200, response.text
        wait_until(
            lambda: client.portal.call(_binding_snapshot, service, agent_id)["published"]
            == failed["desired"],
            timeout=15,
            interval=0.05,
        )
        recovered = client.portal.call(_binding_snapshot, service, agent_id)
        assert len(recovered["revisions"][0]["attempts"]) == 2
        assert [item["status"] for item in recovered["revisions"][0]["attempts"]] == [
            "error", "success"
        ]
        client.portal.call(_cleanup, service, task["task_id"], workspace["workspace_id"])


def test_transient_provider_failure_uses_bounded_backoff_without_duplicate_revision(
    tmp_path, monkeypatch, wait_until
):
    import backend.app.desktop.context_curator.engine as engine_module

    calls = 0

    async def transient(self, model_name, payload):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise CurationEngineError("provider", "temporary upstream failure")
        return await _successful_curate(self, model_name, payload)

    monkeypatch.setattr(engine_module.CurationEngine, "curate", transient)
    with TestClient(app, client=("127.0.0.1", 51003)) as client:
        workspace, task, service, run = _create_root_and_curator(client, tmp_path, "transient")
        agent_id = run["agent_id"]
        wait_until(
            lambda: client.portal.call(_binding_snapshot, service, agent_id)["published"] is not None,
            timeout=15,
            interval=0.05,
        )
        state = client.portal.call(_binding_snapshot, service, agent_id)
        assert len(state["revisions"]) == 1
        assert [item["status"] for item in state["revisions"][0]["attempts"]] == [
            "error", "error", "success"
        ]
        client.portal.call(_cleanup, service, task["task_id"], workspace["workspace_id"])


def test_compiler_rejection_retains_raw_and_parsed_attempt_evidence(
    tmp_path, monkeypatch, wait_until
):
    import backend.app.desktop.context_curator.engine as engine_module

    async def duplicate_evidence(_self, _model_name, payload):
        source_id = payload["source_snapshot"]["messages"][0]["source_message_id"]
        plan = CuratedContextPlan.model_validate({"outcome": "replace", "items": [
            {
                "type": "compose_message",
                "role": "ai",
                "content": "重复证据",
                "source_message_ids": [source_id, source_id],
            },
        ]})
        return CurationEngineResult(plan=plan, raw_response={"content": "raw-invalid-plan"})

    monkeypatch.setattr(engine_module.CurationEngine, "curate", duplicate_evidence)
    with TestClient(app, client=("127.0.0.1", 51007)) as client:
        workspace, task, service, run = _create_root_and_curator(client, tmp_path, "evidence")
        wait_until(
            lambda: client.portal.call(_binding_snapshot, service, run["agent_id"])["health"]
            == "degraded",
            timeout=15,
            interval=0.05,
        )
        detail = client.get(
            f"/desktop/api/agents/{run['agent_id']}/context-curation", headers=SESSION
        ).json()
        attempt = detail["latest_revision"]["attempts"][0]
        assert attempt["error_kind"] == "content"
        assert attempt["raw_response"] == {"content": "raw-invalid-plan"}
        assert len(attempt["parsed_response"]["items"]) == 1
        client.portal.call(_cleanup, service, task["task_id"], workspace["workspace_id"])


def test_no_change_advances_source_without_rewriting_managed_context(
    tmp_path, monkeypatch, wait_until
):
    import backend.app.desktop.context_curator.engine as engine_module

    calls = 0

    async def replace_then_unchanged(self, model_name, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return await _successful_curate(self, model_name, payload)
        return CurationEngineResult(
            plan=CuratedContextPlan.model_validate({
                "outcome": "no_change",
                "items": [],
            }),
            raw_response={"content": "unchanged"},
        )

    monkeypatch.setattr(
        engine_module.CurationEngine, "curate", replace_then_unchanged
    )
    with TestClient(app, client=("127.0.0.1", 51008)) as client:
        workspace, task, service, run = _create_root_and_curator(
            client, tmp_path, "unchanged"
        )
        agent_id = run["agent_id"]
        wait_until(
            lambda: client.portal.call(_binding_snapshot, service, agent_id)["published"]
            is not None,
            timeout=15,
        )
        state = client.portal.call(_binding_snapshot, service, agent_id)
        managed_id = state["managed_context_id"]
        before = client.get(
            f"/desktop/api/contexts/{managed_id}/snapshot", headers=SESSION
        ).json()
        checkpoint_id = client.portal.call(
            _seed_root, service, task["thread_id"], "重复且不影响受管 Context 的消息"
        )
        client.portal.call(
            service.context_patrol.notify_stable_context_checkpoint, task["task_id"]
        )
        wait_until(
            lambda: client.portal.call(_binding_snapshot, service, agent_id)["published"]
            == checkpoint_id,
            timeout=15,
            interval=0.05,
        )
        after = client.get(
            f"/desktop/api/contexts/{managed_id}/snapshot", headers=SESSION
        ).json()
        finished = client.portal.call(_binding_snapshot, service, agent_id)
        assert after["checkpoint_id"] == before["checkpoint_id"]
        assert after["messages"] == before["messages"]
        assert finished["prepared"] == finished["published"] == checkpoint_id
        assert finished["revisions"][-1]["status"] == "unchanged"
        assert finished["revisions"][-1]["attempts"] == [{
            "number": 1, "status": "success", "kind": None
        }]
        client.portal.call(_cleanup, service, task["task_id"], workspace["workspace_id"])


def test_restart_reconciles_actual_root_head_not_stale_desired(
    tmp_path, monkeypatch, wait_until
):
    import backend.app.desktop.context_curator.engine as engine_module

    monkeypatch.setattr(engine_module.CurationEngine, "curate", _successful_curate)
    with TestClient(app, client=("127.0.0.1", 51004)) as client:
        workspace, task, service, run = _create_root_and_curator(client, tmp_path, "reconcile")
        agent_id = run["agent_id"]
        wait_until(
            lambda: client.portal.call(_binding_snapshot, service, agent_id)["published"] is not None,
            timeout=15,
        )
        client.portal.call(service.context_patrol.close)
        checkpoint_id = client.portal.call(_seed_root, service, task["thread_id"], "C1 未发通知")
        stale = client.portal.call(_binding_snapshot, service, agent_id)
        assert stale["desired"] != checkpoint_id
        client.portal.call(service.context_patrol.start)
        wait_until(
            lambda: client.portal.call(_binding_snapshot, service, agent_id)["published"] == checkpoint_id,
            timeout=15,
            interval=0.05,
        )
        recovered = client.portal.call(_binding_snapshot, service, agent_id)
        assert recovered["observed"] == recovered["desired"] == checkpoint_id
        assert len([item for item in recovered["revisions"] if item["source"] == checkpoint_id]) == 1
        client.portal.call(_cleanup, service, task["task_id"], workspace["workspace_id"])
