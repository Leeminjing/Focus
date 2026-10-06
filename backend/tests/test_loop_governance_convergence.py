"""本文件对外提供执行来源、事务后果、模型角色预算与历史审计的数据库回归。

输入为隔离 PostgreSQL、冻结 Observation、真实准入及预算服务；输出为非法来源拒绝、后果屏障、Worker 唯一领取消费与重复回填幂等断言。
具体工作流为建立独立 Loop，经持久实体验证服务边界，再暂停并经 finalizer 清理受理队列，避免污染后续 worker；历史 198 次审计只分类，不推断所属 Round。
示例：pytest backend/tests/test_loop_governance_convergence.py；不访问真实 Vault 或生产数据库。
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import os
import uuid

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.models import DesktopRun, ModelAttemptAudit
from backend.app.desktop.agent_loop.models import AgentLoop, LoopAction, LoopBudgetUsage, LoopRound, LoopObservation, LoopDecision, LoopDirective, LoopPatrolAttempt, LoopWorkerRequest
from backend.app.desktop.agent_loop.accounting_query import LoopAccountingQuery
from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.agent_loop.execution_ownership import RunOwnershipPolicy
from backend.app.desktop.agent_loop.model_usage_owner import OwnedModelUsage
from backend.app.desktop.agent_loop.rounds import settle_round
from backend.app.desktop.agent_loop.round_consequences import RoundConsequenceReader
from backend.app.desktop.context_curation.models import PortfolioRevision
from backend.app.desktop.workspace_coordination.models import WorkspaceAdoption, WorkspaceSlot
from backend.app.desktop.agent_loop.context_expansion.index_model_budget import IndexBudgetExceeded
from backend.app.desktop.agent_loop.usage import LoopUsageDelta
from backend.app.desktop.run_orchestration.admission import RunAdmissionService
from backend.app.desktop.run_orchestration.lineage import validate_parent_chain
from backend.app.desktop.run_orchestration.lifecycle import RunLifecycleFinalizer
from backend.app.desktop.run_orchestration.dispatch import RunDispatchRepository
from backend.app.desktop.run_orchestration.models import RunOutboxEvent
from backend.app.desktop.agent_loop.directive_lifecycle import DirectiveLifecycleRepository
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.run_orchestration.models import RunDispatch
from backend.app.desktop.execution_attempts.tool import ToolExecutionLedger
from backend.app.desktop.models import ToolExecutionAttempt
from test_agent_loop_round_liveness import _seed_loop, _stop
from test_loop_execution_ownership import _seed_launching_directive, _admit_run

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


@asynccontextmanager
async def _loop(tmp_path, label, budgets=None):
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    fixture = None
    try:
        fixture = await _seed_loop(sessions, tmp_path, label=label, started_at=datetime.now(UTC), budgets=budgets)
        yield sessions, fixture
    finally:
        if fixture:
            await _stop(fixture["service"], fixture["loop_id"])
            async with sessions() as session:
                identities = tuple(await session.scalars(select(DesktopRun.run_id).where(DesktopRun.loop_id == fixture["loop_id"])))
            for identity in identities:
                await RunLifecycleFinalizer(sessions, None).abort_prepared(identity, "governance fixture cleanup")
                async with sessions.begin() as session:
                    await RunDispatchRepository().settle_by_run(session, identity)
        await engine.dispose()


async def _freeze(session, fixture):
    observation = LoopObservation(observation_id=uuid.uuid4().hex, loop_id=fixture["loop_id"],
        round_id=fixture["round_id"], envelope={"scope": "governance-test"}, envelope_hash="a" * 64)
    session.add(observation)
    await session.flush()
    current = await session.get(LoopRound, fixture["round_id"])
    current.observation_id = observation.observation_id
    return current


@pytest.mark.parametrize("binding_state", ["delivered", "run_started"])
def test_old_failure_settlement_does_not_terminate_replacement_binding(tmp_path, binding_state):
    async def run():
        async with _loop(tmp_path, "late-binding") as (sessions, fixture):
            directive_id = await _seed_launching_directive(sessions, fixture, label="late-binding")
            old_id = await _admit_run(sessions, fixture, directive_id)
            await RunLifecycleFinalizer(sessions, None).abort_prepared(old_id, "old launch failed")
            replacement_id = await _admit_run(sessions, fixture, directive_id)
            async with sessions.begin() as session:
                round_row = await _freeze(session, fixture)
                directive = await session.get(LoopDirective, directive_id)
                round_row.decision_id = directive.decision_id
                round_row.status = "running"
                await DirectiveLifecycleRepository().transition(session, directive_id, "delivered", run_id=replacement_id)
                if binding_state == "run_started":
                    await DirectiveLifecycleRepository().transition(session, directive_id, "run_started", run_id=replacement_id)
                directive.status = "launched"
            async with sessions.begin() as session:
                event = await session.scalar(select(RunOutboxEvent).where(RunOutboxEvent.run_id == old_id))
                coordinator = LoopCoordinator(sessions)
                await coordinator.handle_run_settled(event, session)
                await coordinator.handle_run_settled(event, session)
            async with sessions() as session:
                directive = await session.get(LoopDirective, directive_id)
                assert directive.lifecycle_state == binding_state and directive.launched_run_id == replacement_id
                assert directive.status == "launched" and directive.queued_reason is None
                assert (await session.get(LoopRound, fixture["round_id"])).status == "running"
                assert await session.scalar(select(func.count()).select_from(LoopJournalEvent).where(
                    LoopJournalEvent.loop_id == fixture["loop_id"], LoopJournalEvent.kind == "context.run.settled",
                    LoopJournalEvent.entity_id == old_id)) == 1
    asyncio.run(run())


def test_parent_chain_rejects_missing_cross_round_cycle_and_unowned_loop_run(tmp_path):
    async def run():
        async with _loop(tmp_path, "lineage") as (sessions, fixture):
            directive_id = await _seed_launching_directive(sessions, fixture, label="lineage")
            parent_id = await _admit_run(sessions, fixture, directive_id)
            async with sessions.begin() as session:
                parent = await session.get(DesktopRun, parent_id)
                child = DesktopRun(run_id=uuid.uuid4().hex, task_id=parent.task_id, agent_id="child",
                    kind="worker", status="pending", loop_id=parent.loop_id, round_id=parent.round_id,
                    parent_run_id="missing", equipment={"permissions": ["read"]})
                with pytest.raises(ValueError, match="不存在"):
                    await validate_parent_chain(session, child)
                child.parent_run_id = parent_id
                child.round_id = "other-round"
                with pytest.raises(ValueError, match="跨 Loop 或 Round"):
                    await validate_parent_chain(session, child)
                child.round_id = parent.round_id
                child.loop_id = "other-loop"
                with pytest.raises(ValueError, match="跨 Loop 或 Round"):
                    await validate_parent_chain(session, child)
                child.loop_id = parent.loop_id
                parent.parent_run_id = parent_id
                with pytest.raises(ValueError, match="环"):
                    await validate_parent_chain(session, child)
                parent.parent_run_id = None
                with pytest.raises(ValueError, match="缺少权威来源"):
                    await RunAdmissionService(RunOwnershipPolicy().admit).admit(session, child)
                parent.equipment = {"permissions": ["read"]}
                with pytest.raises(ValueError, match="控制版本"):
                    await RunOwnershipPolicy().assert_live(session, parent)
    asyncio.run(run())


def test_last_worker_cleanup_and_publication_only_round_advance_once(tmp_path):
    async def run():
        async with _loop(tmp_path, "barrier") as (sessions, fixture):
            worker_id, decision_id = uuid.uuid4().hex, uuid.uuid4().hex
            async with sessions.begin() as session:
                current = await _freeze(session, fixture)
                session.add(LoopDecision(decision_id=decision_id, loop_id=fixture["loop_id"],
                    round_id=current.round_id, holder_id="patrol", intent={}, rationale="Publication only",
                    status="committed", idempotency_key=decision_id))
                session.add(LoopWorkerRequest(worker_request_id=worker_id, loop_id=fixture["loop_id"],
                    round_id=current.round_id, kind="advisor", scope={}, status="running"))
                current.decision_id, current.status = decision_id, "running"
                loop = await session.get(AgentLoop, fixture["loop_id"])
                assert not await settle_round(session, loop, current)
                assert "active_workers" in loop.waiting_reason
            async with sessions.begin() as session:
                (await session.get(LoopWorkerRequest, worker_id)).status = "success"
                current = await session.get(LoopRound, fixture["round_id"])
                loop = await session.get(AgentLoop, fixture["loop_id"], with_for_update=True)
                assert await settle_round(session, loop, current)
                assert await settle_round(session, loop, current)
            coordinator = LoopCoordinator(sessions)
            await coordinator.maintain_rounds()
            await coordinator.maintain_rounds()
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(LoopRound).where(LoopRound.loop_id == fixture["loop_id"])) == 2
                assert (await session.get(LoopBudgetUsage, fixture["loop_id"])).rounds == 1
                assert (await session.get(AgentLoop, fixture["loop_id"])).current_round_id != fixture["round_id"]
                next_id = (await session.get(AgentLoop, fixture["loop_id"])).current_round_id
            from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
            from backend.tests.incremental_index_support import Checkpoints
            from backend.app.desktop.agent_loop.live_projection_projector import LoopLiveSnapshotProjector

            await LoopObservationService(sessions, Checkpoints()).capture(fixture["loop_id"], next_id)
            async with sessions() as session:
                live = await LoopLiveSnapshotProjector().project(session, fixture["loop_id"])
                assert live.round.entity_id == next_id and live.round.state["number"] == 2
                assert live.loop.state["current_round_id"] == next_id
                assert live.accounting.state["accounting"]["completed_rounds"] == 1
    asyncio.run(run())


def test_publication_barrier_preserves_exact_identity_and_failure_never_counts_as_completion(tmp_path):
    async def run():
        async with _loop(tmp_path, "pub-fail") as (sessions, fixture):
            directive_id = await _seed_launching_directive(sessions, fixture, label="pub-fail")
            portfolio_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                current = await _freeze(session, fixture)
                directive = await session.get(LoopDirective, directive_id)
                directive.lifecycle_state = "settled"
                directive.status = "delivered"
                current.decision_id = directive.decision_id
                current.status = "running"
                session.add(PortfolioRevision(portfolio_revision_id=portfolio_id, program_id=loop.program_id,
                    generation=2, source_frontier=[], frontier_hash="b" * 64, base_program_revision=0,
                    control_revisions={}, target_lanes=[], status="preparing"))
                session.add(LoopAction(action_id=uuid.uuid4().hex, decision_id=directive.decision_id, loop_id=loop.loop_id,
                    position=1, action_type="create_lane", payload={}, status="pending",
                    result={"portfolio_revision_id": portfolio_id}))
                await session.flush()
                state = await RoundConsequenceReader().read(session, current)
                assert state.identities["portfolio_publications"] == (portfolio_id,)
                assert not state.stable and not state.failed
                assert not await settle_round(session, loop, current)
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                current = await session.get(LoopRound, fixture["round_id"])
                (await session.get(PortfolioRevision, portfolio_id)).status = "error"
                await session.flush()
                state = await RoundConsequenceReader().read(session, current)
                assert state.failed == ("failed_publications",)
                assert state.identities["failed_publications"] == (portfolio_id,)
                assert not await settle_round(session, loop, current)
                assert current.status == "error" and loop.status == "waiting_user"
                assert current.barrier["consequence_identities"]["failed_publications"] == (portfolio_id,)
                assert (await session.get(LoopBudgetUsage, loop.loop_id)).rounds == 0
    asyncio.run(run())


def test_typed_model_owners_share_budget_and_unknown_receipts_survive_pause(tmp_path):
    async def run():
        async with _loop(tmp_path, "owners", {"max_model_calls": 3}) as (sessions, fixture):
            patrol_id, worker_id = uuid.uuid4().hex, uuid.uuid4().hex
            async with sessions.begin() as session:
                await _freeze(session, fixture)
                session.add(LoopPatrolAttempt(patrol_attempt_id=patrol_id, loop_id=fixture["loop_id"],
                    round_id=fixture["round_id"], attempt=1, execution_thread_id="patrol", checkpoint_ns="patrol",
                    observation_hash="a" * 64, status="running"))
                session.add(LoopWorkerRequest(worker_request_id=worker_id, loop_id=fixture["loop_id"],
                    round_id=fixture["round_id"], kind="completion_verifier", scope={}, status="pending"))
            from backend.app.desktop.agent_loop.workers import LoopWorkerRuntime

            worker = (await LoopWorkerRuntime(sessions, None)._claim_many(1, fixture["loop_id"]))[0]
            receipts = []
            for kind, source in (("patrol", patrol_id), ("worker", worker_id), ("round", fixture["round_id"])):
                receipts.append(await OwnedModelUsage(sessions, fixture["loop_id"], fixture["round_id"], kind, source,
                    retry_identity=worker.retry_identity if kind == "worker" else None).reserve(100, 50))
            with pytest.raises(IndexBudgetExceeded, match="exhausted"):
                await OwnedModelUsage(sessions, fixture["loop_id"], fixture["round_id"], "worker", worker_id, retry_identity=worker.retry_identity).reserve(100, 50)
            async with sessions() as session:
                view = await LoopAccountingQuery().read(session, fixture["loop_id"])
                assert view["consumption"]["occupied"]["model_calls"] == 3
                assert view["consumption"]["actual"]["model_calls"] == 0
                assert view["unknown_model_attempts"] == 0
            await receipts[0].settle(LoopUsageDelta(model_calls=1, input_tokens=25, output_tokens=10))
            await receipts[0].settle(LoopUsageDelta(model_calls=1, input_tokens=25, output_tokens=10))
            await fixture["service"].control(fixture["loop_id"], "pause")
            await receipts[1].mark_unknown()
            await receipts[2].settle(LoopUsageDelta(model_calls=1, input_tokens=30, output_tokens=15))
            with pytest.raises(ValueError, match="来源已取消"):
                await OwnedModelUsage(sessions, fixture["loop_id"], fixture["round_id"], "worker", worker_id).reserve(100, 50)
            async with sessions() as session:
                view = await LoopAccountingQuery().read(session, fixture["loop_id"])
                assert view["consumption"]["actual"] == {"model_calls": 2, "input_tokens": 55, "output_tokens": 25}
                assert view["consumption"]["occupied"]["model_calls"] == 3
                assert view["unknown_model_attempts"] == 1
    asyncio.run(run())


def test_workspace_barrier_reads_only_the_decision_adoption_and_records_conflict(tmp_path):
    async def run():
        async with _loop(tmp_path, "adopt-fail") as (sessions, fixture):
            directive_id = await _seed_launching_directive(sessions, fixture, label="adopt-fail")
            action_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                current = await _freeze(session, fixture)
                directive = await session.get(LoopDirective, directive_id)
                directive.lifecycle_state = "settled"
                directive.status = "delivered"
                current.decision_id = directive.decision_id
                current.status = "running"
                slot = await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.workspace_id == loop.workspace_id,
                    WorkspaceSlot.kind == "authoritative"))
                session.add(LoopAction(action_id=action_id, decision_id=directive.decision_id, loop_id=loop.loop_id,
                    position=1, action_type="adopt_workspace_result", payload={}, status="pending", result={}))
                session.add(WorkspaceAdoption(adoption_id=action_id, source_slot_id=slot.slot_id, target_slot_id=slot.slot_id,
                    source_revision=1, expected_target_revision=1, status="pending"))
                unrelated = uuid.uuid4().hex
                session.add(WorkspaceAdoption(adoption_id=unrelated, source_slot_id=slot.slot_id, target_slot_id=slot.slot_id,
                    source_revision=2, expected_target_revision=1, status="pending"))
                await session.flush()
                state = await RoundConsequenceReader().read(session, current)
                assert state.identities["workspace_adoptions"] == (action_id,)
                assert unrelated not in state.identities["workspace_adoptions"]
                assert not await settle_round(session, loop, current)
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                current = await session.get(LoopRound, fixture["round_id"])
                (await session.get(WorkspaceAdoption, action_id)).status = "conflict"
                await session.flush()
                assert not await settle_round(session, loop, current)
                assert current.status == "error"
                assert current.barrier["consequence_identities"]["failed_adoptions"] == (action_id,)
                assert (await session.get(LoopBudgetUsage, loop.loop_id)).rounds == 0
    asyncio.run(run())


def test_198_completed_historical_audits_are_classified_without_inventing_owners(tmp_path):
    async def run():
        async with _loop(tmp_path, "history") as (sessions, fixture):
            async with sessions.begin() as session:
                baseline = await session.scalar(select(DesktopRun).where(DesktopRun.task_id == fixture["context_id"]))
                owned_id, orphan_id = uuid.uuid4().hex, uuid.uuid4().hex
                session.add_all([DesktopRun(run_id=owned_id, task_id=baseline.task_id, agent_id="main", kind="main",
                    status="interrupted", loop_id=fixture["loop_id"], round_id=fixture["round_id"]),
                    DesktopRun(run_id=orphan_id, task_id=baseline.task_id, agent_id="legacy-swarm", kind="worker", status="success")])
                await session.flush()
                for index in range(198):
                    run_id = baseline.run_id if index == 0 else owned_id if index < 75 else orphan_id
                    session.add(ModelAttemptAudit(attempt_id=uuid.uuid4().hex, run_id=run_id,
                        execution_thread_id="history", checkpoint_ns="", checkpoint_id=str(index), source_manifest={},
                        status="completed", usage={"input_tokens": 10, "output_tokens": 5}))
            async with sessions() as session:
                query = LoopAccountingQuery()
                first = await query.read(session, fixture["loop_id"])
                assert first == await query.read(session, fixture["loop_id"])
                history = first["historical_reconciliation"]
                assert history["task_completed_attempts"] == 198
                assert history["baseline_completed_attempts"] == 1
                assert history["owned_completed_attempts"] == 74
                assert history["unattributed_task_attempts"] == 123
                orphan = await session.get(DesktopRun, orphan_id)
                assert orphan.loop_id is None and orphan.round_id is None and orphan.parent_run_id is None
                assert first["completed_rounds"] == 0
    asyncio.run(run())


def test_pause_rejects_new_tool_intent_and_accepts_late_result(tmp_path):
    async def run():
        async with _loop(tmp_path, "effects") as (sessions, fixture):
            directive_id = await _seed_launching_directive(sessions, fixture, label="effects")
            run_id = await _admit_run(sessions, fixture, directive_id)
            ledger = ToolExecutionLedger(sessions, authority_validator=RunOwnershipPolicy().assert_live)
            call = {"id": "before-pause", "name": "write_file", "args": {"path": "fixture.txt", "content": "authorized"}}
            assert await ledger.claim("effect-before", run_id, call) is None
            await fixture["service"].control(fixture["loop_id"], "pause")
            with pytest.raises(ValueError, match="控制已失效"):
                await ledger.claim("effect-after", run_id, {**call, "id": "after-pause"})
            from langchain_core.messages import ToolMessage

            await ledger.complete("effect-before", ToolMessage(content="started before pause", name="write_file", tool_call_id=call["id"]))
            async with sessions() as session:
                assert await session.get(ToolExecutionAttempt, "effect-after") is None
                assert (await session.get(ToolExecutionAttempt, "effect-before")).status == "completed"
                assert (await session.get(AgentLoop, fixture["loop_id"])).status == "paused"
    asyncio.run(run())
