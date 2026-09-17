r"""本文件验证 Patrol 自主压缩的领域契约、事务提交、迁移往返与用户优先级。

输入为消息协议组、保护锚点、版本化授权 facts、并发 Kernel intent 与隔离 PostgreSQL；输出为 bounded
manifest、纯 policy、唯一 resolution、无 Context 副作用及可逆 schema 的确定性断言。具体工作流为先
验证无正文 manifest 和 closed action，再在真实数据库中覆盖并发提交、用户 supersession 及
`head → down_revision → head` 迁移。示例：`pytest test_agent_loop_compression_authority.py`。
"""

from datetime import UTC, datetime, timedelta
import asyncio
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from backend.app.desktop.agent_loop.compression_authority.contracts import AutonomousCompressionPolicy, CompressionCandidateRequest
from backend.app.desktop.agent_loop.compression_authority.manifest import CompressionManifestBuilder
from backend.app.desktop.agent_loop.compression_authority.policy import CompressionAuthorityFacts, CompressionAuthorityPolicy
from backend.app.desktop.agent_loop.round_orchestration import PatrolCognitiveStep, StructuredPatrolDecisionModel
from backend.app.desktop.agent_loop.schemas import PATROL_ACTION_ADAPTER
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.compression_authority.models import LoopCompressionCandidate, LoopCompressionResolution
from backend.app.desktop.agent_loop.compression_authority.gate_projector import LoopCompressionGateProjector
from backend.app.desktop.agent_loop.compression_authority.resolution import CompressionResolutionCoordinator
from backend.app.desktop.agent_loop.gates import PendingDecisionProjector
from backend.app.desktop.agent_loop.kernel import LoopKernel
from backend.app.desktop.agent_loop.models import AgentLoop, LoopAction, LoopPendingDecision, LoopRound
from backend.app.desktop.agent_loop.schemas import LoopCreateRequest, PatrolDecisionIntent
from backend.app.desktop.agent_loop.service import AgentLoopService
from backend.app.desktop.context_evolution import ContextRevisionContract, ContextRevisionOriginKind, ContextRevisionPayloadMode, ContextRevisionProjectionStatus, ContextRevisionRef, ContextRevisionRepository
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace


def test_compression_authority_migration_downgrades_and_upgrades(isolated_postgres_database) -> None:
    migrations = Path(__file__).parents[1] / "packages" / "harness" / "focus" / "persistence" / "migrations" / "alembic.ini"
    config = Config(str(migrations))
    database_url = make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg")
    engine = create_engine(database_url)
    try:
        command.downgrade(config, "e2f3a4b5c6d7")
        schema = inspect(engine)
        assert "loop_compression_candidates" not in schema.get_table_names()
        assert "loop_compression_resolutions" not in schema.get_table_names()
        assert "compression_policy" not in {column["name"] for column in schema.get_columns("loop_delegation_grants")}
        command.upgrade(config, "head")
        schema = inspect(engine)
        assert {"loop_compression_candidates", "loop_compression_resolutions"} <= set(schema.get_table_names())
        assert "compression_policy" in {column["name"] for column in schema.get_columns("loop_delegation_grants")}
    finally:
        command.upgrade(config, "head")
        engine.dispose()


def test_manifest_is_bounded_body_free_and_keeps_tool_protocol_group() -> None:
    messages = (
        {"id": "system", "role": "system", "content": "secret safety text"},
        {"id": "human-old", "role": "human", "content": "investigate failure"},
        {"id": "ai-call", "role": "ai", "content": "", "tool_calls": [{"id": "call-1", "name": "shell", "args": {}}]},
        {"id": "tool-result", "role": "tool", "tool_call_id": "call-1", "content": "large private tool output"},
        {"id": "human-current", "role": "human", "content": "current instruction"},
    )
    builder = CompressionManifestBuilder()
    page = builder.page(messages, limit=3)
    assert page["total"] == 5
    assert len(page["items"]) == 3
    assert "content" not in page["items"][0]
    assert "secret safety text" not in str(page)
    normalized = builder.normalize(messages, ("tool-result", "human-old"), AutonomousCompressionPolicy(min_reduction_tokens=1))
    assert normalized.source_ids == ("human-old", "ai-call", "tool-result")


