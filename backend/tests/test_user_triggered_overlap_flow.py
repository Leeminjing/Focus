"""本文件对外提供用户介入重叠轮的 PostgreSQL 生产链验收。

输入为真实 Context/授权、受控 Agent Future 和记忆模型；输出为 R5→R6 多 V→R7 多 V→唯一自动 R8 的持久断言。
工作流调用真实 Observation、Patrol、Kernel、准入、dispatch、启动、取消 outbox、finalizer 和记忆队列；仅外部模型与 Agent 执行受控。
混合动作通过无效取消目标验证整体回滚，发布端口注入失败验证成功授权后的 Mission/取消保留且新执行不启动。
示例：python -m pytest backend/tests/test_user_triggered_overlap_flow.py -q；不调用外部模型或修改正式数据库。
"""

import asyncio
from datetime import UTC, datetime
import os
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from focus.runtime.runs.manager import RunRecord
from focus.runtime.runs.schemas import DisconnectMode, RunStatus
from focus.runtime.stream_bridge.memory import MemoryStreamBridge

from test_agent_loop_round_liveness import _seed_loop
from test_round_task_progress import _Checkpointer
from test_task_progress_validation_recovery import _Model
from test_loop_failure_repair_transaction import NoCheckpoints
from backend.app.desktop.patrol.agent_loop.coordinator import LoopCoordinator, LoopCoordinatorRuntime
from backend.app.desktop.patrol.agent_loop.execution.directive_equipment import resolve_directive_equipment
from backend.app.desktop.patrol.agent_loop.execution.dispatch import LoopRunWorkspaceBinder, LoopWaveDispatcher
from backend.app.desktop.patrol.agent_loop.execution.execution_ownership import RunOwnershipPolicy
from backend.app.desktop.patrol.agent_loop.kernel import LoopKernel
from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopAction, LoopBudgetUsage, LoopContextMembership, LoopDecision, LoopDelegationGrant, LoopDirective, LoopEventOutbox, LoopObservation, LoopRound, LoopUserIntent
from backend.app.desktop.patrol.agent_loop.governance.ownership import KernelFencingRejected
from backend.app.desktop.patrol.agent_loop.observations.observation_capture import LoopObservationService
from backend.app.desktop.patrol.agent_loop.patrol import PortfolioPatrol
from backend.app.desktop.patrol.agent_loop.inputs.patrol_inputs import PatrolInputRequest
from backend.app.desktop.patrol.agent_loop.rounds.lifecycle import advance_settled_round
from backend.app.desktop.patrol.agent_loop.execution.run_execution import LoopRunExecutionBoundary
from backend.app.desktop.patrol.agent_loop.schemas import PatrolDecisionIntent
from backend.app.desktop.patrol.agent_loop.task_progress.models import LoopProgressReceipt, LoopProgressWork
from backend.app.desktop.patrol.agent_loop.task_progress.contracts import canonical_hash
from backend.app.desktop.patrol.agent_loop.task_progress.repository import TaskProgressRepository
from backend.app.desktop.patrol.agent_loop.task_progress.runtime import TaskProgressRuntime
from backend.app.desktop.patrol.agent_loop.workspaces.workspace_patrol import WorkspacePatrolService
from backend.app.desktop.contexts.curation import ComposeMessage, CreateLanePlan
from backend.app.desktop.contexts.curation.contracts import MissionEvidenceRef
from backend.app.desktop.contexts.evolution import ContextRevisionContract, ContextRevisionNotFound, ContextRevisionOriginKind, ContextRevisionPayloadMode, ContextRevisionProjectionStatus, ContextRevisionRef, ContextRevisionRepository
from backend.app.desktop.runs.evidence.repository import DomainResultRepository
from backend.app.desktop.persistence.models import DesktopRun, DesktopThread
from backend.app.desktop.runs.orchestration.admission import RunAdmissionService
from backend.app.desktop.runs.orchestration.assembler import RunExecutionAssembly
from backend.app.desktop.runs.orchestration.dispatch import DurableRunDispatchWorker
from backend.app.desktop.runs.orchestration.lifecycle import RunLifecycleFinalizer
from backend.app.desktop.runs.orchestration.outbox import RunOutboxConsumer

