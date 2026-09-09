"""验证 Context 派生树、审批边界和独立 LangGraph checkpoint。"""

import asyncio
import os
from pathlib import Path
import uuid

import pytest
from fastapi.testclient import TestClient
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage
from sqlalchemy import delete, func, select


os.environ.setdefault("OPENAI_API_KEY", "context-test")
pytestmark = pytest.mark.usefixtures("isolated_postgres_database")

from backend.app.desktop.models import (  # noqa: E402
    DesktopContextDefinition,
    DesktopContextSource,
    DesktopMaterial,
    DesktopRun,
    DesktopThread,
    DesktopWorkspace,
    PatrolDraft,
)
from backend.app.gateway.app import app  # noqa: E402


SESSION = {"X-Focus-Session": "focus-dev-session"}


def _client():
    return TestClient(app, client=("127.0.0.1", 50000))


async def _seed(service, thread_id: str, content: str) -> str:
    graph = create_agent(model=FakeListChatModel(responses=["unused"]), tools=[])
    graph.checkpointer = service.checkpointer
    config = await graph.aupdate_state(
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
        {"messages": [HumanMessage(content=content)]},
    )
    return config["configurable"]["checkpoint_id"]


async def _append(service, thread_id: str, content: str) -> None:
    graph = create_agent(model=FakeListChatModel(responses=["unused"]), tools=[])
    graph.checkpointer = service.checkpointer
    await graph.aupdate_state(
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
        {"messages": [HumanMessage(content=content)]},
        as_node="model",
    )


async def _run_count(service, context_id: str) -> int:
    async with service.session_factory() as session:
        return await session.scalar(
            select(func.count()).select_from(DesktopRun).where(DesktopRun.task_id == context_id)
        )


async def _insert_main_run(
    service, context_id: str, input_tokens: int, cache_hit_tokens: int
) -> None:
    run_id = uuid.uuid4().hex
    async with service.session_factory() as session:
        session.add(
            DesktopRun(
                run_id=run_id,
                task_id=context_id,
                agent_id=f"main:{context_id}",
                kind="main",
                status="success",
                input_messages=[{"role": "human", "content": "sent"}],
            )
        )
        await session.commit()
    await service._set_run_status(
        run_id,
        "success",
        None,
        input_tokens,
        cache_hit_tokens,
    )


async def _context_extras(service, context_id: str) -> tuple[dict, int, int]:
    async with service.session_factory() as session:
        task = await session.get(DesktopThread, context_id)
        materials = await session.scalar(
            select(func.count()).select_from(DesktopMaterial).where(DesktopMaterial.task_id == context_id)
        )
        drafts = await session.scalar(
            select(func.count()).select_from(PatrolDraft).where(PatrolDraft.task_id == context_id)
        )
        return task.ui_state, materials, drafts


async def _cleanup(service, context_ids: list[str], workspace_ids: list[str], thread_ids: list[str]) -> None:
    for thread_id in thread_ids:
        await service.checkpointer.adelete_thread(thread_id)
    async with service.session_factory() as session:
        await session.execute(
            delete(DesktopContextSource).where(
                DesktopContextSource.context_id.in_(context_ids)
                | DesktopContextSource.parent_context_id.in_(context_ids)
            )
        )
        await session.execute(delete(DesktopThread).where(DesktopThread.task_id.in_(context_ids)))
        await session.execute(delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id.in_(workspace_ids)))
        await session.commit()


