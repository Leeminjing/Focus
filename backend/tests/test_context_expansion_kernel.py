r"""本文件对外提供 additive Context expansion 的 Kernel、并发发布、事务重试与提交后恢复集成测试。

输入为真实 PostgreSQL 中带活动 Run 的 Loop、冻结来源 Revision、read-only 或 isolated-write 内部 CreateLanePlan；
输出为边界 assessment、只读增量派生获授权、原子创建第二 Context、Directive 交付、双 Run 并存、并发发布收敛、
serialization 重试不泄漏身份、提交后重启仅派发一次、compiler blocker 终结 Round，以及未授权隔离写入被拒绝的断言。
具体工作流为播种 Loop、登记 expansion lifecycle、提交 Kernel intent、注入首次提交回滚或同时发布、从新连接恢复 Outbox 派发，
并核对持久身份、终态历史与释放后的独立性覆盖。
示例：`pytest backend/tests/test_context_expansion_kernel.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop.agent_loop import LoopKernel, LoopWaveDispatcher, PatrolDecisionIntent
from backend.app.desktop.agent_loop.coordinator import CoordinatorClaim
from backend.app.desktop.agent_loop.context_expansion.coordinator import ContextExpansionStage
from backend.app.desktop.agent_loop.context_expansion.contracts import ExpansionBlocker, ExpansionOpportunity
from backend.app.desktop.agent_loop.context_expansion.models import LoopContextExpansion
from backend.app.desktop.agent_loop.context_expansion.repository import ContextExpansionRepository
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.agent_loop.models import AgentLoop, LoopContextMembership, LoopDelegationGrant, LoopDirective, LoopRound
from backend.app.desktop.agent_loop.patrol_runtime import PatrolSessionLifecycle
from backend.app.desktop.agent_loop.patrol_session_state import PatrolActivity, PatrolPhase
from backend.app.desktop.agent_loop.portfolio_publication import LoopPortfolioAuthorityHook, LoopPortfolioPublicationService
from backend.app.desktop.agent_loop.round_orchestration import LoopRoundOrchestrator
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.context_curation import ComposeMessage, CreateLanePlan, CurationLane, NamespacedMessageRef
from backend.app.desktop.context_evolution import ContextRevisionPayloadMode, ContextRevisionRef
from backend.app.desktop.models import DesktopRun, DesktopThread

from test_agent_loop_round_liveness import _seed_loop, _stop
from config_helpers import app_config_for


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


class _SourceCheckpointer(BaseCheckpointSaver):
    def __init__(self) -> None:
        super().__init__()

    async def aget_tuple(self, config):
        checkpoint_id = config["configurable"]["checkpoint_id"]
        return SimpleNamespace(
            config={"configurable": {"checkpoint_id": checkpoint_id}},
            checkpoint={
                "channel_values": {
                    "messages": [
                        HumanMessage(
                            content="Implement the feature and preserve independent verification evidence.",
                            id="source-evidence",
                        )
                    ]
                }
            },
            metadata={},
        )


class _ShadowGraph:
    def __init__(self) -> None:
        self.checkpointer = None
        self._messages = []

    async def aupdate_state(self, config, values):
        self._messages = list(values["messages"])
        return {
            "configurable": {
                **config["configurable"],
                "checkpoint_id": f"checkpoint-{uuid.uuid4().hex}",
            }
        }

    async def aget_state(self, config):
        return SimpleNamespace(config=config, values={"messages": self._messages})


class _ContextService:
    def __init__(self) -> None:
        self.checkpointer = _SourceCheckpointer()

    @staticmethod
    async def make_state_graph():
        return _ShadowGraph()


def test_read_only_expansion_can_be_authorized_while_source_run_is_active(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        seeded = await _seed_loop(sessions, tmp_path, label="additive-read", started_at=datetime.now(UTC))
        try:
            await _grant_create_lane_and_add_active_run(sessions, seeded)
            intent = await _create_lane_intent(sessions, seeded, "read_only")

            result = await LoopKernel(sessions, queue_portfolio_publication=True).commit(intent)

            assert result.status == "publishing"
        finally:
            await _stop(seeded["service"], seeded["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_isolated_write_expansion_requires_workspace_adoption_authority(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        seeded = await _seed_loop(sessions, tmp_path, label="additive-write", started_at=datetime.now(UTC))
        try:
            await _grant_create_lane_and_add_active_run(sessions, seeded)
            intent = await _create_lane_intent(sessions, seeded, "isolated_write")

            result = await LoopKernel(sessions, queue_portfolio_publication=True).commit(intent)

            assert result.status == "rejected"
            assert "隔离写入" in (result.reason or "")
        finally:
            await _stop(seeded["service"], seeded["loop_id"])
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("case", "expected_level", "expected_code"),
    (
        ("no_split", "not_applicable", "not_independent"),
        ("context_budget", "not_applicable", "context_budget_exhausted"),
        ("token_pressure", "required", None),
        ("duplicate", "not_applicable", "duplicate_expansion"),
    ),
)
def test_persisted_assessment_pipeline_covers_expansion_boundaries(
    tmp_path: Path,
    case: str,
    expected_level: str,
    expected_code: str | None,
) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        seeded = await _seed_loop(sessions, tmp_path, label=f"bound-{case[:6]}", started_at=datetime.now(UTC))
        try:
            await _grant_create_lane_and_add_active_run(sessions, seeded)
            observation = _expansion_observation(seeded)
            if case in {"no_split", "token_pressure"}:
                mission = {
                    **observation.mission,
                    "completion_checks": observation.mission["completion_checks"][:1],
                }
                observation = observation.model_copy(update={"mission": mission})
            if case == "context_budget":
                observation = observation.model_copy(
                    update={
                        "budget": {
                            **observation.budget,
                            "usage": {**observation.budget["usage"], "contexts": 16},
                        }
                    }
                )
            elif case == "token_pressure":
                observation = observation.model_copy(
                    update={
                        "budget": {
                            **observation.budget,
                            "usage": {**observation.budget["usage"], "input_tokens": 950},
                        }
                    }
                )
            elif case == "duplicate":
                prior_round_id = uuid.uuid4().hex
                async with sessions.begin() as session:
                    current = await session.get(LoopRound, seeded["round_id"])
                    session.add(
                        LoopRound(
                            round_id=prior_round_id,
                            loop_id=seeded["loop_id"],
                            number=0,
                            status="settled",
                            authority_revision=current.authority_revision,
                            goal_revision=current.goal_revision,
                            frontier_hash=current.frontier_hash,
                            workspace_revision=current.workspace_revision,
                            settled_at=datetime.now(UTC),
                        )
                    )
                    await session.flush()
                    for check in observation.mission["completion_checks"]:
                        prior = ExpansionOpportunity.create(
                            loop_id=seeded["loop_id"],
                            round_id=prior_round_id,
                            source=_source_ref(seeded),
                            purpose=f"Earlier independent {check['check_id']}",
                            work_order="Verify independently.",
                            completion_check=check["claim"],
                            workspace_mode="read_only",
                            independence_key=f"completion-check:{check['check_id']}",
                            triggers=("independent_verification",),
                            required=True,
                        )
                        await ContextExpansionRepository().create(
                            session,
                            prior,
                            policy_version="context-expansion-v1",
                            level="required",
                        )

            assessment = await ContextExpansionStage(sessions, _SourceCheckpointer()).assess(observation)
            async with sessions() as session:
                rows = await ContextExpansionRepository().by_round(session, seeded["round_id"])

            assert assessment.level == expected_level
            if expected_code is None:
                assert {item.independence_key for item in assessment.opportunities} == {"token-pressure-continuation"}
                assert rows and all(row.state in {"detected", "curated"} for row in rows)
            else:
                assert expected_code in {item.code for item in assessment.blockers}
                if case == "no_split":
                    assert rows == ()
                else:
                    assert rows and all(row.state == "blocked" for row in rows)
        finally:
            await _stop(seeded["service"], seeded["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_read_only_expansion_retries_atomically_and_dispatches_once_after_restart(tmp_path: Path, monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        seeded = await _seed_loop(sessions, tmp_path, label="expansion-e2e", started_at=datetime.now(UTC))
        try:
            await _grant_create_lane_and_add_active_run(sessions, seeded)
            context_service = _ContextService()
            stage = ContextExpansionStage(sessions, context_service.checkpointer)
            observation = _expansion_observation(seeded)
            assessment = await stage.assess(observation)
            assert {item.independence_key for item in assessment.opportunities} == {
                "completion-check:implementation",
                "completion-check:verification",
            }
            opportunity = next(
                item
                for item in assessment.opportunities
                if item.independence_key == "completion-check:verification"
            )
            base_intent = await _create_lane_intent(sessions, seeded, "read_only")
            semantic_intent = PatrolDecisionIntent.model_validate(
                {
                    **base_intent.model_dump(mode="json"),
                    "actions": [
                        {
                            "action": "spawn_context",
                            "opportunity_id": opportunity.opportunity_id,
                            "source_context_id": opportunity.source.context_id,
                            "purpose": opportunity.purpose,
                            "work_order": opportunity.work_order,
                            "completion_check": opportunity.completion_check,
                            "workspace_mode": opportunity.workspace_mode,
                        }
                    ],
                }
            )
            resolution = await stage.resolve(
                observation.model_copy(
                    update={"expansion_assessment": assessment.model_dump(mode="json")}
                ),
                semantic_intent,
            )
            assert resolution.blocker is None
            assert resolution.intent is not None
            intent = resolution.intent
            original_commit = LoopPortfolioAuthorityHook.commit
            attempted_directives = []

            class SerializationFailure(Exception):
                sqlstate = "40001"

            async def fail_first_commit(hook, *args, **kwargs):
                await original_commit(hook, *args, **kwargs)
                attempted_directives.append(hook.directive_ids)
                if len(attempted_directives) == 1:
                    raise DBAPIError("COMMIT", {}, SerializationFailure("retry"), False)

            monkeypatch.setattr(LoopPortfolioAuthorityHook, "commit", fail_first_commit)
            publisher = LoopPortfolioPublicationService(sessions, context_service)
            committed = await LoopKernel(sessions, publisher).commit(intent)
            assert committed.status == "committed", committed.reason
            assert len(committed.directive_ids) == 1
            assert len(attempted_directives) == 2
            assert attempted_directives[0] == attempted_directives[1] == committed.directive_ids

            run_id = uuid.uuid4().hex
            restart_engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
            restart_sessions = async_sessionmaker(restart_engine, expire_on_commit=False)

            async def launch(directive, _message, _slot_id):
                async with restart_sessions.begin() as session:
                    session.add(
                        DesktopRun(
                            run_id=run_id,
                            task_id=directive.target_context_id,
                            agent_id=f"main:{directive.target_context_id}",
                            kind="main",
                            status="running",
                            origin="delegated_patrol",
                            execution_thread_id=f"thread-{directive.target_context_id}",
                            context_revision_id=directive.target_context_revision_id,
                            directive_id=directive.directive_id,
                            loop_id=seeded["loop_id"],
                            round_id=seeded["round_id"],
                        )
                    )
                return run_id

            restarted_dispatcher = LoopWaveDispatcher(restart_sessions, launch)
            launched = await restarted_dispatcher.dispatch(
                seeded["loop_id"],
                seeded["round_id"],
                2,
            )
            assert launched == (run_id,)
            assert await restarted_dispatcher.dispatch(seeded["loop_id"], seeded["round_id"], 2) == ()

            async with restart_sessions() as session:
                expansion = await session.get(LoopContextExpansion, opportunity.opportunity_id)
                directive = await session.get(LoopDirective, committed.directive_ids[0])
                context = await session.get(DesktopThread, expansion.result["context_id"])
                grant = await session.scalar(
                    select(LoopDelegationGrant).where(
                        LoopDelegationGrant.loop_id == seeded["loop_id"],
                        LoopDelegationGrant.status == "active",
                    )
                )
                membership = await session.scalar(
                    select(LoopContextMembership).where(
                        LoopContextMembership.loop_id == seeded["loop_id"],
                        LoopContextMembership.context_id == context.task_id,
                    )
                )
                running = tuple(
                    (
                        await session.scalars(
                            select(DesktopRun).where(
                                DesktopRun.loop_id == seeded["loop_id"],
                                DesktopRun.status == "running",
                            )
                        )
                    ).all()
                )
                coverage = await ContextExpansionRepository().active_independence_keys(
                    session,
                    seeded["loop_id"],
                )
                expansion_events = tuple(
                    (
                        await session.scalars(
                            select(LoopJournalEvent)
                            .where(
                                LoopJournalEvent.loop_id == seeded["loop_id"],
                                LoopJournalEvent.entity_type == "context_expansion",
                                LoopJournalEvent.entity_id == opportunity.opportunity_id,
                            )
                            .order_by(LoopJournalEvent.sequence)
                        )
                    ).all()
                )
                context_events = tuple(
                    (
                        await session.scalars(
                            select(LoopJournalEvent)
                            .where(
                                LoopJournalEvent.loop_id == seeded["loop_id"],
                                LoopJournalEvent.entity_type == "context",
                                LoopJournalEvent.entity_id == context.task_id,
                            )
                            .order_by(LoopJournalEvent.sequence)
                        )
                    ).all()
                )
                persisted_directives = tuple(
                    (
                        await session.scalars(
                            select(LoopDirective).where(LoopDirective.decision_id == committed.decision_id)
                        )
                    ).all()
                )
            await restart_engine.dispose()

            assert expansion.state == "dispatched"
            assert expansion.result["directive_id"] == directive.directive_id
            assert expansion.result["run_id"] == run_id
            assert directive.lifecycle_state == "run_started"
            assert context.current_revision_id == expansion.result["context_revision_id"]
            assert membership.lane_id == expansion.result["lane_id"]
            assert context.task_id in grant.context_scope
            assert {item.task_id for item in running} == {seeded["context_id"], context.task_id}
            assert opportunity.independence_key in coverage
            assert len(persisted_directives) == 1
            assert [item.kind for item in context_events] == ["context.created"]
            assert [item.kind for item in expansion_events] == [
                "context_expansion.detected",
                "context_expansion.proposed",
                "context_expansion.compiled",
                "context_expansion.authorized",
                "context_expansion.committed",
                "context_expansion.dispatched",
            ]

            async with restart_sessions.begin() as session:
                membership = await session.scalar(
                    select(LoopContextMembership).where(
                        LoopContextMembership.loop_id == seeded["loop_id"],
                        LoopContextMembership.context_id == context.task_id,
                    )
                )
                lane = await session.get(CurationLane, membership.lane_id)
                membership.status = "discarded"
                lane.lifecycle = "retired"
            async with restart_sessions() as session:
                retired_coverage = await ContextExpansionRepository().active_independence_keys(
                    session,
                    seeded["loop_id"],
                )
            assert opportunity.independence_key not in retired_coverage
        finally:
            await _stop(seeded["service"], seeded["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_concurrent_expansion_publication_converges_on_one_context(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        seeded = await _seed_loop(sessions, tmp_path, label="exp-concurrent", started_at=datetime.now(UTC))
        try:
            await _grant_create_lane_and_add_active_run(sessions, seeded)
            context_service = _ContextService()
            stage = ContextExpansionStage(sessions, context_service.checkpointer)
            observation = _expansion_observation(seeded)
            assessment = await stage.assess(observation)
            opportunity = next(
                item
                for item in assessment.opportunities
                if item.independence_key == "completion-check:verification"
            )
            semantic = await _semantic_intent(sessions, seeded, opportunity)
            resolution = await stage.resolve(
                observation.model_copy(update={"expansion_assessment": assessment.model_dump(mode="json")}),
                semantic,
            )
            assert resolution.intent is not None
            queued = await LoopKernel(sessions, queue_portfolio_publication=True).commit(resolution.intent)
            assert queued.status == "publishing"

            publisher = LoopPortfolioPublicationService(sessions, context_service)
            first, second = await asyncio.gather(
                publisher.publish(queued.decision_id),
                publisher.publish(queued.decision_id),
            )

            assert first.portfolio.portfolio_revision_id == second.portfolio.portfolio_revision_id
            assert first.directive_ids == second.directive_ids
            assert len(first.directive_ids) == 1
            replayed = await publisher.publish(queued.decision_id)
            assert replayed.portfolio.portfolio_revision_id == first.portfolio.portfolio_revision_id
            assert replayed.portfolio.idempotent is True
            assert replayed.directive_ids == first.directive_ids
            async with sessions() as session:
                expansion = await session.get(LoopContextExpansion, opportunity.opportunity_id)
                memberships = tuple(
                    (
                        await session.scalars(
                            select(LoopContextMembership).where(
                                LoopContextMembership.loop_id == seeded["loop_id"],
                                LoopContextMembership.lane_id == expansion.result["lane_id"],
                            )
                        )
                    ).all()
                )
                directives = tuple(
                    (
                        await session.scalars(
                            select(LoopDirective).where(LoopDirective.decision_id == queued.decision_id)
                        )
                    ).all()
                )
            assert len(memberships) == 1
            assert len(directives) == 1
        finally:
            await _stop(seeded["service"], seeded["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_goal_revision_supersedes_compiled_expansion_before_authorization(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        seeded = await _seed_loop(sessions, tmp_path, label="expansion-goal", started_at=datetime.now(UTC))
        try:
            await _grant_create_lane_and_add_active_run(sessions, seeded)
            opportunity = ExpansionOpportunity.create(
                loop_id=seeded["loop_id"],
                round_id=seeded["round_id"],
                source=_source_ref(seeded),
                purpose="Independent verification",
                work_order="Verify the original goal independently.",
                completion_check="Report reproducible evidence for the original goal.",
                workspace_mode="read_only",
                independence_key="completion-check:goal-revision",
                triggers=("independent_verification",),
                required=True,
            )
            repository = ContextExpansionRepository()
            async with sessions.begin() as session:
                await repository.create(session, opportunity, policy_version="context-expansion-v1", level="required")
                await repository.transition(session, opportunity.opportunity_id, "proposed", "Patrol 已提出派生")
                await repository.transition(session, opportunity.opportunity_id, "compiled", "已编译可运行 LanePlan")
            stale_intent = await _create_lane_intent(
                sessions,
                seeded,
                "read_only",
                expansion_id=opportunity.opportunity_id,
            )

            await seeded["service"].override(
                seeded["loop_id"],
                "Deliver the revised goal",
                "Do not execute work derived from the previous Mission revision",
                [{"criterion_id": "revised", "text": "revised evidence exists"}],
            )
            result = await LoopKernel(sessions, queue_portfolio_publication=True).commit(stale_intent)

            async with sessions() as session:
                expansion = await session.get(LoopContextExpansion, opportunity.opportunity_id)
            assert result.status == "superseded"
            assert result.reason == "mission_revision_changed"
            assert expansion.state == "superseded"
            assert expansion.safe_summary == "mission_revision_changed"
        finally:
            await _stop(seeded["service"], seeded["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_compiler_blocker_settles_round_and_terminal_history_prevents_retry(tmp_path: Path, monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        seeded = await _seed_loop(sessions, tmp_path, label="exp-block", started_at=datetime.now(UTC))
        try:
            await _grant_create_lane_and_add_active_run(sessions, seeded)
            stage = ContextExpansionStage(sessions, _SourceCheckpointer())
            observation = _expansion_observation(seeded)
            assessment = await stage.assess(observation)
            opportunity = next(
                item
                for item in assessment.opportunities
                if item.independence_key == "completion-check:verification"
            )
            semantic = await _semantic_intent(sessions, seeded, opportunity)
            blocker = ExpansionBlocker(
                code="compiler_failed",
                summary="冻结 Tool Exchange 不完整",
                opportunity_id=opportunity.opportunity_id,
            )

            async def blocked_compile(*_args, **_kwargs):
                return blocker

            monkeypatch.setattr(stage._compiler, "compile", blocked_compile)
            resolution = await stage.resolve(
                observation.model_copy(update={"expansion_assessment": assessment.model_dump(mode="json")}),
                semantic,
            )
            repeated = await stage.assess(observation)

            assert resolution.intent is None
            assert resolution.blocker == blocker
            assert opportunity.opportunity_id not in {item.opportunity_id for item in repeated.opportunities}
            assert any(item.code == "compiler_failed" for item in repeated.blockers)

            claim = CoordinatorClaim(
                lease_id=uuid.uuid4().hex,
                loop_id=seeded["loop_id"],
                round_id=seeded["round_id"],
                fencing_token="1",
            )
            lifecycle = PatrolSessionLifecycle(sessions)
            patrol = await lifecycle.begin(claim)
            await lifecycle.transition(patrol.session_id, PatrolPhase.OBSERVING, PatrolActivity(summary="已冻结观察"))
            await lifecycle.transition(patrol.session_id, PatrolPhase.PROPOSING, PatrolActivity(summary="Patrol 提议派生"))
            orchestrator = LoopRoundOrchestrator(
                sessions,
                app_config_for("exp-block", None),
                LoopKernel(sessions),
                _SourceCheckpointer(),
            )
            await orchestrator._settle_expansion_blocker(claim, patrol.session_id, blocker)
            await orchestrator._settle_expansion_blocker(claim, patrol.session_id, blocker)

            async with sessions() as session:
                expansion = await session.get(LoopContextExpansion, opportunity.opportunity_id)
                round_row = await session.get(LoopRound, seeded["round_id"])
                loop = await session.get(AgentLoop, seeded["loop_id"])
            patrol_state = await lifecycle.get(patrol.session_id)
            assert expansion.state == "blocked"
            assert expansion.blocker_code == "compiler_failed"
            assert round_row.status == "error"
            assert loop.status == "waiting_user"
            assert "compiler_failed" in (loop.waiting_reason or "")
            assert patrol_state.phase == PatrolPhase.FAILED
        finally:
            await _stop(seeded["service"], seeded["loop_id"])
            await engine.dispose()

    asyncio.run(run())


async def _grant_create_lane_and_add_active_run(sessions, seeded: dict) -> None:
    async with sessions.begin() as session:
        grant = await session.scalar(
            select(LoopDelegationGrant).where(
                LoopDelegationGrant.loop_id == seeded["loop_id"],
                LoopDelegationGrant.revision == seeded["snapshot"]["authority_revision"],
            )
        )
        grant.capabilities = [*grant.capabilities, "create_lane"]
        session.add(
            DesktopRun(
                run_id=f"active-{uuid.uuid4().hex[:8]}",
                task_id=seeded["context_id"],
                agent_id=f"main:{seeded['context_id']}",
                kind="main",
                status="running",
                origin="delegated_patrol",
                execution_thread_id=f"thread-{seeded['context_id']}",
                context_revision_id=seeded["revision_id"],
                loop_id=seeded["loop_id"],
                round_id=seeded["round_id"],
            )
        )


def _source_ref(seeded: dict) -> ContextRevisionRef:
    return ContextRevisionRef(
        context_id=seeded["context_id"],
        revision_id=seeded["revision_id"],
        generation=1,
        execution_thread_id=f"thread-{seeded['context_id']}",
        checkpoint_ns="",
        checkpoint_id=f"checkpoint-{seeded['context_id']}",
        payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
    )


def _expansion_observation(seeded: dict) -> LoopObservationEnvelope:
    source = _source_ref(seeded)
    snapshot = seeded["snapshot"]
    return LoopObservationEnvelope.model_validate(
        {
            "loop_id": seeded["loop_id"],
            "loop_revision": snapshot["revision"],
            "round_id": seeded["round_id"],
            "goal_revision": snapshot["goal_revision"],
            "authority_revision": snapshot["authority_revision"],
            "observed_frontier_hash": "a" * 64,
            "mission": {
                "outcome": "Implement and independently verify the change",
                "boundaries": {
                    "in_scope": [],
                    "required_invariants": [],
                    "prohibited_actions": [],
                },
                "completion_checks": [
                    {
                        "check_id": "implementation",
                        "claim": "Implementation is complete",
                        "required": True,
                        "expected_evidence_kinds": ["artifact"],
                    },
                    {
                        "check_id": "verification",
                        "claim": "Focused verification passes",
                        "required": True,
                        "expected_evidence_kinds": ["test"],
                    },
                ],
            },
            "grant": {
                "capabilities": ["continue_context", "create_lane"],
                "context_scope": [seeded["context_id"]],
                "permission_scope": ["read"],
            },
            "portfolio_frontier": [
                {
                    "lane_id": "lane-primary",
                    "context_id": seeded["context_id"],
                    "revision_id": seeded["revision_id"],
                    "revision": source.model_dump(mode="json"),
                    "role": "primary",
                }
            ],
            "workspace": {"revision": 1},
            "budget": {
                "limits": {
                    "max_contexts": 16,
                    "max_lanes": 8,
                    "max_new_lanes_per_round": 3,
                    "max_concurrent_runs": 4,
                    "max_input_tokens": 1000,
                },
                "usage": {
                    "contexts": 1,
                    "lanes": 1,
                    "rounds": 1,
                    "input_tokens": 100,
                    "no_progress_count": 0,
                },
            },
        }
    )


async def _semantic_intent(
    sessions,
    seeded: dict,
    opportunity: ExpansionOpportunity,
) -> PatrolDecisionIntent:
    base = await _create_lane_intent(sessions, seeded, opportunity.workspace_mode)
    return PatrolDecisionIntent.model_validate(
        {
            **base.model_dump(mode="json"),
            "actions": [
                {
                    "action": "spawn_context",
                    "opportunity_id": opportunity.opportunity_id,
                    "source_context_id": opportunity.source.context_id,
                    "purpose": opportunity.purpose,
                    "work_order": opportunity.work_order,
                    "completion_check": opportunity.completion_check,
                    "workspace_mode": opportunity.workspace_mode,
                }
            ],
        }
    )


async def _create_lane_intent(
    sessions,
    seeded: dict,
    workspace_mode: str,
    *,
    expansion_id: str | None = None,
) -> PatrolDecisionIntent:
    source = _source_ref(seeded)
    lane_policy = {
        "workspace_mode": workspace_mode,
        "semantic_fingerprint": "d" * 64,
        "independence_key": f"kernel:{workspace_mode}",
    }
    if expansion_id is not None:
        lane_policy["expansion_id"] = expansion_id
    plan = CreateLanePlan(
        action="create",
        purpose=f"Independent {workspace_mode} verification",
        source_frontier=(source,),
        items=(
            ComposeMessage(
                type="compose_message",
                role="human",
                content="Verify the active source independently.",
                sources=(NamespacedMessageRef(source=source, message_id="source-evidence"),),
            ),
        ),
        lane_policy=lane_policy,
    )
    async with sessions() as session:
        round_row = await session.get(LoopRound, seeded["round_id"])
    snapshot = seeded["snapshot"]
    return PatrolDecisionIntent(
        decision_id=uuid.uuid4().hex,
        idempotency_key=f"create-{workspace_mode}-{uuid.uuid4().hex}",
        loop_id=seeded["loop_id"],
        loop_revision=snapshot["revision"],
        round_id=seeded["round_id"],
        holder_id=snapshot["holder_id"],
        grant_id=snapshot["grant"]["grant_id"],
        grant_revision=snapshot["authority_revision"],
        goal_revision=snapshot["goal_revision"],
        observed_frontier_hash=round_row.frontier_hash,
        observed_workspace_revision=round_row.workspace_revision,
        rationale="Create an additive branch from an immutable source.",
        actions=(
            {
                "action": "create_lane",
                "plan": plan.model_dump(mode="json"),
                "message": "Verify the active source independently.",
            },
        ),
    )