pytestmark = pytest.mark.usefixtures("runtime_postgres_database")


class ControlledExecution:
    def __init__(self, sessions, loop_id):
        self.sessions, self.loop_id = sessions, loop_id
        self.records, self.cancelled = {}, []

    async def admit(self, directive, message, slot_id):
        async with self.sessions.begin() as session:
            loop = await session.get(AgentLoop, self.loop_id)
            context = await session.get(DesktopThread, directive.target_context_id)
            run = DesktopRun(run_id=uuid.uuid4().hex, task_id=context.task_id, agent_id="lead_agent",
                kind="main", status="pending", origin="delegated_patrol", execution_thread_id=context.thread_id,
                context_revision_id=directive.target_context_revision_id, directive_id=directive.directive_id,
                action_id=directive.action_id, loop_id=self.loop_id, round_id=directive.round_id,
                equipment=await resolve_directive_equipment(session, loop, directive),
                workspace_anchor={"slot_id": slot_id}, input_messages=[{"role": "human", "id": message.id, "content": message.content}])
            await RunAdmissionService(RunOwnershipPolicy().admit).admit(session, run)
            return run.run_id

    async def assemble(self, run_id):
        async with self.sessions() as session:
            run = await session.get(DesktopRun, run_id)
        body = SimpleNamespace(context={})
        await LoopRunWorkspaceBinder(self.sessions).bind(run_id=run_id, loop_id=self.loop_id,
            body=body, slot_id=run.workspace_anchor["slot_id"])
        return RunExecutionAssembly(run_id=run_id, body=body, thread_id=run.execution_thread_id, agent_factory=lambda: None)

    async def start(self, assembly):
        record = RunRecord(run_id=assembly.run_id, thread_id=assembly.thread_id,
            status=RunStatus.running, on_disconnect=DisconnectMode.continue_, task=asyncio.get_running_loop().create_future())
        self.records[record.run_id] = record

        async def execute(*args):
            return record
        resources = SimpleNamespace(bridge=MemoryStreamBridge(), run_manager=self,
            checkpointer=NoCheckpoints(), store=None, app_config=None)
        return await LoopRunExecutionBoundary(self.sessions).start(assembly, resources, execute)

    def cancel(self, run_id, *, action):
        self.cancelled.append(run_id)
        record = self.records[run_id]
        record.status, record.error = RunStatus.interrupted, action

    async def finish(self, run_id, coordinator, *, late_result=False):
        record = self.records[run_id]
        if record.status == RunStatus.running:
            record.status = RunStatus.success
        record.task.set_result(None)
        await record.loop_activity_task
        await RunLifecycleFinalizer(self.sessions, NoCheckpoints()).finalize(record)
        source = None
        if late_result:
            async with self.sessions.begin() as session:
                run = await session.get(DesktopRun, run_id)
                source = await DomainResultRepository().record(session, kind="test", source_id=uuid.uuid4().hex,
                    loop_id=self.loop_id, context_id=run.task_id, run_id=run_id,
                    payload={"status": "verified", "summary": "R5 late valid result"})
        await RunOutboxConsumer(self.sessions).drain("overlap-flow", coordinator.handle_run_settled, loop_id=self.loop_id)
        return source


