"""Opt-in full production pipeline smoke, with isolated DB/workspaces/checkpoints.

FOCUS_SUBMISSION_REAL_MODEL=1 enables paid real-model calls. Three repeats per
case by default; FOCUS_SUBMISSION_BENCHMARK_OUTPUT selects the report directory.
The supervisor is started after intake to keep preparation outside measurement;
its startup/recovery overhead is included in downstream milestone upper bounds.
"""

import asyncio
import json
import os
from pathlib import Path
import uuid

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import select

from backend.app.gateway.app import app
from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopDelegationGrant
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.agent_loop.patrol_inputs import PatrolInputRequest
from backend.app.desktop.agent_loop.submission_latency import (
    CommittedSubmissionProbe, SCENARIOS, SubmissionSample, measure_submission, summarize,
)
from backend.app.desktop.agent_loop.context_expansion.models import LoopContextDerivationArtifact
from backend.app.desktop.agent_loop.workspace_patrol import WorkspacePatrolBootstrap
from backend.app.desktop.context_evolution.schemas import ContextRevisionOriginKind, ContextRevisionSourceContract
from backend.app.desktop.models import DesktopWorkspace

pytestmark = [pytest.mark.usefixtures("isolated_postgres_database"), pytest.mark.skipif(
    os.environ.get("FOCUS_SUBMISSION_REAL_MODEL") != "1", reason="explicit real-provider end-to-end benchmark",
)]