def test_manifest_rejects_protected_latest_human_and_system() -> None:
    messages = (
        {"id": "system", "role": "system", "content": "guard"},
        {"id": "human", "role": "human", "content": "current"},
    )
    builder = CompressionManifestBuilder()
    with pytest.raises(ValueError, match="受保护锚点"):
        builder.normalize(messages, ("system", "human"), AutonomousCompressionPolicy(min_reduction_tokens=1))


def test_manifest_protects_origin_material_and_open_protocol_with_auditable_reasons() -> None:
    messages = (
        {"id": "origin", "role": "human", "content": "task with 【图片1 material_id=abc123】"},
        {"id": "answer", "role": "ai", "content": "older result"},
        {"id": "answer-2", "role": "ai", "content": "another older result"},
        {"id": "open-call", "role": "ai", "content": "", "tool_calls": [{"id": "still-running", "name": "shell", "args": {}}]},
        {"id": "latest", "role": "human", "content": "continue"},
    )
    builder = CompressionManifestBuilder()
    page = builder.page(messages, protected_message_ids=("origin",))
    reasons = {item["message_id"]: item["protected_reason"] for item in page["items"]}
    assert reasons["origin"] == "current_direct_user_message"
    assert reasons["open-call"] == "active_tool_protocol"
    normalized = builder.normalize(
        messages,
        ("answer", "answer-2"),
        AutonomousCompressionPolicy(min_reduction_tokens=1),
        protected_message_ids=("origin",),
    )
    assert {item["reason"] for item in normalized.protection_evidence} >= {
        "current_direct_user_message",
        "active_tool_protocol",
        "latest_direct_user_message",
    }
    assert all(item["overlap"] is False for item in normalized.protection_evidence)


def test_manifest_rejects_duplicate_message_identity() -> None:
    messages = (
        {"id": "duplicate", "role": "human", "content": "one"},
        {"id": "duplicate", "role": "ai", "content": "two"},
    )
    with pytest.raises(ValueError, match="重复 message id"):
        CompressionManifestBuilder().page(messages)


def test_autonomous_compression_policy_round_trips_with_strict_safe_defaults() -> None:
    policy = AutonomousCompressionPolicy()
    assert AutonomousCompressionPolicy.model_validate_json(policy.model_dump_json()) == policy
    assert policy.allow_replace is True
    assert policy.allow_delete is False
    assert "current_direct_user_message" in policy.protected_anchors
    with pytest.raises(ValidationError):
        AutonomousCompressionPolicy.model_validate({"allow_delete": True, "unknown": "authority"})


def _facts(**overrides) -> CompressionAuthorityFacts:
    values = {
        "loop_status": "running",
        "loop_id": "loop",
        "workspace_id": "workspace",
        "authority_revision": 2,
        "goal_revision": 3,
        "round_id": "round",
        "round_authority_revision": 2,
        "round_goal_revision": 3,
        "grant_loop_id": "loop",
        "grant_revision": 2,
        "grant_status": "active",
        "grant_capabilities": frozenset({"apply_context_compression"}),
        "grant_gates": frozenset({"compression"}),
        "grant_context_scope": frozenset({"context"}),
        "pending_loop_id": "loop",
        "pending_kind": "compression",
        "pending_status": "pending",
        "pending_delegable": True,
        "candidate_loop_id": "loop",
        "candidate_pending_decision_id": "pending",
        "candidate_round_id": "round",
        "candidate_context_id": "context",
        "candidate_revision_id": "revision",
        "candidate_checkpoint_id": "checkpoint",
        "candidate_frontier_hash": "f" * 64,
        "candidate_authority_revision": 2,
        "candidate_goal_revision": 3,
        "candidate_policy_revision": 1,
        "candidate_status": "prepared",
        "candidate_expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "candidate_before_tokens": 2000,
        "candidate_after_tokens": 500,
        "candidate_protection_violations": 0,
        "action_pending_decision_id": "pending",
        "action_candidate_id": "candidate",
        "action_context_id": "context",
        "action_revision_id": "revision",
        "action_checkpoint_id": "checkpoint",
        "candidate_id": "candidate",
        "current_revision_id": "revision",
        "current_checkpoint_id": "checkpoint",
        "current_frontier_hash": "f" * 64,
        "policy_version": 1,
        "policy_min_reduction_tokens": 256,
    }
    values.update(overrides)
    return CompressionAuthorityFacts(**values)