def test_recursive_tree_merge_and_parent_independence(tmp_path: Path, monkeypatch):
    import backend.app.desktop.context_service as context_module

    async def fake_make_lead_agent(**_kwargs):
        return create_agent(model=FakeListChatModel(responses=["unused"]), tools=[])

    monkeypatch.setattr(context_module, "make_lead_agent", fake_make_lead_agent)
    context_ids: list[str] = []
    workspace_ids: list[str] = []
    thread_ids: list[str] = []
    with _client() as client:
        service = app.state.desktop_service
        try:
            folder = tmp_path / "workspace"
            folder.mkdir()
            workspace = client.post(
                "/desktop/api/workspaces", headers=SESSION, json={"path": str(folder)}
            ).json()
            workspace_ids.append(workspace["workspace_id"])
            root = client.post(
                f"/desktop/api/workspaces/{workspace['workspace_id']}/threads",
                headers=SESSION,
                json={"thread_id": f"root-{uuid.uuid4().hex}", "title": "A"},
            ).json()
            context_ids.append(root["task_id"])
            thread_ids.append(root["thread_id"])
            root_checkpoint = client.portal.call(_seed, service, root["thread_id"], "A1")

            def derive(title, sources, messages):
                response = client.post(
                    "/desktop/api/contexts/derive",
                    headers=SESSION,
                    json={"title": title, "sources": sources, "messages": messages},
                )
                assert response.status_code == 200, response.text
                payload = response.json()
                context_ids.append(payload["context_id"])
                thread_ids.append(payload["thread_id"])
                return payload

            b = derive(
                "B",
                [{"context_id": root["task_id"], "checkpoint_id": root_checkpoint}],
                [{"role": "human", "content": "B1"}],
            )
            assert b["projection_status"] == "valid"
            assert b["thread_id"] != root["thread_id"]
            assert client.portal.call(_context_extras, service, b["context_id"]) == ({}, 0, 0)
            original_b_snapshot = client.get(
                f"/desktop/api/contexts/{b['context_id']}/snapshot", headers=SESSION
            ).json()
            assert original_b_snapshot["messages"] == [{"role": "human", "content": "B1"}]

            edited_b = client.put(
                f"/desktop/api/contexts/{b['context_id']}/definition",
                headers=SESSION,
                json={"messages": [{"role": "human", "content": "B2"}]},
            )
            assert edited_b.status_code == 200, edited_b.text
            assert edited_b.json()["editable"] is True
            assert edited_b.json()["projection_status"] == "valid", edited_b.json()["issues"]
            b_snapshot = client.get(
                f"/desktop/api/contexts/{b['context_id']}/snapshot", headers=SESSION
            ).json()
            assert b_snapshot["messages"] == [{"role": "human", "content": "B2"}]
            historical_b_snapshot = client.get(
                f"/desktop/api/contexts/{b['context_id']}/snapshot",
                headers=SESSION,
                params={"checkpoint_id": original_b_snapshot["checkpoint_id"]},
            ).json()
            assert [
                {"role": message["role"], "content": message["content"]}
                for message in historical_b_snapshot["messages"]
            ] == [{"role": "human", "content": "B1"}]

            client.portal.call(
                _insert_main_run,
                service,
                b["context_id"],
                200,
                50,
            )
            locked_edit = client.put(
                f"/desktop/api/contexts/{b['context_id']}/definition",
                headers=SESSION,
                json={"messages": [{"role": "human", "content": "B3"}]},
            )
            assert locked_edit.status_code == 409
            assert client.get(
                f"/desktop/api/contexts/{b['context_id']}/snapshot", headers=SESSION
            ).json()["messages"] == [{"role": "human", "content": "B2"}]

            client.portal.call(_append, service, root["thread_id"], "A2")
            assert client.get(
                f"/desktop/api/contexts/{b['context_id']}/snapshot", headers=SESSION
            ).json()["messages"] == [{"role": "human", "content": "B2"}]

            c = derive(
                "C",
                [{"context_id": b["context_id"], "checkpoint_id": b_snapshot["checkpoint_id"]}],
                [{"role": "system", "content": "C1"}],
            )
            c_snapshot = client.get(
                f"/desktop/api/contexts/{c['context_id']}/snapshot", headers=SESSION
            ).json()
            d = derive(
                "D",
                [
                    {"context_id": b["context_id"], "checkpoint_id": b_snapshot["checkpoint_id"]},
                    {"context_id": c["context_id"], "checkpoint_id": c_snapshot["checkpoint_id"]},
                ],
                [{"role": "human", "content": "D1"}],
            )
            assert [source["context_id"] for source in d["sources"]] == [b["context_id"], c["context_id"]]
            tree = client.get(
                f"/desktop/api/workspaces/{workspace['workspace_id']}/contexts/tree", headers=SESSION
            ).json()
            depths = {item["title"]: item["depth"] for item in tree}
            assert depths["A"] == 0
            assert depths["B"] == 1
            assert depths["C"] == 2
            assert depths["D"] == 2
            b_node = next(item for item in tree if item["context_id"] == b["context_id"])
            assert b_node["editable"] is False
            assert b_node["cache_input_tokens"] == 200
            assert b_node["cache_hit_tokens"] == 50
            assert b_node["cache_hit_rate"] == 0.25
            root_node = next(item for item in tree if item["context_id"] == root["task_id"])
            assert root_node["cache_hit_rate"] is None

            other_folder = tmp_path / "other"
            other_folder.mkdir()
            other_workspace = client.post(
                "/desktop/api/workspaces", headers=SESSION, json={"path": str(other_folder)}
            ).json()
            workspace_ids.append(other_workspace["workspace_id"])
            other = client.post(
                f"/desktop/api/workspaces/{other_workspace['workspace_id']}/threads",
                headers=SESSION,
                json={"thread_id": f"other-{uuid.uuid4().hex}", "title": "other"},
            ).json()
            context_ids.append(other["task_id"])
            thread_ids.append(other["thread_id"])
            other_checkpoint = client.portal.call(_seed, service, other["thread_id"], "other")
            response = client.post(
                "/desktop/api/contexts/derive",
                headers=SESSION,
                json={
                    "title": "invalid merge",
                    "sources": [
                        {"context_id": b["context_id"], "checkpoint_id": b_snapshot["checkpoint_id"]},
                        {"context_id": other["task_id"], "checkpoint_id": other_checkpoint},
                    ],
                    "messages": [],
                },
            )
            assert response.status_code == 422
        finally:
            client.portal.call(_cleanup, service, context_ids, workspace_ids, thread_ids)