async def _setup_flow(sessions, tmp_path):
    fixture = await _seed_loop(sessions, tmp_path, label="overlap-flow", started_at=datetime.now(UTC))
    contexts = [fixture["context_id"]]
    async with sessions.begin() as session:
        loop = await session.get(AgentLoop, fixture["loop_id"])
        loop.interaction_mode, loop.equipment = "workspace_patrol", {**loop.equipment, "permissions": ["read"], "access_mode": "read-only"}
        (await session.get(LoopRound, fixture["round_id"])).number = 5
        for label in ("B", "C", "D"):
            context_id = uuid.uuid4().hex
            contexts.append(context_id)
            session.add(DesktopThread(task_id=context_id, workspace_id=loop.workspace_id, thread_id=f"thread-{context_id}", title=label))
            await session.flush()
            ref = ContextRevisionRef(context_id=context_id, revision_id=uuid.uuid4().hex, generation=1,
                execution_thread_id=f"thread-{context_id}", checkpoint_ns="", checkpoint_id=f"checkpoint-{context_id}",
                payload_mode=ContextRevisionPayloadMode.CHECKPOINT)
            repo = ContextRevisionRepository()
            await repo.insert(session, ContextRevisionContract(ref=ref, content_hash="a" * 64,
                projection_status=ContextRevisionProjectionStatus.VALID, origin_kind=ContextRevisionOriginKind.ROOT, created_at=datetime.now(UTC)))
            await repo.switch_current(session, ref, None)
            session.add(LoopContextMembership(membership_id=uuid.uuid4().hex, loop_id=loop.loop_id,
                context_id=context_id, role="worker"))
        grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop.loop_id))
        grant.capabilities = [*grant.capabilities, "apply_user_inputs", "cancel_execution"]
        grant.permission_scope, grant.context_scope = ["read"], contexts
        workspace_id = loop.workspace_id
    intake = WorkspacePatrolService(sessions, SimpleNamespace(checkpointer=_Checkpointer(),
        app_config=SimpleNamespace(resolve_default_model_name=lambda: "test")))
    return fixture, contexts, workspace_id, intake


async def _proposal(sessions, envelope, claim, execution_contexts=(), cancel=None, revise_mission=False):
    async with sessions() as session:
        loop = await session.get(AgentLoop, envelope.loop_id)
    actions = []
    if envelope.user_intents:
        actions.append({"action": "apply_user_inputs", "inputs": [{"intent_id": item["intent_id"],
            "disposition": "discussion", "explanation": "已考虑此输入的完整因果时点"} for item in envelope.user_intents]})
    if revise_mission:
        source = next(item for item in envelope.user_intents if item["input_type"] == "boundary")
        actions[0]["changes"] = [{"intent_id": source["intent_id"], "section": "boundary",
            "quote": source["content"], "operation": "supplement", "boundary_group": "required_invariants"}]
        for item in actions[0]["inputs"]:
            if item["intent_id"] == source["intent_id"]:
                item["disposition"] = "decision"
    if cancel:
        actions.append({"action": "cancel_execution", "target_kind": "run", "target_id": cancel[0], "round_id": cancel[1], "reason": "用户修正方向"})
    actions += [{"action": "continue_context", "context_id": item["context_id"],
        "context_revision_id": item["revision_id"], "message": "执行已授权工作"}
        for item in envelope.portfolio_frontier if item["context_id"] in execution_contexts]
    return PatrolDecisionIntent(decision_id=uuid.uuid4().hex, idempotency_key=uuid.uuid4().hex,
        loop_id=loop.loop_id, loop_revision=envelope.loop_revision, round_id=envelope.round_id,
        observation_id=envelope.observation_id, observation_generation=envelope.generation,
        holder_id=loop.holder_id, grant_id=envelope.grant["grant_id"], grant_revision=envelope.authority_revision,
        goal_revision=envelope.goal_revision, fencing_token=claim.fencing_token,
        observed_frontier_hash=envelope.observed_frontier_hash, observed_workspace_revision=envelope.workspace["revision"],
        observed_projection_sequence=envelope.projection_sequence, base_entity_revisions=envelope.base_entity_revisions,
        context_permissions=[{"context_id": item["context_id"], "access_mode": "read-only"} for item in envelope.portfolio_frontier],
        rationale="基于当前版本与完整输入链提交", actions=actions)