def test_policy_allows_fresh_exact_candidate_and_rejects_every_authority_drift() -> None:
    policy = CompressionAuthorityPolicy()
    assert policy.evaluate(_facts()).allowed is True
    cases = {
        "authority_revision_changed": {"candidate_authority_revision": 1},
        "goal_revision_changed": {"candidate_goal_revision": 2},
        "context_revision_changed": {"current_revision_id": "new"},
        "checkpoint_changed": {"current_checkpoint_id": "new"},
        "frontier_changed": {"current_frontier_hash": "0" * 64},
        "protected_anchor_overlap": {"candidate_protection_violations": 1},
        "insufficient_token_reduction": {"candidate_after_tokens": 1900},
    }
    for reason, changes in cases.items():
        assert policy.evaluate(_facts(**changes)).reason == reason


def test_closed_action_and_cognitive_step_reject_arbitrary_or_multiple_outputs() -> None:
    action = PATROL_ACTION_ADAPTER.validate_python({
        "action": "apply_context_compression",
        "pending_decision_id": "pending",
        "candidate_id": "candidate",
        "context_id": "context",
        "context_revision_id": "revision",
        "checkpoint_id": "checkpoint",
    })
    assert action.candidate_id == "candidate"
    with pytest.raises(ValidationError):
        PATROL_ACTION_ADAPTER.validate_python({**action.model_dump(), "ranges": []})
    with pytest.raises(ValidationError):
        PatrolCognitiveStep.model_validate({
            "reads": [{"context_id": "context", "revision_id": "revision"}],
            "compression_candidate": {"pending_decision_id": "pending", "context_id": "context", "context_revision_id": "revision"},
        })


def test_patrol_candidate_branch_reads_body_free_manifest_before_exact_preparation() -> None:
    calls = []

    async def manifest(_observation, request):
        calls.append(("manifest", request.source_message_ids))
        return {"items": [{"message_id": "m1", "role": "tool", "tokens": 800}], "total": 1}

    async def prepare(_observation, request):
        calls.append(("prepare", request.source_message_ids))
        return SimpleNamespace(
            candidate_id="candidate",
            pending_decision_id="pending",
            context_id="context",
            base_context_revision_id="revision",
            base_checkpoint_id="checkpoint",
            normalized_ranges=[{"source_ids": ["m1", "m2"], "replacement": "summary"}],
            before_tokens=1800,
            after_tokens=300,
            expires_at=datetime.now(UTC),
        )

    model = StructuredPatrolDecisionModel(None, candidate_manifest=manifest, candidate_preparer=prepare)

    async def exercise() -> None:
        manifest_evidence = await model._cognitive_evidence(
            SimpleNamespace(loop_id="loop"),
            PatrolCognitiveStep(compression_candidate=CompressionCandidateRequest(
                pending_decision_id="pending",
                context_id="context",
                context_revision_id="revision",
            )),
        )
        assert manifest_evidence[0]["kind"] == "compression_manifest"
        assert "content" not in str(manifest_evidence)
        candidate_evidence = await model._cognitive_evidence(
            SimpleNamespace(loop_id="loop"),
            PatrolCognitiveStep(compression_candidate=CompressionCandidateRequest(
                pending_decision_id="pending",
                context_id="context",
                context_revision_id="revision",
                source_message_ids=("m1", "m2"),
            )),
        )
        assert candidate_evidence[0]["candidate_id"] == "candidate"
        assert calls == [("manifest", ()), ("prepare", ("m1", "m2"))]

    asyncio.run(exercise())