def _history(long_tool=False):
    messages = [{"id": f"history-{i}", "role": "human", "content": (
        f"Requirement R{i}: the offline inventory tool must keep item {i} in a local JSON file. "
        "Implementation and documentation are independent workstreams; no network access is needed."
    )} for i in range(24)]
    if long_tool:
        messages += [
            {"id": "tool-request", "role": "ai", "content": "Inspecting the existing local data.",
             "tool_calls": [{"id": "inventory-read", "name": "read_file", "args": {"path": "inventory.json"}}]},
            {"id": "tool-result", "role": "tool", "tool_call_id": "inventory-read", "name": "read_file",
             "content": "\n".join(f'Item {i}: sku=SKU-{i}, stock={i % 17}, verified=true' for i in range(1500))},
        ]
    return tuple(messages)


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("repeat", range(int(os.environ.get("FOCUS_SUBMISSION_BENCHMARK_REPEATS", "3"))))
def test_real_submission_to_execution(tmp_path, monkeypatch, runtime_postgres_database, scenario, repeat):
    # Use the same product-default normal-budget cap as the published index smoke.
    # Do not modify config files or silently switch the configured default model.
    from focus.config import get_app_config
    config = get_app_config("config.yaml", recover_models=True)
    selected = config.get_model(config.resolve_default_model_name())
    host_cap = selected.curation_max_output_tokens
    monkeypatch.setattr(selected, "curation_max_output_tokens", type(selected).model_fields["curation_max_output_tokens"].default)
    monkeypatch.setenv("FOCUS_LOOP_SUPERVISOR", "0")
    monkeypatch.setenv("FOCUS_LOOP_CANONICAL_EVENTS", "1")
    output = Path(os.environ.get("FOCUS_SUBMISSION_BENCHMARK_OUTPUT", ".tmp/submission-latency"))
    output.mkdir(parents=True, exist_ok=True)
    timeout = float(os.environ.get("FOCUS_SUBMISSION_BENCHMARK_TIMEOUT", "300"))
    metadata = {"scenario": scenario, "repeat": repeat, "model": selected.name,
                "budget_preset": "normal", "curation_max_output_tokens": selected.curation_max_output_tokens,
                "host_curation_max_output_tokens": host_cap, "cap_source": "product default, test only",
                "derivation_max_output_tokens": selected.derivation_max_output_tokens,
                "measurement_boundary": "HTTP submission through committed DB polling; includes supervisor startup",
                "database": "per-sample isolated PostgreSQL, migrated to head",
                "synthetic_history": True, "background_prewarm": True, "observer_version": 3,
                "run_scope": "all_causally_related_runs",
                "timeout_seconds": timeout}
    sample = SubmissionSample(scenario, repeat)

    def save_report():
        (output / f"{scenario}-{repeat}.json").write_text(json.dumps(
            {"metadata": metadata, "sample": sample.document()}, ensure_ascii=False, indent=2), encoding="utf-8")

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        async def run():
            nonlocal sample
            service = app.state.desktop_service
            sessions = service.session_factory
            workspace_id = uuid.uuid4().hex
            workspace_path = tmp_path / "work"
            workspace_path.mkdir()
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(workspace_path), display_name="latency"))
            receipt = await app.state.workspace_patrol.submit(workspace_id, PatrolInputRequest(
                submission_id=uuid.uuid4().hex, content="历史资料准备阶段，请等待下一条用户任务。", budget_preset="normal"))
            loop_id = receipt["loop_id"]
            try:
                await WorkspacePatrolBootstrap(sessions, service.contexts.evolution).prepare(loop_id)
                async with sessions.begin() as session:
                    loop = await session.get(AgentLoop, loop_id)
                    target = await service.contexts.evolution.stage_definition(session, loop.initial_context_id,
                        _history(scenario == "long_tool_output"), (), ContextRevisionOriginKind.DEFINITION_UPDATE, "benchmark-history")
                    context_id = loop.initial_context_id
                    grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop_id))
                    metadata["budgets"] = grant.budgets
                metadata["source_revision_id"] = target.ref.revision_id
                # Warm exactly the same production index service/model contracts.
                if scenario in {"exact_cache", "small_append"}:
                    frozen = await LoopObservationService(sessions, service.checkpointer).capture(loop_id, receipt["round_id"])
                    builder = app.state.agent_loop_runtime._orchestrator._curators._index_service
                    warm = await builder.build(frozen)
                    metadata["preparation"] = {"blocker_code": warm.blocker_code,
                        "index_ids": [index.index_id for index in warm.indexes],
                        "quality": [index.quality_state for index in warm.indexes]}
                    if warm.catalog is None or any(index.quality_state != "complete" for index in warm.indexes):
                        sample.outcome, sample.failure_code = "precondition_failed", warm.blocker_code or "incomplete_warm_index"
                        return
                    if scenario == "small_append":
                        async with sessions.begin() as session:
                            target = await service.contexts.evolution.stage_definition(session, context_id,
                                (*_history(), {"id": "append", "role": "human", "content": "Documentation must include an offline quickstart and the JSON format."}),
                                (ContextRevisionSourceContract(source=target.ref, position=0),),
                                ContextRevisionOriginKind.RUN_SETTLED, "benchmark-synthetic-settled-append")
                        metadata["source_revision_id"] = target.ref.revision_id
                async with sessions() as session:
                    usage = await session.get(LoopBudgetUsage, loop_id)
                    before = {key: getattr(usage, key) for key in ("model_calls", "input_tokens", "output_tokens")}
                    metadata["usage_before_submission"] = before
                    preparation_ids = (await session.scalars(select(LoopContextDerivationArtifact.artifact_id).where(
                        LoopContextDerivationArtifact.loop_id == loop_id))).all()
                    metadata["preparation_artifact_ids"] = preparation_ids
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                            headers={"X-Focus-Session": "focus-dev-session"}) as http:
                    async def submit():
                        response = await http.post(f"/desktop/api/agent-loops/workspace/{workspace_id}/inputs", json={
                            "submission_id": uuid.uuid4().hex, "input_type": "information", "budget_preset": "normal",
                            "content": "请基于已有历史并行推进两个独立工作线：一条实现离线库存读写，一条撰写使用文档。请派生独立 Context 分工推进，现在开始，首条输出说明具体步骤。只读权限，给出内容即可。",
                        })
                        response.raise_for_status()
                        return response.json()
                    sample = await measure_submission(submit, CommittedSubmissionProbe(sessions,
                        preparation_artifact_ids=preparation_ids), scenario=scenario,
                        repeat=repeat, timeout_seconds=timeout,
                        after_acceptance=app.state.agent_loop_runtime.start)
                async with sessions() as session:
                    usage = await session.get(LoopBudgetUsage, loop_id)
                    metadata["measured_usage"] = {key: getattr(usage, key) - before[key] for key in before}
                    metadata["total_usage"] = {key: getattr(usage, key) for key in before}
                save_report()
            finally:
                await app.state.agent_loop_service.control(loop_id, "stop")
                await app.state.agent_loop_runtime.close()

        try:
            client.portal.call(run)
        except Exception as exc:
            if sample.milestones_ms:
                metadata["cleanup_error_type"] = type(exc).__name__
            else:
                sample.outcome, sample.failure_code = "error", type(exc).__name__
        finally:
            save_report()
    assert sample.outcome == "complete", f"{scenario}/{repeat}: {sample.outcome} ({sample.failure_code}); report: {output}"