async def _commit_and_launch(sessions, loop_id, coordinator, execution, contexts=(), cancel=None, *, revise_mission=False):
    claim = await coordinator.claim_for_loop(loop_id, "overlap-flow")
    assert claim is not None
    envelope = await LoopObservationService(sessions, _Checkpointer()).capture(loop_id, claim.round_id)

    async def model(frozen):
        return await _proposal(sessions, frozen, claim, contexts, cancel, revise_mission)
    intent = await PortfolioPatrol(sessions, model).decide(envelope, envelope.grant["holder_id"])
    result = await LoopKernel(sessions, require_fencing=True).commit(intent)
    assert result.status == "committed", result.reason
    launched = await coordinator.dispatch_ready(claim, LoopWaveDispatcher(sessions, execution.admit), 8) if contexts else ()
    await coordinator.release(claim)
    if contexts:
        assert len(launched) == len(contexts)
        assert await DurableRunDispatchWorker(sessions, "overlap-flow", execution, execution.start).drain(limit=8) == len(contexts)
    async with sessions() as session:
        by_context = {row.task_id: row.run_id for row in await session.scalars(select(DesktopRun).where(DesktopRun.run_id.in_(launched)))}
    return envelope, by_context


async def _advance(sessions, loop_id):
    async with sessions.begin() as session:
        loop = await session.get(AgentLoop, loop_id, with_for_update=True)
        row = await session.get(LoopRound, loop.current_round_id)
        successor = await advance_settled_round(session, loop, row)
        return successor.round_id if successor else None