def test_resolution_recovery_observes_running_run_then_reconciles_applied_checkpoint() -> None:
    resolution = LoopCompressionResolution(
        resolution_id="resolution",
        loop_id="loop",
        pending_decision_id="pending",
        candidate_id="candidate",
        decision_id="decision",
        action_id="action",
        round_id="round",
        grant_id="grant",
        grant_revision=1,
        goal_revision=1,
        resume_payload_hash="a" * 64,
        idempotency_key="compression:recovery",
        status="resuming",
        resume_run_id="resume-run",
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    candidate = LoopCompressionCandidate(candidate_id="candidate", before_tokens=1800, after_tokens=300)
    pending = LoopPendingDecision(pending_decision_id="pending", status="resolving")
    action = LoopAction(action_id="action", status="committed", result={})
    round_row = LoopRound(round_id="round", status="running")
    loop = AgentLoop(loop_id="loop", status="running", health="waiting_runs")
    run = DesktopRun(run_id="resume-run", task_id="context", status="running", final_checkpoint_id=None)
    current = ContextRevisionContract(
        ref=ContextRevisionRef(
            context_id="context",
            revision_id="revision-after",
            generation=2,
            execution_thread_id="thread",
            checkpoint_ns="",
            checkpoint_id="checkpoint-after",
            payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
        ),
        content_hash="c" * 64,
        projection_status=ContextRevisionProjectionStatus.VALID,
        origin_kind=ContextRevisionOriginKind.RUN_SETTLED,
        origin_id="resume-run",
        created_at=datetime.now(UTC),
    )
    rows = {
        (LoopCompressionCandidate, "candidate"): candidate,
        (LoopPendingDecision, "pending"): pending,
        (LoopAction, "action"): action,
        (LoopRound, "round"): round_row,
        (AgentLoop, "loop"): loop,
        (DesktopRun, "resume-run"): run,
    }

    class ScalarRows:
        def all(self):
            return [resolution]

    class Session:
        async def scalars(self, _statement):
            return ScalarRows()

        async def get(self, model, identity, **_kwargs):
            return rows.get((model, identity))

    class Transaction:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return False

    class Sessions:
        def begin(self):
            return Transaction()

    class Revisions:
        async def current(self, _session, _context_id):
            return current

    async def exercise() -> None:
        coordinator = CompressionResolutionCoordinator(Sessions(), None)
        coordinator._revisions = Revisions()
        assert await coordinator.reconcile() == 0
        assert resolution.status == "resuming"
        run.status = "pending"
        assert await coordinator.reconcile() == 1
        assert resolution.status == "committed"
        resolution.status = "resuming"
        run.status = "error"
        assert await coordinator.reconcile() == 1
        assert resolution.status == "failed"
        resolution.status = "resuming"
        run.status = "success"
        run.settled_at = datetime.now(UTC)
        run.final_checkpoint_id = "checkpoint-after"
        assert await coordinator.reconcile() == 1
        assert resolution.status == "applied"
        assert resolution.result_checkpoint_id == "checkpoint-after"
        assert resolution.result_context_revision_id == "revision-after"
        assert resolution.actual_before_tokens == 1800
        assert resolution.actual_after_tokens == 300
        assert pending.status == "resolved"
        assert action.status == "applied"

    asyncio.run(exercise())


def test_resolution_launch_uses_resolution_identity_as_stable_physical_run_id(monkeypatch) -> None:
    resolution = LoopCompressionResolution(
        resolution_id="stable-resolution-run",
        loop_id="loop",
        pending_decision_id="pending",
        candidate_id="candidate",
        decision_id="decision",
        action_id="action",
        round_id="round",
        grant_id="grant",
        grant_revision=1,
        goal_revision=1,
        resume_payload_hash="a" * 64,
        idempotency_key="compression:stable-run",
        status="committed",
        lease_token="lease",
    )
    candidate = LoopCompressionCandidate(
        candidate_id="candidate",
        context_id="context",
        normalized_ranges=[{"source_ids": ["m1", "m2"], "replacement": "summary"}],
    )
    loop = AgentLoop(loop_id="loop")
    task = DesktopThread(task_id="context", thread_id="thread")
    rows = {
        (LoopCompressionResolution, "stable-resolution-run"): resolution,
        (LoopCompressionCandidate, "candidate"): candidate,
        (AgentLoop, "loop"): loop,
        (DesktopThread, "context"): task,
    }

    class Session:
        async def get(self, model, identity, **_kwargs):
            return rows.get((model, identity))

    class Transaction:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return False

    class Sessions:
        def __call__(self):
            return Transaction()

        def begin(self):
            return Transaction()

    identities = []

    class Desktop:
        bridge = run_manager = checkpointer = store = app_config = object()

        async def resume_run(self, _thread_id, _payload, identity):
            identities.append(identity)
            return SimpleNamespace(body=object(), thread_id="thread", agent_factory=object())

        def attach_run_sync(self, _record):
            return None

    async def execute(*_args, **_kwargs):
        return object()

    monkeypatch.setattr(
        "backend.app.desktop.agent_loop.compression_authority.resolution.execute_prepared_run",
        execute,
    )

    async def exercise() -> None:
        await CompressionResolutionCoordinator(Sessions(), Desktop())._launch("stable-resolution-run", "lease")
        assert identities[0]["run_id"] == "stable-resolution-run"
        assert resolution.resume_run_id == "stable-resolution-run"
        assert resolution.status == "resuming"

    asyncio.run(exercise())


def test_kernel_commits_one_resolution_without_mutating_context_and_user_override_wins(tmp_path, isolated_postgres_database, monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        suffix = uuid.uuid4().hex[:8]
        workspace_id = f"ws-compression-{suffix}"
        context_id = f"context-compression-{suffix}"
        revision_id = uuid.uuid4().hex
        loop_id = uuid.uuid4().hex
        workspace_path = tmp_path / workspace_id
        workspace_path.mkdir()
        service = AgentLoopService(sessions)
        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(workspace_path), display_name="compression"))
                await session.flush()
                session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{suffix}", title="compression"))
                await session.flush()
                ref = ContextRevisionRef(context_id=context_id, revision_id=revision_id, generation=1, execution_thread_id=f"thread-{suffix}", checkpoint_ns="", checkpoint_id="checkpoint-1", payload_mode=ContextRevisionPayloadMode.CHECKPOINT)
                await ContextRevisionRepository().insert(session, ContextRevisionContract(ref=ref, content_hash="a" * 64, projection_status=ContextRevisionProjectionStatus.VALID, origin_kind=ContextRevisionOriginKind.ROOT, created_at=datetime.now(UTC)))
                await ContextRevisionRepository().switch_current(session, ref, None)
                session.add(DesktopRun(run_id=f"initial-{suffix}", task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="success", origin="direct_user", origin_message_id="direct-message", execution_thread_id=f"thread-{suffix}", context_revision_id=revision_id, settled_at=datetime.now(UTC)))
            snapshot = await service.start(LoopCreateRequest(
                loop_id=loop_id,
                workspace_id=workspace_id,
                initial_context_id=context_id,
                initial_run_id=f"initial-{suffix}",
                holder_id=f"patrol:{loop_id}",
                goal="Finish the long task",
                task_contract="Preserve user constraints",
                acceptance_criteria=({"criterion_id": "done", "text": "done"},),
                capabilities=("continue_context", "apply_context_compression", "request_completion", "wait_for_user", "stop_loop"),
                context_scope=(context_id,),
                permission_scope=("read", "write"),
                delegable_gates=("compression",),
                compression_policy=AutonomousCompressionPolicy(min_reduction_tokens=100),
            ))
            async def recovery(*_args):
                return {"status": "resumable", "request": {"type": "compression_request", "usage": 120000}}

            monkeypatch.setattr(
                "backend.app.desktop.agent_loop.compression_authority.gate_projector.compression_recovery_payload",
                recovery,
            )
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, loop_id)
                run = await session.get(DesktopRun, f"initial-{suffix}")
                run.round_id = snapshot["current_round_id"]
                projected = await LoopCompressionGateProjector(None, PendingDecisionProjector(sessions)).project_settled(
                    session,
                    type("Settled", (), {"payload": {"context_revision": {"revision_id": revision_id, "checkpoint_id": "checkpoint-1"}}})(),
                    run,
                    loop,
                )
            assert projected is not None and projected.delegable is True
            assert projected.payload["origin_message_id"] == "direct-message"
            assert projected.payload["round_id"] == snapshot["current_round_id"]
            async with sessions.begin() as session:
                round_row = await session.get(LoopRound, snapshot["current_round_id"])
                candidate = LoopCompressionCandidate(
                    candidate_id=uuid.uuid4().hex,
                    loop_id=loop_id,
                    pending_decision_id=projected.pending_decision_id,
                    round_id=round_row.round_id,
                    context_id=context_id,
                    base_context_revision_id=revision_id,
                    base_checkpoint_id="checkpoint-1",
                    frontier_hash=round_row.frontier_hash,
                    authority_revision=1,
                    goal_revision=1,
                    policy_revision=1,
                    normalized_ranges=[{"source_ids": ["m1", "m2"], "replacement": "summary"}],
                    replacement_hash="b" * 64,
                    candidate_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                    before_tokens=2000,
                    after_tokens=400,
                    protection_evidence=[],
                    expires_at=datetime.now(UTC) + timedelta(minutes=5),
                )
                session.add(candidate)
            intent = PatrolDecisionIntent(
                decision_id=uuid.uuid4().hex,
                idempotency_key=f"compression:{suffix}",
                loop_id=loop_id,
                loop_revision=snapshot["revision"],
                round_id=snapshot["current_round_id"],
                holder_id=f"patrol:{loop_id}",
                grant_id=snapshot["grant"]["grant_id"],
                grant_revision=1,
                goal_revision=1,
                observed_frontier_hash=round_row.frontier_hash,
                observed_workspace_revision=1,
                rationale="The stable Context is above its threshold.",
                actions=({
                    "action": "apply_context_compression",
                    "pending_decision_id": projected.pending_decision_id,
                    "candidate_id": candidate.candidate_id,
                    "context_id": context_id,
                    "context_revision_id": revision_id,
                    "checkpoint_id": "checkpoint-1",
                },),
            )
            kernel = LoopKernel(sessions)
            committed, concurrent_duplicate = await asyncio.gather(kernel.commit(intent), kernel.commit(intent))
            assert committed.status == "committed"
            assert concurrent_duplicate == committed
            assert await kernel.commit(intent) == committed
            claims = await asyncio.gather(
                CompressionResolutionCoordinator(sessions, None)._claim(),
                CompressionResolutionCoordinator(sessions, None)._claim(),
            )
            assert sum(claim is not None for claim in claims) == 1
            async with sessions() as session:
                resolution = await session.scalar(select(LoopCompressionResolution).where(LoopCompressionResolution.loop_id == loop_id))
                context = await session.get(DesktopThread, context_id)
                assert resolution.status == "committed"
                assert context.current_revision_id == revision_id
            await service.override(loop_id, "User changed direction", "New contract", [{"criterion_id": "new", "text": "new"}])
            async with sessions() as session:
                resolution = await session.scalar(select(LoopCompressionResolution).where(LoopCompressionResolution.loop_id == loop_id))
                candidate = await session.get(LoopCompressionCandidate, candidate.candidate_id)
                assert resolution.status == "superseded"
                assert candidate.status == "superseded"
        finally:
            snapshot = await service.get(loop_id)
            if snapshot["status"] in {"running", "paused", "waiting_user"}:
                await service.control(loop_id, "stop")
            await engine.dispose()

    asyncio.run(run())