def test_degradation_requires_hash_bound_decision(tmp_path: Path, monkeypatch):
    import backend.app.desktop.context_service as context_module

    async def fake_make_lead_agent(**_kwargs):
        return create_agent(model=FakeListChatModel(responses=["unused"]), tools=[])

    monkeypatch.setattr(context_module, "make_lead_agent", fake_make_lead_agent)
    context_ids: list[str] = []
    workspace_ids: list[str] = []
    thread_ids: list[str] = []
    with _client() as client:
        service = app.state.desktop_service
        try:
            folder = tmp_path / "workspace"
            folder.mkdir()
            workspace = client.post(
                "/desktop/api/workspaces", headers=SESSION, json={"path": str(folder)}
            ).json()
            workspace_ids.append(workspace["workspace_id"])
            root = client.post(
                f"/desktop/api/workspaces/{workspace['workspace_id']}/threads",
                headers=SESSION,
                json={"thread_id": f"root-{uuid.uuid4().hex}", "title": "root"},
            ).json()
            context_ids.append(root["task_id"])
            thread_ids.append(root["thread_id"])
            checkpoint_id = client.portal.call(_seed, service, root["thread_id"], "root")
            authored_tool = {
                "role": "tool",
                "content": "唯一关注结果",
                "tool_call_id": "kept-call",
                "name": "search",
            }
            repaired = client.post(
                "/desktop/api/contexts/derive",
                headers=SESSION,
                json={
                    "title": "repaired",
                    "sources": [{"context_id": root["task_id"], "checkpoint_id": checkpoint_id}],
                    "messages": [authored_tool],
                },
            ).json()
            context_ids.append(repaired["context_id"])
            thread_ids.append(repaired["thread_id"])
            assert repaired["projection_status"] == "repaired"
            repaired_snapshot = client.get(
                f"/desktop/api/contexts/{repaired['context_id']}/snapshot", headers=SESSION
            ).json()
            assert repaired_snapshot["messages"] == [authored_tool]
            blocked = client.post(
                "/desktop/api/contexts/derive",
                headers=SESSION,
                json={
                    "title": "blocked",
                    "sources": [{"context_id": root["task_id"], "checkpoint_id": checkpoint_id}],
                    "messages": [{"role": "tool", "content": "没有协议字段"}],
                },
            ).json()
            context_ids.append(blocked["context_id"])
            thread_ids.append(blocked["thread_id"])
            assert blocked["projection_status"] == "approval_required"
            before = client.portal.call(_run_count, service, blocked["context_id"])
            run = client.post(
                f"/desktop/api/tasks/{blocked['context_id']}/main/runs",
                headers=SESSION,
                json={"message": "不能发送", "permissions": ["read"]},
            )
            assert run.status_code == 409
            assert client.portal.call(_run_count, service, blocked["context_id"]) == before

            reject = client.post(
                f"/desktop/api/contexts/{blocked['context_id']}/projection/decision",
                headers=SESSION,
                json={
                    "decision": "reject",
                    "definition_hash": blocked["definition_hash"],
                    "projection_hash": blocked["projection_hash"],
                },
            ).json()
            assert reject["projection_status"] == "rejected"

            edited = client.put(
                f"/desktop/api/contexts/{blocked['context_id']}/definition",
                headers=SESSION,
                json={"messages": [{"role": "human", "content": "合法定义"}]},
            ).json()
            assert edited["projection_status"] == "valid"
            stale = client.post(
                f"/desktop/api/contexts/{blocked['context_id']}/projection/decision",
                headers=SESSION,
                json={
                    "decision": "accept",
                    "definition_hash": blocked["definition_hash"],
                    "projection_hash": blocked["projection_hash"],
                },
            )
            assert stale.status_code == 409

            accepted = client.post(
                "/desktop/api/contexts/derive",
                headers=SESSION,
                json={
                    "title": "accepted",
                    "sources": [{"context_id": root["task_id"], "checkpoint_id": checkpoint_id}],
                    "messages": [{"role": "unknown", "content": "保留原文"}],
                },
            ).json()
            context_ids.append(accepted["context_id"])
            thread_ids.append(accepted["thread_id"])
            decision = client.post(
                f"/desktop/api/contexts/{accepted['context_id']}/projection/decision",
                headers=SESSION,
                json={
                    "decision": "accept",
                    "definition_hash": accepted["definition_hash"],
                    "projection_hash": accepted["projection_hash"],
                },
            )
            assert decision.status_code == 200, decision.text
            assert decision.json()["projection_status"] == "approved"
            snapshot = client.get(
                f"/desktop/api/contexts/{accepted['context_id']}/snapshot", headers=SESSION
            ).json()
            assert snapshot["messages"] == [{"role": "unknown", "content": "保留原文"}]
        finally:
            client.portal.call(_cleanup, service, context_ids, workspace_ids, thread_ids)
