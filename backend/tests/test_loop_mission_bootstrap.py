r"""本文件对外提供问候 Run 与新 Mission 交接、Kernel 幂等提交及 Dispatcher 重放的隔离数据库回归测试。

输入为临时测试 Vault、已结算且只收到“你好”的直接用户 Run，以及明确的结构化工程 Mission；输出为
Loop 不会因虚构的目标缺失而创建澄清请求、Mission 正文仅一次交付和 Worker 投影一致的断言。具体工作流为播种独立 workspace/Context revision，
启动 Loop，经协调器、Kernel 与 Dispatcher 验证交付，检查结构化与旧版 Worker 载荷，再提交错误的等待提案并检查确定性拒绝。示例：
`python -m pytest backend/tests/test_loop_mission_bootstrap.py -q`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop.agent_loop import AgentLoopService, LoopCoordinator, LoopCreateRequest, LoopKernel, LoopWaveDispatcher, PatrolDecisionIntent
from backend.app.desktop.agent_loop.mission_contract import CompletionCheckDefinition, ExecutionBoundaries, LoopMissionContract
from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import PlanningRetrievalSession
from backend.app.desktop.agent_loop.authority_control import LoopAuthorityService
from backend.app.desktop.agent_loop.clarification_admission import ClarificationAdmissionPolicy, ClarificationFacts, ClarificationRejected
from backend.app.desktop.agent_loop.mission_bootstrap import MissionBootstrapStage
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.mission_projection import EffectiveMissionProjector
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant, LoopDirective, LoopGoalRevision, LoopPendingDecision, LoopRound, LoopWorkerRequest, MessageProvenance
from backend.app.desktop.agent_loop.provenance import DelegatedDirectiveFactory
from backend.app.desktop.agent_loop.round_orchestration import LoopRoundOrchestrator, PatrolDecisionProposal
from backend.app.desktop.agent_loop.rounds import current_frontier_hash
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope, NarrowLoopGrantRequest, ResumeWithCurrentMissionRequest, WaitEvidenceIdentity, WaitForUserAction
from backend.app.desktop.agent_loop.wait_models import LoopWaitRequest, LoopWaitResponse
from backend.app.desktop.agent_loop.wait_requests import LoopWaitRequestFactory, LoopWaitRequestService
from backend.app.desktop.agent_loop.workers import LoopWorkerRuntime
from backend.app.desktop.context_evolution import (
    ContextRevisionContract,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionRef,
    ContextRevisionRepository,
)
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.context_curation.contracts import MissionEvidenceRef
from backend.tests.config_helpers import app_config_for


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


async def _seed_greeting_loop(sessions, tmp_path: Path) -> dict:
    workspace_id = uuid.uuid4().hex
    context_id = uuid.uuid4().hex
    revision_id = uuid.uuid4().hex
    run_id = uuid.uuid4().hex
    loop_id = uuid.uuid4().hex
    vault = tmp_path / f"test-vault-{loop_id}"
    vault.mkdir()
    async with sessions.begin() as session:
        session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(vault), display_name="test Vault"))
        await session.flush()
        session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{context_id}", title="greeting"))
        await session.flush()
        ref = ContextRevisionRef(
            context_id=context_id,
            revision_id=revision_id,
            generation=1,
            execution_thread_id=f"thread-{context_id}",
            checkpoint_ns="",
            checkpoint_id=f"checkpoint-{context_id}",
            payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
        )
        revisions = ContextRevisionRepository()
        await revisions.insert(session, ContextRevisionContract(
            ref=ref,
            content_hash="a" * 64,
            projection_status=ContextRevisionProjectionStatus.VALID,
            origin_kind=ContextRevisionOriginKind.ROOT,
            created_at=datetime.now(UTC),
        ))
        await revisions.switch_current(session, ref, None)
        session.add(DesktopRun(
            run_id=run_id,
            task_id=context_id,
            agent_id=f"main:{context_id}",
            kind="main",
            status="success",
            origin="direct_user",
            execution_thread_id=f"thread-{context_id}",
            context_revision_id=revision_id,
            input_messages=[{"role": "user", "content": "你好"}],
            settled_at=datetime.now(UTC),
        ))
    mission = LoopMissionContract(
        outcome="交付可运行的 Obsidian 桌面宠物 Agent 插件",
        boundaries=ExecutionBoundaries(
            in_scope=("测试 Vault",),
            prohibited_actions=("修改真实 Vault",),
        ),
        completion_checks=(
            CompletionCheckDefinition(check_id="build", claim="生产构建通过", expected_evidence_kinds=("test",)),
            CompletionCheckDefinition(check_id="vault", claim="测试 Vault 工具通过", expected_evidence_kinds=("test",)),
        ),
    )
    service = AgentLoopService(sessions)
    snapshot = await service.start(LoopCreateRequest(
        loop_id=loop_id,
        workspace_id=workspace_id,
        initial_context_id=context_id,
        initial_run_id=run_id,
        holder_id="patrol-test",
        mission=mission,
        capabilities=("continue_context", "request_completion", "wait_for_user"),
        context_scope=(context_id,),
        permission_scope=("read", "write"),
    ))
    return {"service": service, "snapshot": snapshot, "loop_id": loop_id, "context_id": context_id, "revision_id": revision_id, "run_id": run_id, "mission": mission}


def test_legacy_observation_wait_and_retrieval_payloads_remain_readable() -> None:
    observation_payload = {
        "loop_id": "legacy-loop",
        "loop_revision": 1,
        "round_id": "legacy-round",
        "goal_revision": 1,
        "authority_revision": 1,
        "observed_frontier_hash": "a" * 64,
        "goal": {"goal": "旧目标", "task_contract": "原始\n边界", "acceptance_criteria": [{"criterion_id": "tests", "text": "测试通过"}]},
        "grant": {},
        "portfolio_frontier": [],
        "workspace": {"revision": 1},
        "budget": {},
    }
    retrieval_payload = {
        "session_id": "b" * 64,
        "observation_hash": "c" * 64,
        "frontier_hash": "a" * 64,
        "catalog_id": "d" * 64,
        "authorized_index_ids": ["e" * 64],
        "planner_version": "legacy-planner-v1",
        "retrieval_version": "legacy-retrieval-v1",
        "budget": {"max_queries": 1, "max_candidates": 1, "max_exact_reads": 1, "max_model_calls": 1, "max_tokens": 100},
    }
    originals = json.loads(json.dumps([observation_payload, retrieval_payload], ensure_ascii=False))

    observation = LoopObservationEnvelope.model_validate(observation_payload)
    retrieval = PlanningRetrievalSession.model_validate(retrieval_payload)
    wait = WaitForUserAction.model_validate({"action": "wait_for_user", "reason": "旧版原因"})
    evidence = MissionEvidenceRef.model_validate({"loop_id": "legacy-loop", "goal_revision": 1, "item_id": "tests", "content_hash": "f" * 64})

    assert observation.goal == observation_payload["goal"]
    assert retrieval.schema_version == "retrieval-session-v1"
    assert wait.reason == "旧版原因"
    assert evidence.item_id == "tests"
    assert [observation_payload, retrieval_payload] == originals


def test_clarification_policy_requires_current_specific_blocker() -> None:
    policy = ClarificationAdmissionPolicy()
    facts = ClarificationFacts(
        mission_revision=3,
        outcome="交付可运行插件",
        check_ids=frozenset({"build"}),
        capabilities=frozenset({"continue_context"}),
        pending_gate_ids=frozenset({"approval-1"}),
        budget_exhaustions=frozenset({"model_calls_budget"}),
        external_blockers=frozenset(),
        safe_continuation=False,
        permission_needs=frozenset({"write"}),
    )
    def action(cause: str, kind: str, reference_id: str, revision: int = 3) -> WaitForUserAction:
        return WaitForUserAction(
            action="wait_for_user",
            reason="需要具体用户决定",
            cause=cause,
            required_input="请决定是否继续",
            evidence_identity=WaitEvidenceIdentity(kind=kind, reference_id=reference_id, revision=revision),
        )

    policy.validate(action("human_gate", "gate", "approval-1"), facts)
    policy.validate(action("permission", "capability", "write"), facts)
    policy.validate(action("budget", "budget", "model_calls_budget"), facts)
    with pytest.raises(ClarificationRejected, match="已包含最终结果"):
        policy.validate(action("missing_goal", "mission", "outcome"), facts)
    with pytest.raises(ClarificationRejected, match="已过期"):
        policy.validate(action("human_gate", "gate", "approval-1", revision=2), facts)
    with pytest.raises(ClarificationRejected, match="不一致"):
        policy.validate(action("human_gate", "gate", "invented"), facts)
    with pytest.raises(ClarificationRejected, match="不一致"):
        policy.validate(action("permission", "capability", "arbitrary"), facts)

    safely_continuable = ClarificationFacts(
        mission_revision=3,
        outcome=facts.outcome,
        check_ids=facts.check_ids,
        capabilities=facts.capabilities,
        pending_gate_ids=frozenset(),
        budget_exhaustions=frozenset(),
        external_blockers=frozenset(),
        safe_continuation=True,
    )
    with pytest.raises(ClarificationRejected, match="未决输入请求"):
        policy.validate(action("missing_input", "mission", "missing:some_detail"), safely_continuable)
    with pytest.raises(ClarificationRejected, match="未决输入请求"):
        policy.validate(action("missing_input", "mission", "missing:some_detail"), facts)
    input_pending = ClarificationFacts(
        mission_revision=3,
        outcome=facts.outcome,
        check_ids=facts.check_ids,
        capabilities=facts.capabilities,
        pending_gate_ids=frozenset({"input-1"}),
        budget_exhaustions=frozenset(),
        external_blockers=frozenset(),
        safe_continuation=False,
        missing_input_ids=frozenset({"input-1"}),
    )
    policy.validate(action("missing_input", "input", "input-1"), input_pending)
    with pytest.raises(ClarificationRejected, match="未决输入请求"):
        policy.validate(action("missing_input", "input", "input-fabricated"), input_pending)


def test_new_patrol_wait_requires_typed_cause_while_legacy_audit_remains_readable() -> None:
    legacy = WaitForUserAction.model_validate({"action": "wait_for_user", "reason": "旧版原因"})
    assert legacy.cause is None
    with pytest.raises(ValidationError, match="cause"):
        PatrolDecisionProposal.model_validate({
            "rationale": "尝试澄清", "mission_references": ({"role": "outcome", "reference_id": "outcome"},),
            "actions": ({"action": "wait_for_user", "reason": "请补充目标"},),
        })


def test_bootstrap_delivers_current_mission_once_through_kernel(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture = await _seed_greeting_loop(sessions, tmp_path)
            snapshot = fixture["snapshot"]
            assert snapshot["mission_delivery"]["state"] == "pending"
            stage = MissionBootstrapStage()
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                round_row = await session.get(LoopRound, snapshot["current_round_id"])
                original = await session.get(DesktopRun, fixture["run_id"])
                grant = await session.get(LoopDelegationGrant, snapshot["grant"]["grant_id"])
                original.status = "running"
                assert (await stage.assess(session, loop, round_row)).reason == "active_run"
                original.status = "success"
                capabilities = grant.capabilities
                grant.capabilities = [item for item in capabilities if item != "continue_context"]
                assert (await stage.assess(session, loop, round_row)).reason == "continue_context_not_authorized"
                grant.capabilities = capabilities
                assessment = await stage.assess(session, loop, round_row)
            assert assessment.intent is not None
            content = assessment.intent.actions[0].message
            assert fixture["mission"].outcome in content
            assert "[build] 生产构建通过" in content
            assert "[vault] 测试 Vault 工具通过" in content
            assert "测试 Vault" in content
            assert "修改真实 Vault" in content
            assert "mission_bootstrap" not in content
            kernel = LoopKernel(sessions)
            first, replay = await asyncio.gather(kernel.commit(assessment.intent), kernel.commit(assessment.intent))
            assert first == replay
            assert first.status == "committed" and len(first.directive_ids) == 1
            async with sessions() as session:
                directive = await session.get(LoopDirective, first.directive_ids[0])
                provenance = await session.scalar(select(MessageProvenance).where(MessageProvenance.directive_id == directive.directive_id))
                original = await session.get(DesktopRun, fixture["run_id"])
                loop = await session.get(AgentLoop, fixture["loop_id"])
                round_row = await session.get(LoopRound, snapshot["current_round_id"])
                current = await stage.assess(session, loop, round_row)
                directives = tuple((await session.scalars(select(LoopDirective).where(LoopDirective.loop_id == fixture["loop_id"]))).all())
            assert directive.origin_kind == "mission_bootstrap"
            assert provenance.source_kind == "mission_bootstrap"
            assert DelegatedDirectiveFactory.to_model_message(directive).additional_kwargs == {}
            assert original.input_messages == [{"role": "user", "content": "你好"}]
            assert current.state == "authorized"
            assert len(directives) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_coordinator_bootstrap_and_replayed_dispatch_start_one_run(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture = await _seed_greeting_loop(sessions, tmp_path)
            coordinator = LoopCoordinator(sessions)
            claim = await coordinator.claim_for_loop(fixture["loop_id"], "mission-bootstrap-test")
            assert claim is not None
            orchestrator = LoopRoundOrchestrator(sessions, app_config_for("patrol-test", None), LoopKernel(sessions), None)
            committed = await orchestrator.process(claim)
            replay = await orchestrator.process(claim)
            assert committed is not None and committed.status == "committed"
            assert replay is None
            await coordinator.release(claim)
            launches = []

            async def launch(directive, message, _slot_id):
                launches.append((directive.directive_id, message))
                await asyncio.sleep(0.02)
                return uuid.uuid4().hex

            first, second = await asyncio.gather(
                LoopWaveDispatcher(sessions, launch).dispatch(fixture["loop_id"], claim.round_id, 1),
                LoopWaveDispatcher(sessions, launch).dispatch(fixture["loop_id"], claim.round_id, 1),
            )
            assert sorted((first, second), key=len)[0] == ()
            assert len(launches) == 1
            assert launches[0][1].content.startswith("请执行当前用户授权的任务。")
            assert launches[0][1].additional_kwargs == {}
            async with sessions() as session:
                directive = await session.get(LoopDirective, committed.directive_ids[0])
                original = await session.get(DesktopRun, fixture["run_id"])
            assert directive.lifecycle_state == "run_started"
            assert directive.origin_kind == "mission_bootstrap"
            assert original.input_messages == [{"role": "user", "content": "你好"}]
            assert await LoopWaveDispatcher(sessions, launch).dispatch(fixture["loop_id"], claim.round_id, 1) == ()
            assert len(launches) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_bootstrap_rejects_stale_mission_revision_before_delivery(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture = await _seed_greeting_loop(sessions, tmp_path)
            snapshot = fixture["snapshot"]
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                round_row = await session.get(LoopRound, snapshot["current_round_id"])
                old = await MissionBootstrapStage().assess(session, loop, round_row)
            assert old.intent is not None
            revised = await fixture["service"].override(fixture["loop_id"], mission=fixture["mission"].model_copy(update={"outcome": "交付新的 Obsidian 插件版本"}))
            stale = await LoopKernel(sessions).commit(old.intent)
            async with sessions() as session:
                directives = tuple((await session.scalars(select(LoopDirective).where(LoopDirective.loop_id == fixture["loop_id"], LoopDirective.origin_kind == "mission_bootstrap"))).all())
                original = await session.get(DesktopRun, fixture["run_id"])
            assert stale.status == "superseded"
            assert revised["mission_delivery"]["state"] == "pending"
            assert directives == ()
            assert original.input_messages == [{"role": "user", "content": "你好"}]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_bootstrap_rechecks_grant_and_frontier_before_commit(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture = await _seed_greeting_loop(sessions, tmp_path)
            snapshot = fixture["snapshot"]
            stage = MissionBootstrapStage()
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                round_row = await session.get(LoopRound, snapshot["current_round_id"])
                before = await stage.assess(session, loop, round_row)
            assert before.intent is not None
            await LoopAuthorityService(sessions).mutate(fixture["loop_id"], NarrowLoopGrantRequest(
                command="narrow",
                capabilities=("request_completion", "wait_for_user"),
                context_scope=(fixture["context_id"],),
                permission_scope=("read", "write"),
            ))
            stale = await LoopKernel(sessions).commit(before.intent)
            current = await fixture["service"].get(fixture["loop_id"])
            assert stale.status == "superseded"
            assert current["mission_delivery"]["state"] == "blocked"
            assert current["mission_delivery"]["reason"] == "continue_context_not_authorized"
            async with sessions() as session:
                directives = tuple((await session.scalars(select(LoopDirective).where(LoopDirective.loop_id == fixture["loop_id"], LoopDirective.origin_kind == "mission_bootstrap"))).all())
            assert directives == ()

            second = await _seed_greeting_loop(sessions, tmp_path)
            async with sessions() as session:
                loop = await session.get(AgentLoop, second["loop_id"])
                round_row = await session.get(LoopRound, second["snapshot"]["current_round_id"])
                before_frontier = await stage.assess(session, loop, round_row)
            async with sessions.begin() as session:
                round_row = await session.get(LoopRound, second["snapshot"]["current_round_id"], with_for_update=True)
                round_row.frontier_hash = "f" * 64
            frontier_result = await LoopKernel(sessions).commit(before_frontier.intent)
            assert frontier_result.status == "superseded"
            assert frontier_result.reason == "frontier_changed"
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("blocker", ["permission", "human_gate"])
def test_bootstrap_blocker_commits_typed_wait_through_kernel(tmp_path: Path, blocker: str) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture = await _seed_greeting_loop(sessions, tmp_path)
            gate_id = uuid.uuid4().hex
            if blocker == "permission":
                await LoopAuthorityService(sessions).mutate(fixture["loop_id"], NarrowLoopGrantRequest(
                    command="narrow", capabilities=("request_completion", "wait_for_user"),
                    context_scope=(fixture["context_id"],), permission_scope=("read", "write"),
                ))
            else:
                async with sessions.begin() as session:
                    session.add(LoopPendingDecision(
                        pending_decision_id=gate_id, loop_id=fixture["loop_id"],
                        kind="approval", delegable=False, status="pending", payload={"choice": "approve"},
                    ))
            coordinator = LoopCoordinator(sessions)
            claim = await coordinator.claim_for_loop(fixture["loop_id"], "bootstrap-wait-test")
            assert claim is not None
            try:
                orchestrator = LoopRoundOrchestrator(sessions, app_config_for("patrol-test", None), LoopKernel(sessions), None)
                result = await orchestrator.process(claim)
                assert result is not None and result.status == "committed"
            finally:
                await coordinator.release(claim)
            snapshot = await fixture["service"].get(fixture["loop_id"])
            assert snapshot["status"] == "waiting_user"
            assert snapshot["wait_request"]["scope"]["cause"] == ("permission" if blocker == "permission" else "human_gate")
            evidence = snapshot["wait_request"]["scope"]["evidence_identity"]
            assert evidence["reference_id"] == ("continue_context" if blocker == "permission" else gate_id)
            async with sessions() as session:
                directives = tuple((await session.scalars(select(LoopDirective).where(LoopDirective.loop_id == fixture["loop_id"]))).all())
            assert directives == ()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_bootstrap_does_not_create_wait_without_wait_authority(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture = await _seed_greeting_loop(sessions, tmp_path)
            await LoopAuthorityService(sessions).mutate(fixture["loop_id"], NarrowLoopGrantRequest(
                command="narrow", capabilities=("request_completion",),
                context_scope=(fixture["context_id"],), permission_scope=("read", "write"),
            ))
            coordinator = LoopCoordinator(sessions)
            claim = await coordinator.claim_for_loop(fixture["loop_id"], "bootstrap-no-wait-test")
            assert claim is not None
            try:
                orchestrator = LoopRoundOrchestrator(sessions, app_config_for("patrol-test", None), LoopKernel(sessions), None)
                assert await orchestrator.process(claim) is None
            finally:
                await coordinator.release(claim)
            snapshot = await fixture["service"].get(fixture["loop_id"])
            assert snapshot["status"] == "running"
            assert snapshot["mission_delivery"]["reason"] == "continue_context_not_authorized"
            assert snapshot["wait_request"] is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_bootstrap_wait_proposal_is_fenced_after_grant_change(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture = await _seed_greeting_loop(sessions, tmp_path)
            await LoopAuthorityService(sessions).mutate(fixture["loop_id"], NarrowLoopGrantRequest(
                command="narrow", capabilities=("request_completion", "wait_for_user"),
                context_scope=(fixture["context_id"],), permission_scope=("read", "write"),
            ))
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                round_row = await session.get(LoopRound, loop.current_round_id)
                proposal = await MissionBootstrapStage().assess(session, loop, round_row)
            assert proposal.intent is not None and proposal.intent.actions[0].cause == "permission"
            await LoopAuthorityService(sessions).mutate(fixture["loop_id"], NarrowLoopGrantRequest(
                command="narrow", capabilities=("request_completion",),
                context_scope=(fixture["context_id"],), permission_scope=("read", "write"),
            ))
            result = await LoopKernel(sessions).commit(proposal.intent)
            assert result.status == "superseded"
            assert (await fixture["service"].get(fixture["loop_id"]))["wait_request"] is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_worker_evidence_uses_the_canonical_structured_and_legacy_mission(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            runtime = LoopWorkerRuntime(sessions, app_config_for("worker-test", None))
            structured_fixture = await _seed_greeting_loop(sessions, tmp_path)
            structured_request = LoopWorkerRequest(
                worker_request_id=uuid.uuid4().hex, loop_id=structured_fixture["loop_id"],
                round_id=structured_fixture["snapshot"]["current_round_id"], kind="lane_curator", scope={},
            )
            structured_payload, _, _ = await runtime._evidence(structured_request)
            async with sessions() as session:
                structured = await session.scalar(select(LoopMissionRevision).where(LoopMissionRevision.loop_id == structured_fixture["loop_id"]))
            assert structured_payload["mission"] == EffectiveMissionProjector.from_rows(structured=structured, legacy=None).model_payload()
            assert structured_payload["mission"]["section_hashes"]["outcome"]
            assert "goal" not in structured_payload

            legacy_fixture = await _seed_greeting_loop(sessions, tmp_path)
            async with sessions.begin() as session:
                legacy_structured = await session.scalar(select(LoopMissionRevision).where(LoopMissionRevision.loop_id == legacy_fixture["loop_id"]))
                await session.delete(legacy_structured)
            legacy_request = LoopWorkerRequest(
                worker_request_id=uuid.uuid4().hex, loop_id=legacy_fixture["loop_id"],
                round_id=legacy_fixture["snapshot"]["current_round_id"], kind="lane_curator", scope={},
            )
            legacy_payload, _, _ = await runtime._evidence(legacy_request)
            async with sessions() as session:
                legacy = await session.scalar(select(LoopGoalRevision).where(LoopGoalRevision.loop_id == legacy_fixture["loop_id"]))
            assert legacy_payload["mission"] == EffectiveMissionProjector.from_rows(structured=None, legacy=legacy).model_payload()
            assert legacy_payload["mission"]["source_format"] == "legacy_adapter"
            assert "legacy_source" not in legacy_payload["mission"]
            assert "goal" not in legacy_payload
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_complete_new_mission_cannot_be_replaced_by_missing_goal_clarification(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture = await _seed_greeting_loop(sessions, tmp_path)
            snapshot = fixture["snapshot"]
            loop_id = fixture["loop_id"]
            run_id = fixture["run_id"]
            async with sessions() as session:
                original = await session.get(DesktopRun, run_id)
                round_row = await session.get(LoopRound, snapshot["current_round_id"])
                assert original.input_messages == [{"role": "user", "content": "你好"}]
            intent = PatrolDecisionIntent(
                decision_id=uuid.uuid4().hex,
                idempotency_key=f"false-missing-goal:{loop_id}",
                loop_id=loop_id,
                loop_revision=snapshot["revision"],
                round_id=round_row.round_id,
                holder_id="patrol-test",
                grant_id=snapshot["grant"]["grant_id"],
                grant_revision=snapshot["authority_revision"],
                goal_revision=snapshot["goal_revision"],
                observed_frontier_hash=round_row.frontier_hash,
                observed_workspace_revision=round_row.workspace_revision,
                rationale="没有派生机会，所以请用户重新给目标",
                actions=({
                    "action": "wait_for_user",
                    "reason": "请用户重新说明要做什么",
                    "cause": "missing_goal",
                    "required_input": "请重新给出最终结果",
                    "evidence_identity": {"kind": "mission", "reference_id": "outcome", "revision": snapshot["goal_revision"]},
                },),
            )
            result = await LoopKernel(sessions).commit(intent)
            async with sessions() as session:
                waits = tuple((await session.scalars(select(LoopWaitRequest).where(LoopWaitRequest.loop_id == loop_id, LoopWaitRequest.status == "open", LoopWaitRequest.kind == "clarification"))).all())
            assert result.status == "rejected", "完整 Mission 不能被缺失目标的虚假澄清取代"
            assert waits == ()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_stale_frontier_wait_proposal_never_opens_clarification(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture = await _seed_greeting_loop(sessions, tmp_path)
            snapshot = fixture["snapshot"]
            async with sessions() as session:
                round_row = await session.get(LoopRound, snapshot["current_round_id"])
                intent = PatrolDecisionIntent(
                    decision_id=uuid.uuid4().hex,
                    idempotency_key=f"stale-wait:{fixture['loop_id']}",
                    loop_id=fixture["loop_id"],
                    loop_revision=snapshot["revision"],
                    round_id=round_row.round_id,
                    holder_id="patrol-test",
                    grant_id=snapshot["grant"]["grant_id"],
                    grant_revision=snapshot["authority_revision"],
                    goal_revision=snapshot["goal_revision"],
                    observed_frontier_hash=round_row.frontier_hash,
                    observed_workspace_revision=round_row.workspace_revision,
                    rationale="请求权限",
                    actions=({
                        "action": "wait_for_user", "reason": "需要写入权限", "cause": "permission",
                        "required_input": "请授权写入",
                        "evidence_identity": {"kind": "capability", "reference_id": "write", "revision": snapshot["goal_revision"]},
                    },),
                )
            async with sessions.begin() as session:
                changed = await session.get(LoopRound, snapshot["current_round_id"], with_for_update=True)
                changed.frontier_hash = "b" * 64
            result = await LoopKernel(sessions).commit(intent)
            async with sessions() as session:
                waits = tuple((await session.scalars(select(LoopWaitRequest).where(
                    LoopWaitRequest.loop_id == fixture["loop_id"], LoopWaitRequest.status == "open",
                ))).all())
            assert result.status == "superseded" and result.reason == "frontier_changed"
            assert waits == ()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_explicit_recovery_reuses_current_mission_once_and_rejects_other_waits(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture = await _seed_greeting_loop(sessions, tmp_path)
            loop_id = fixture["loop_id"]
            old_round_id = fixture["snapshot"]["current_round_id"]
            async with sessions() as session:
                prior_round_count = len(tuple((await session.scalars(select(LoopRound).where(LoopRound.loop_id == loop_id))).all()))
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, loop_id, with_for_update=True)
                old_round = await session.get(LoopRound, old_round_id, with_for_update=True)
                old_round.status = "settled"
                wait = await LoopWaitRequestService().open(
                    session,
                    loop,
                    LoopWaitRequestFactory.clarification("请重新说明任务", {"cause": "missing_goal", "mission_revision": 1, "evidence_identity": {"kind": "mission", "reference_id": "outcome", "revision": 1}}),
                    created_by="portfolio-patrol",
                    correlation_id="old-decision",
                    round_id=old_round_id,
                )
                wait_id = wait.request_id
            body = ResumeWithCurrentMissionRequest(
                confirmation="resume_with_current_mission",
                request_revision=1,
                idempotency_key=f"resume:{wait_id}",
            )
            service = fixture["service"]
            first = await service.resume_with_current_mission(loop_id, wait_id, body, actor_id="user")
            replay = await service.resume_with_current_mission(loop_id, wait_id, body, actor_id="user")
            async with sessions() as session:
                waits = tuple((await session.scalars(select(LoopWaitRequest).where(LoopWaitRequest.loop_id == loop_id))).all())
                responses = tuple((await session.scalars(select(LoopWaitResponse).where(LoopWaitResponse.request_id == wait_id))).all())
                rounds = tuple((await session.scalars(select(LoopRound).where(LoopRound.loop_id == loop_id))).all())
                successor = await session.get(LoopRound, first["current_round_id"])
                assert successor.frontier_hash == await current_frontier_hash(session, loop_id)
            assert first["created"] is True and replay["created"] is False
            assert first["recovery_event_id"] == replay["recovery_event_id"]
            assert first["current_round_id"] != old_round_id
            assert replay["current_round_id"] == first["current_round_id"]
            assert len(waits) == 1 and waits[0].status == "superseded"
            assert responses == ()
            assert len(rounds) == prior_round_count + 1

            other = await _seed_greeting_loop(sessions, tmp_path)
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, other["loop_id"], with_for_update=True)
                budget_wait = await LoopWaitRequestService().open(
                    session,
                    loop,
                    LoopWaitRequestFactory.budget_action("模型调用上限", {}),
                    created_by="kernel",
                    correlation_id="budget-decision",
                )
                budget_wait_id = budget_wait.request_id
            with pytest.raises(HTTPException) as error:
                await other["service"].resume_with_current_mission(
                    other["loop_id"],
                    budget_wait_id,
                    ResumeWithCurrentMissionRequest(confirmation="resume_with_current_mission", request_revision=1, idempotency_key="wrong-budget"),
                    actor_id="user",
                )
            assert error.value.status_code == 409

            limited = await _seed_greeting_loop(sessions, tmp_path)
            async with sessions.begin() as session:
                limited_loop = await session.get(AgentLoop, limited["loop_id"], with_for_update=True)
                limited_round = await session.get(LoopRound, limited_loop.current_round_id, with_for_update=True)
                limited_round.status = "settled"
                limited_grant = await session.scalar(select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == limited_loop.loop_id,
                    LoopDelegationGrant.revision == limited_loop.authority_revision,
                ))
                limited_grant.budgets = {**limited_grant.budgets, "max_model_calls": 0}
                limited_wait = await LoopWaitRequestService().open(
                    session, limited_loop,
                    LoopWaitRequestFactory.clarification("请重新说明任务", {"cause": "missing_goal", "mission_revision": limited_loop.goal_revision, "evidence_identity": {"kind": "mission", "reference_id": "outcome", "revision": limited_loop.goal_revision}}),
                    created_by="portfolio-patrol", correlation_id="limited-decision", round_id=limited_round.round_id,
                )
            with pytest.raises(HTTPException, match="当前预算"):
                await limited["service"].resume_with_current_mission(
                    limited["loop_id"], limited_wait.request_id,
                    ResumeWithCurrentMissionRequest(confirmation="resume_with_current_mission", request_revision=1, idempotency_key="limited-resume"),
                    actor_id="user",
                )
            async with sessions() as session:
                persisted = await session.get(LoopWaitRequest, limited_wait.request_id)
                assert persisted.status == "open"
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("prompt,scope", [
    ("请批准写入", {}),
    ("请补充确实缺少的输入", {"cause": "missing_input", "evidence_identity": {"kind": "input", "reference_id": "input-1", "revision": 1}}),
    ("请批准写入", {"cause": "permission", "evidence_identity": {"kind": "capability", "reference_id": "write", "revision": 1}}),
])
def test_current_mission_recovery_keeps_unrelated_text_waits_open(tmp_path: Path, prompt: str, scope: dict) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture = await _seed_greeting_loop(sessions, tmp_path)
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"], with_for_update=True)
                round_row = await session.get(LoopRound, loop.current_round_id, with_for_update=True)
                round_row.status = "settled"
                wait = await LoopWaitRequestService().open(
                    session, loop, LoopWaitRequestFactory.clarification(prompt, scope),
                    created_by="portfolio-patrol", correlation_id=uuid.uuid4().hex, round_id=round_row.round_id,
                )
                request_id = wait.request_id
            with pytest.raises(HTTPException) as error:
                await fixture["service"].resume_with_current_mission(
                    fixture["loop_id"], request_id,
                    ResumeWithCurrentMissionRequest(confirmation="resume_with_current_mission", request_revision=1, idempotency_key=uuid.uuid4().hex),
                    actor_id="user",
                )
            assert error.value.status_code == 409
            async with sessions() as session:
                persisted = await session.get(LoopWaitRequest, request_id)
                assert persisted.status == "open"
                assert (await session.get(AgentLoop, fixture["loop_id"])).status == "waiting_user"
        finally:
            await engine.dispose()

    asyncio.run(run())