def test_input_replay_survives_disappeared_current_checkpoint(tmp_path, monkeypatch):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture, _, workspace, intake = await _setup_flow(sessions, tmp_path)
            request = PatrolInputRequest(submission_id=uuid.uuid4().hex, content="已耐久受理的原输入")
            receipt = await intake.submit(workspace, request)
            reads = []

            async def missing_checkpoint(self, config):
                reads.append(config)
                return None

            monkeypatch.setattr(_Checkpointer, "aget_tuple", missing_checkpoint)
            with pytest.raises(ContextRevisionNotFound):
                await intake._observations.prepare_views(fixture["loop_id"])
            assert await intake.submit(workspace, request) == receipt
            with pytest.raises(HTTPException) as conflict:
                await intake.submit(workspace, request.model_copy(update={"content": "同身份篡改原文"}))
            assert conflict.value.status_code == 409 and len(reads) == 1
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(LoopObservation).where(LoopObservation.loop_id == fixture["loop_id"])) == 1
                assert (await session.get(LoopRound, receipt["round_id"])).observation_id == receipt["observation_id"]
                assert await session.scalar(select(func.count()).select_from(LoopUserIntent).where(LoopUserIntent.loop_id == fixture["loop_id"])) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_full_overlap_chain_joins_only_after_cancellation_and_late_progress(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        execution = None
        try:
            fixture, contexts, workspace, intake = await _setup_flow(sessions, tmp_path)
            loop_id, coordinator = fixture["loop_id"], LoopCoordinator(sessions)
            execution = ControlledExecution(sessions, loop_id)
            r5, old_runs = await _commit_and_launch(sessions, loop_id, coordinator, execution, contexts[:3])
            received = [await intake.submit(workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex,
                input_type="boundary", content="保留 A/C 的工作，取消 B；D 只读取文件核对新方向。"))]
            received += await asyncio.gather(*(intake.submit(workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex, content=value)) for value in ("U2", "U3")))
            assert len({item["round_id"] for item in received}) == 1
            assert sorted(item["introduced_generation"] for item in received) == [1, 2, 3]
            r6, new_runs = await _commit_and_launch(sessions, loop_id, coordinator, execution, [contexts[3]],
                (old_runs[contexts[1]], r5.round_id), revise_mission=True)
            assert r6.generation == 3 and len(r6.causal_chain) == 3
            later = [await intake.submit(workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex, content="U4"))]
            later += await asyncio.gather(*(intake.submit(workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex, content=value)) for value in ("U5", "U6")))
            assert later[0]["round_id"] != r6.round_id and len({item["round_id"] for item in later}) == 1
            r7, _ = await _commit_and_launch(sessions, loop_id, coordinator, execution)
            async with sessions() as session:
                assert (await session.get(LoopRound, r5.round_id)).goal_revision == 1
                assert (await session.get(LoopRound, r6.round_id)).goal_revision == 2
                assert (await session.get(DesktopRun, new_runs[contexts[3]])).equipment["_loop_goal_revision"] == 2
                for context in (contexts[0], contexts[2]):
                    retained = await session.get(DesktopRun, old_runs[context])
                    assert retained.status in {"pending", "running"}, (retained.status, retained.error, execution.cancelled)
                    await RunOwnershipPolicy().assert_live(session, retained)
                cancelled = await session.get(DesktopRun, old_runs[contexts[1]])
                assert cancelled.status == "interrupted" and cancelled.settled_at is None
            restarted = LoopCoordinatorRuntime(LoopCoordinator(sessions), RunOutboxConsumer(sessions), run_manager=execution)
            await restarted._drain_preemptions()
            assert execution.cancelled == [old_runs[contexts[1]]]
            assert await _advance(sessions, loop_id) is None
            async with sessions() as session:
                usage = await session.get(LoopBudgetUsage, loop_id)
                before_memory = (usage.model_calls, usage.input_tokens, usage.output_tokens)
            memory = TaskProgressRuntime(sessions, None, model_factory=lambda _: _Model())
            assert [await memory.drain(loop_id=loop_id) for _ in range(4)] == [1, 1, 1, 0]
            await execution.finish(new_runs[contexts[3]], coordinator)
            await execution.finish(old_runs[contexts[1]], coordinator)
            assert await _advance(sessions, loop_id) is None
            late = await execution.finish(old_runs[contexts[0]], coordinator, late_result=True)
            await execution.finish(old_runs[contexts[2]], coordinator)
            assert await _advance(sessions, loop_id) is None
            async with sessions() as session:
                assert await session.get(LoopProgressReceipt, (loop_id, late)) is None
                assert await session.scalar(select(func.count()).select_from(LoopRound).where(LoopRound.loop_id == loop_id)) == 3
            assert await TaskProgressRuntime(sessions, None, model_factory=lambda _: _Model()).drain(loop_id=loop_id) == 1
            advanced = await asyncio.gather(_advance(sessions, loop_id), _advance(sessions, loop_id))
            assert sum(item is not None for item in advanced) == 1
            successor = next(item for item in advanced if item)
            r8 = await LoopObservationService(sessions, _Checkpointer()).capture(loop_id, successor)
            async with sessions() as session:
                assert (await session.get(LoopRound, successor)).number == 8
                assert await session.get(LoopProgressReceipt, (loop_id, late)) is not None
                head = await TaskProgressRepository().current(session, loop_id)
                assert r8.previous_task_progress == head.document
                assert all(row.authority_committed_at is not None for row in await session.scalars(select(LoopDecision).where(LoopDecision.loop_id == loop_id)))
                assert len(r7.causal_chain) == 3
                for receipt in (*received, *later):
                    source = await session.get(LoopUserIntent, receipt["intent_id"])
                    origin = await session.get(LoopObservation, receipt["observation_id"])
                    assert source.introduced_observation_id == origin.observation_id
                    assert source.snapshot_hash == canonical_hash(origin.envelope["pre_input_state"]) == receipt["snapshot_hash"]
                works = list(await session.scalars(select(LoopProgressWork).where(LoopProgressWork.loop_id == loop_id)))
                assert len(works) == 4 and all(work.state == "published" for work in works)
                entries = [entry for work in works for entry in work.usage]
                assert entries and all(entry["accounted"] and entry["settlement"] == "actual" for entry in entries)
                usage = await session.get(LoopBudgetUsage, loop_id)
                assert tuple(getattr(usage, field) - before_memory[index] for index, field in enumerate(("model_calls", "input_tokens", "output_tokens"))) == tuple(
                    sum(entry[field] for entry in entries) for field in ("model_calls", "input_tokens", "output_tokens"))
        finally:
            if execution:
                for record in execution.records.values():
                    if not record.task.done():
                        record.task.set_result(None)
                await asyncio.gather(*(record.loop_activity_task for record in execution.records.values()), return_exceptions=True)
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("submission_first", [False, True])
def test_input_and_authority_commit_race_keeps_one_current_observation(tmp_path, submission_first):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture, _, workspace, intake = await _setup_flow(sessions, tmp_path)
            loop_id, coordinator = fixture["loop_id"], LoopCoordinator(sessions)
            first = await intake.submit(workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex, content="U1"))
            claim = await coordinator.claim_for_loop(loop_id, "commit-race")
            observation = await LoopObservationService(sessions, _Checkpointer()).capture(loop_id, claim.round_id)

            async def model(frozen):
                return await _proposal(sessions, frozen, claim)

            intent = await PortfolioPatrol(sessions, model).decide(observation, observation.grant["holder_id"])
            request = PatrolInputRequest(submission_id=uuid.uuid4().hex, content="U2")
            submit = intake.submit(workspace, request)
            commit = LoopKernel(sessions, require_fencing=True).commit(intent)
            results = await asyncio.gather(*([submit, commit] if submission_first else [commit, submit]), return_exceptions=True)
            receipt, result = results if submission_first else reversed(results)
            assert not isinstance(receipt, Exception), receipt
            assert not isinstance(result, Exception) or isinstance(result, KernelFencingRejected), result
            replay = await intake.submit(workspace, request)
            assert (replay["intent_id"], replay["observation_id"]) == (receipt["intent_id"], receipt["observation_id"])
            async with sessions() as session:
                loop = await session.get(AgentLoop, loop_id)
                original = await session.get(LoopRound, first["round_id"])
                current = await session.get(LoopRound, loop.current_round_id)
                frozen = await session.get(LoopObservation, current.observation_id)
                pending = list(await session.scalars(select(LoopRound).where(LoopRound.loop_id == loop_id,
                    LoopRound.decision_id.is_(None), LoopRound.status.in_(["observed", "curated", "waiting_workers"]))))
                assert [row.round_id for row in pending] == [current.round_id]
                assert current.observation_id == receipt["observation_id"]
                assert await session.scalar(select(func.count()).select_from(LoopObservation).where(LoopObservation.loop_id == loop_id)) == 2
                if original.decision_id is not None:
                    assert result.status == "committed"
                    assert (await session.get(LoopDecision, original.decision_id)).authority_committed_at is not None
                    assert current.round_id != original.round_id and frozen.generation == 1
                    assert len(frozen.envelope["causal_chain"]) == 1
                    assert await session.scalar(select(func.count()).select_from(LoopProgressWork).where(LoopProgressWork.loop_id == loop_id)) == 1
                else:
                    assert isinstance(result, KernelFencingRejected) or result.status == "superseded"
                    assert current.round_id == original.round_id and frozen.generation == 2
                    assert len(frozen.envelope["causal_chain"]) == 2
                    assert await session.scalar(select(func.count()).select_from(LoopProgressWork).where(LoopProgressWork.loop_id == loop_id)) == 0
            await coordinator.release(claim)
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("complete", [False, True])
def test_input_dispositions_require_complete_set_and_deferred_preserves_origin(tmp_path, complete):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture, _, workspace, intake = await _setup_flow(sessions, tmp_path)
            loop_id, coordinator = fixture["loop_id"], LoopCoordinator(sessions)
            first = await intake.submit(workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex, content="稍后明确此需求"))
            second = await intake.submit(workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex, content="讨论当前问题"))
            claim = await coordinator.claim_for_loop(loop_id, "input-dispositions")
            observation = await LoopObservationService(sessions, _Checkpointer()).capture(loop_id, claim.round_id)

            async def model(frozen):
                intent = await _proposal(sessions, frozen, claim)
                action = intent.actions[0]
                uses = tuple(item.model_copy(update={"disposition": "deferred"}) if item.intent_id == first["intent_id"] else item
                    for item in action.inputs if complete or item.intent_id == second["intent_id"])
                return intent.model_copy(update={"actions": (action.model_copy(update={"inputs": uses}),)})

            intent = await PortfolioPatrol(sessions, model).decide(observation, observation.grant["holder_id"])
            result = await LoopKernel(sessions, require_fencing=True).commit(intent)
            await coordinator.release(claim)
            async with sessions() as session:
                original = await session.get(LoopRound, first["round_id"])
                source = await session.get(LoopUserIntent, first["intent_id"])
                other = await session.get(LoopUserIntent, second["intent_id"])
                if not complete:
                    assert result.status == "rejected" and original.decision_id is None
                    assert source.status == other.status == "observed"
                    assert await session.scalar(select(func.count()).select_from(LoopProgressWork).where(LoopProgressWork.loop_id == loop_id)) == 0
                    return
                assert result.status == "committed" and source.status == "pending" and other.status == "addressed"
                assert source.introduced_observation_id == first["observation_id"]
            third = await intake.submit(workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex, content="现在结合原需求继续"))
            assert third["round_id"] != first["round_id"]
            next_observation, _ = await _commit_and_launch(sessions, loop_id, coordinator, ControlledExecution(sessions, loop_id))
            assert {row["intent_id"] for row in next_observation.causal_chain} == {first["intent_id"], third["intent_id"]}
            async with sessions() as session:
                source = await session.get(LoopUserIntent, first["intent_id"])
                assert source.status == "addressed" and source.observed_round_id == third["round_id"]
                assert (source.introduced_observation_id, source.introduced_generation, source.snapshot_hash) == (
                    first["observation_id"], first["introduced_generation"], first["snapshot_hash"])
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["invalid_action", "publication"])
def test_mixed_mission_cancel_and_execution_failure_preserves_commit_boundary(tmp_path, failure):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        execution = None
        try:
            fixture, contexts, workspace, intake = await _setup_flow(sessions, tmp_path)
            loop_id, coordinator = fixture["loop_id"], LoopCoordinator(sessions)
            async with sessions.begin() as session:
                grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop_id))
                grant.capabilities = [*grant.capabilities, "create_lane"]
            execution = ControlledExecution(sessions, loop_id)
            r5, runs = await _commit_and_launch(sessions, loop_id, coordinator, execution, contexts[:1])
            target = runs[contexts[0]]
            receipt = await intake.submit(workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex,
                input_type="boundary", content="取消旧检查，新增核对只读取文件。"))
            claim = await coordinator.claim_for_loop(loop_id, "mixed-failure")
            observation = await LoopObservationService(sessions, _Checkpointer()).capture(loop_id, claim.round_id)

            async def model(frozen):
                intent = await _proposal(sessions, frozen, claim, [contexts[3]], (target, r5.round_id), True)
                data = intent.model_dump(mode="json")
                if failure == "invalid_action":
                    data["actions"].insert(2, {"action": "cancel_execution", "target_kind": "run",
                        "target_id": uuid.uuid4().hex, "round_id": r5.round_id, "reason": "不存在的取消目标"})
                else:
                    source = ContextRevisionRef.model_validate(frozen.portfolio_frontier[0]["revision"])
                    evidence = MissionEvidenceRef(identity_version="mission-section-v2", loop_id=loop_id,
                        goal_revision=frozen.goal_revision, section_kind="outcome", item_id="outcome",
                        content_hash=frozen.mission["section_hashes"]["outcome"])
                    plan = CreateLanePlan(action="create", purpose="独立只读核验", source_frontier=(source,),
                        evidence_frontier=(evidence,), items=(ComposeMessage(type="compose_message", role="human",
                            content=frozen.mission["outcome"], sources=(evidence,)),), lane_policy={"workspace_mode": "read_only"})
                    data["context_permissions"].append({"context_id": f"action:{len(data['actions'])}", "access_mode": "read-only"})
                    data["actions"].append({"action": "create_lane", "plan": plan.model_dump(mode="json"), "message": "执行独立核验"})
                return PatrolDecisionIntent.model_validate(data)

            class FailedPublication:
                calls = 0

                async def publish(self, decision_id):
                    self.calls += 1
                    raise RuntimeError("授权后派生发布失败")

            publication = FailedPublication()
            intent = await PortfolioPatrol(sessions, model).decide(observation, observation.grant["holder_id"])
            kernel = LoopKernel(sessions, portfolio_publication=publication, require_fencing=True)
            result = await kernel.commit(intent)
            assert result.status == "rejected", result.reason
            assert (await kernel.commit(intent)).decision_id == result.decision_id
            async with sessions() as session:
                loop = await session.get(AgentLoop, loop_id)
                round_row = await session.get(LoopRound, observation.round_id)
                decision = await session.get(LoopDecision, result.decision_id)
                old_run = await session.get(DesktopRun, target)
                user_input = await session.get(LoopUserIntent, receipt["intent_id"])
                cancellations = await session.scalar(select(func.count()).select_from(LoopEventOutbox).where(
                    LoopEventOutbox.loop_id == loop_id, LoopEventOutbox.event_type == "loop.execution.cancel_requested"))
                assert await session.scalar(select(func.count()).select_from(DesktopRun).where(DesktopRun.loop_id == loop_id,
                    DesktopRun.round_id == observation.round_id)) == 0
                assert await session.scalar(select(func.count()).select_from(LoopDirective).where(LoopDirective.decision_id == decision.decision_id,
                    LoopDirective.lifecycle_state.in_(["authorized", "delivering", "delivered", "run_started"]))) == 0
                if failure == "invalid_action":
                    assert publication.calls == 0 and loop.goal_revision == round_row.goal_revision == 1
                    assert old_run.status in {"pending", "running"} and cancellations == 0
                    assert round_row.decision_id is None and decision.authority_committed_at is None
                    assert user_input.status == "observed"
                else:
                    assert publication.calls == 1 and loop.goal_revision == round_row.goal_revision == 2
                    assert old_run.status == "interrupted" and old_run.settled_at is None and cancellations == 1
                    assert round_row.decision_id == decision.decision_id and decision.authority_committed_at is not None
                    assert user_input.status == "addressed"
                    actions = list(await session.scalars(select(LoopAction).where(LoopAction.decision_id == decision.decision_id)))
                    assert {row.action_type for row in actions if row.status == "applied"} == {"apply_user_inputs", "cancel_execution"}
            if failure == "publication":
                retry = intent.model_copy(update={"decision_id": uuid.uuid4().hex, "idempotency_key": uuid.uuid4().hex})
                assert (await LoopKernel(sessions).commit(retry)).status == "superseded"
                async with sessions() as session:
                    assert await session.scalar(select(func.count()).select_from(LoopDecision).where(LoopDecision.round_id == observation.round_id,
                        LoopDecision.authority_committed_at.is_not(None))) == 1
            await coordinator.release(claim)
        finally:
            if execution:
                for record in execution.records.values():
                    if not record.task.done():
                        record.task.set_result(None)
                await asyncio.gather(*(record.loop_activity_task for record in execution.records.values()), return_exceptions=True)
            await engine.dispose()
    asyncio.run(run())
