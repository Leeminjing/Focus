r"""本文件对外提供 Loop Run 单一 durable 启动者的竞争与授权回归测试。

输入为真实 PostgreSQL 中的已授权 Directive、持久 Run 计划和两个并发 worker；输出为一次启动、正确生命周期，以及撤销授权后的启动拒绝。
直接用户消息夹具先经真实受理、冻结和 Patrol/Kernel 决策，启动前仍为 observed，成功启动后才绑定 Run。
调度恢复在独立运行时数据库验证执行前安全重排保留计划，执行后恢复必须中断，不用新消息用例替代该原有合同。
具体工作流为模拟通用 worker 竞争同一调度行，使用真实 fencing 与 Loop 启动边界，并在 Run 结算前查询持久状态。示例：`pytest backend/tests/test_loop_execution_ownership.py -q`。
竞争启动用例使用 runtime 隔离库，避免其它测试的真实 accepted 调度行被第二个全局 worker 领取。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from focus.runtime.stream_bridge.memory import MemoryStreamBridge
from focus.runtime.runs.manager import RunManager
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop import service as desktop_service_module
from backend.app.desktop.agent_loop.models import LoopAction, LoopDecision, LoopDelegationGrant, LoopDirective
from backend.app.desktop.agent_loop.models import LoopUserIntent
from backend.app.desktop.agent_loop.intervention_lifecycle import InterventionLifecycleRepository
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.agent_loop.dispatch import LoopRunWorkspaceBinder
from backend.app.desktop.agent_loop.run_execution import LoopRunExecutionBoundary
from backend.app.desktop.models import DesktopRun, DesktopThread
from backend.app.desktop.agent_loop.execution_ownership import RunOwnershipPolicy
from backend.app.desktop.run_orchestration.admission import RunAdmissionService
from backend.app.desktop.run_orchestration.assembler import RunExecutionAssembly
from backend.app.desktop.run_orchestration.dispatch import DurableRunDispatchWorker, RunDispatchRecovery, RunDispatchRepository
from backend.app.desktop.run_orchestration.lifecycle import RunLifecycleFinalizer
from backend.app.desktop.run_orchestration.executor import RunExecutionResources
from backend.app.desktop.service import DesktopService, PreparedRun
from backend.app.desktop.workspace_coordination.leases import WorkspaceLeaseManager
from backend.app.desktop.workspace_coordination.models import WorkspaceLease, WorkspaceSlot
from backend.app.desktop.workspace_coordination.schemas import WorkspaceAccessMode, WorkspaceLeaseRequest

from test_agent_loop_round_liveness import _seed_loop, _stop


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


async def _seed_launching_directive(sessions, fixture: dict, *, label: str) -> str:
    from backend.app.desktop.agent_loop.models import LoopRound
    directive_id = uuid.uuid4().hex
    decision_id = uuid.uuid4().hex
    action_id = uuid.uuid4().hex
    async with sessions.begin() as session:
        session.add(
            LoopDecision(
                decision_id=decision_id,
                loop_id=fixture["loop_id"],
                round_id=fixture["round_id"],
                holder_id=fixture["snapshot"]["holder_id"],
                intent={"context_permissions": [{"context_id": fixture["context_id"], "access_mode": "read-only"}]},
                authority_committed_at=datetime.now(UTC),
                rationale=f"{label} launch decision",
                status="committed",
                idempotency_key=f"decision-{directive_id}",
            )
        )
        await session.flush()
        session.add(
            LoopAction(
                action_id=action_id,
                decision_id=decision_id,
                loop_id=fixture["loop_id"],
                position=0,
                action_type="continue_context",
                payload={},
                status="committed",
            )
        )
        await session.flush()
        session.add(
            LoopDirective(
                directive_id=directive_id,
                loop_id=fixture["loop_id"],
                round_id=fixture["round_id"],
                decision_id=decision_id,
                action_id=action_id,
                target_context_id=fixture["context_id"],
                target_context_revision_id=fixture["revision_id"],
                message_id=f"message-{directive_id}",
                content="Run the focused launch checks.",
                content_hash="c" * 64,
                actor_kind="patrol",
                actor_id=fixture["snapshot"]["holder_id"],
                grant_id=fixture["snapshot"]["grant"]["grant_id"],
                grant_revision=1,
                goal_revision=1,
                status="launching",
                lifecycle_state="delivering",
                revision=3,
                origin_kind="patrol",
                correlation_id=f"correlation-{directive_id}",
                attempt=1,
                max_attempts=3,
                idempotency_key=f"directive-{directive_id}",
            )
        )
        round_row = await session.get(LoopRound, fixture["round_id"])
        round_row.decision_id, round_row.status = decision_id, "ready"
    return directive_id


async def _admit_run(sessions, fixture: dict, directive_id: str, *, slot_id: str | None = None) -> str:
    run_id = uuid.uuid4().hex
    async with sessions.begin() as session:
        directive = await session.get(LoopDirective, directive_id)
        task = await session.get(DesktopThread, fixture["context_id"])
        slot = await session.scalar(select(WorkspaceSlot).where(
            WorkspaceSlot.workspace_id == task.workspace_id,
            WorkspaceSlot.kind == "authoritative",
        ))
        assert slot is not None
        from backend.app.desktop.agent_loop.directive_equipment import resolve_directive_equipment
        from backend.app.desktop.agent_loop.models import AgentLoop

        loop = await session.get(AgentLoop, fixture["loop_id"])
        equipment = await resolve_directive_equipment(session, loop, directive)
        admission = await RunAdmissionService(RunOwnershipPolicy().admit).admit(session, DesktopRun(
            run_id=run_id,
            task_id=fixture["context_id"],
            agent_id=f"main:{fixture['context_id']}",
            kind="main",
            status="pending",
            origin="delegated_patrol",
            execution_thread_id=f"thread-{fixture['context_id']}",
            origin_message_id=directive.message_id,
            context_revision_id=fixture["revision_id"],
            directive_id=directive_id,
            loop_id=fixture["loop_id"],
            round_id=fixture["round_id"],
            equipment={**equipment, "_durable_dispatch_execution": {"agent_role": "main", "base_prompt": "test"}},
            workspace_anchor={"slot_id": slot_id or slot.slot_id},
        ))
        admission.dispatch.accepted_at = datetime(2000, 1, 1, tzinfo=UTC)
    return run_id


def test_interrupted_unsettled_main_keeps_context_and_capacity_with_another_slot(tmp_path: Path) -> None:
    from backend.app.desktop.agent_loop.context_run_pool import ContextRunPool
    from backend.app.desktop.run_orchestration.admission import RunAdmissionConflict

    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="cleanup-slot", started_at=datetime.now(UTC))
        try:
            directive_id = await _seed_launching_directive(sessions, fixture, label="cleanup-slot")
            old_id = await _admit_run(sessions, fixture, directive_id)
            new_slot_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                old = await session.get(DesktopRun, old_id)
                old.status = "interrupted"
                original = await session.get(WorkspaceSlot, old.workspace_anchor["slot_id"])
                session.add(WorkspaceSlot(slot_id=new_slot_id, workspace_id=original.workspace_id,
                    kind="isolated", root_path=str(tmp_path / "other-slot"), current_fingerprint="b" * 64))
            pool = ContextRunPool(sessions, LoopCoordinator(sessions), None)
            assert await pool._active_run_count(fixture["loop_id"]) == 1
            with pytest.raises(RunAdmissionConflict, match="活跃 Main"):
                await _admit_run(sessions, fixture, directive_id, slot_id=new_slot_id)
            async with sessions.begin() as session:
                (await session.get(DesktopRun, old_id)).settled_at = datetime.now(UTC)
                await RunDispatchRepository().settle_by_run(session, old_id)
            assert await pool._active_run_count(fixture["loop_id"]) == 0
            replacement_id = await _admit_run(sessions, fixture, directive_id, slot_id=new_slot_id)
            await RunLifecycleFinalizer(sessions, None).abort_prepared(replacement_id, "fixture complete")
            async with sessions.begin() as session:
                await RunDispatchRepository().settle_by_run(session, replacement_id)
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_competing_durable_workers_start_one_loop_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime_postgres_database) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="one-owner", started_at=datetime.now(UTC))
            directive_id = await _seed_launching_directive(sessions, fixture, label="one-owner")
            run_id = await _admit_run(sessions, fixture, directive_id)
            done = asyncio.get_running_loop().create_future()
            started = []

            async def execute(body, thread_id, resources, factory):
                started.append(body.context["run_id"])
                return SimpleNamespace(run_id=run_id, task=done)

            monkeypatch.setattr(desktop_service_module, "execute_prepared_run", execute)
            service = object.__new__(DesktopService)
            service.session_factory = sessions
            service.bridge = MemoryStreamBridge()
            service.run_manager = SimpleNamespace(cancel=lambda *args, **kwargs: None)
            service.checkpointer = SimpleNamespace()
            service.store = SimpleNamespace()
            service.app_config = SimpleNamespace()

            class Assembler:
                async def assemble(self, requested_run_id):
                    return RunExecutionAssembly(
                        run_id=requested_run_id,
                        body=SimpleNamespace(context={"run_id": requested_run_id}),
                        thread_id="thread-test",
                        agent_factory=lambda: None,
                    )

            workers = [
                DurableRunDispatchWorker(sessions, f"desktop-main:{index}", Assembler(), service._start_dispatched_run, RunDispatchRepository())
                for index in range(2)
            ]
            assert sum(await asyncio.gather(*(worker.drain(limit=1) for worker in workers))) == 1
            assert started == [run_id]
            async with sessions() as session:
                dispatch = await RunDispatchRepository().by_run(session, run_id)
                directive = await session.get(LoopDirective, directive_id)
            assert dispatch.status == "running"
            assert dispatch.claimed_by.startswith("desktop-main:")
            assert directive.lifecycle_state == "run_started"
            assert directive.launched_run_id == run_id
            done.set_result(None)
            await asyncio.sleep(0)
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_loop_start_timeout_cleans_agent_and_activity_before_releasing_lease(tmp_path: Path, runtime_postgres_database) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        activity = record = None
        drain = None
        manager = RunManager()
        committing = asyncio.Event()
        agent_started = asyncio.Event()
        agent_exited = asyncio.Event()
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="start-timeout", started_at=datetime.now(UTC))
            directive_id = await _seed_launching_directive(sessions, fixture, label="start-timeout")
            run_id = await _admit_run(sessions, fixture, directive_id)
            async with sessions() as session:
                slot_id = (await session.get(DesktopRun, run_id)).workspace_anchor["slot_id"]

            class StartupSessions:
                def __call__(self):
                    return sessions()

                @asynccontextmanager
                async def begin(self):
                    async with sessions.begin() as session:
                        yield session
                        await agent_started.wait()
                        committing.set()
                        # Stall after lifecycle writes, before committing the startup transaction.
                        await asyncio.Future()

            class Assembler:
                async def assemble(self, requested_run_id):
                    body = SimpleNamespace(context={"run_id": requested_run_id})
                    await LoopRunWorkspaceBinder(sessions).bind(
                        run_id=requested_run_id, loop_id=fixture["loop_id"], body=body,
                        slot_id=slot_id, directive_id=directive_id,
                    )
                    return RunExecutionAssembly(
                        run_id=requested_run_id, body=body,
                        thread_id=f"thread-{fixture['context_id']}", agent_factory=None,
                    )

            async def agent():
                agent_started.set()
                try:
                    await asyncio.Future()
                finally:
                    await asyncio.sleep(0)
                    assert not activity._closed
                    agent_exited.set()

            async def execute(body, thread_id, adapted, factory):
                nonlocal activity, record
                activity = adapted.bridge
                record = manager.create(thread_id, run_id=run_id)
                record.task = asyncio.create_task(agent())
                return record

            resources = RunExecutionResources(
                bridge=MemoryStreamBridge(), run_manager=manager,
                checkpointer=None, store=None, app_config=None,
            )
            boundary = LoopRunExecutionBoundary(StartupSessions())

            async def start(assembly):
                return await boundary.start(assembly, resources, execute)

            async def failed(identity, reason):
                assert identity == run_id
                assert committing.is_set(), "Timeout must occur after Agent creation and lifecycle writes"
                assert record.task.done() and agent_exited.is_set()
                assert activity._closed and activity._worker.done()
                async with sessions() as session:
                    lease = await session.scalar(select(WorkspaceLease).where(WorkspaceLease.run_id == run_id))
                    assert lease.status == "active", "Lease must cover Agent teardown"
                await RunLifecycleFinalizer(sessions, None).abort_prepared(identity, reason)

            started = []
            worker = DurableRunDispatchWorker(
                sessions, "startup-timeout", Assembler(), start,
                on_started=started.append, on_failed=failed,
            )
            # Shorten only this test's deadline; production keeps its 60-second default.
            worker._startup_deadline_seconds = 1
            drain = asyncio.create_task(worker.drain(limit=1))
            assert await asyncio.wait_for(drain, timeout=5) == 1
            assert started == []
            async with sessions() as session:
                stored = await session.get(DesktopRun, run_id)
                dispatch = await RunDispatchRepository().by_run(session, run_id)
                lease = await session.scalar(select(WorkspaceLease).where(WorkspaceLease.run_id == run_id))
                directive = await session.get(LoopDirective, directive_id)
                assert stored.status == "error" and stored.settled_at is not None
                assert dispatch.status == "failed_to_start" and dispatch.settled_at is not None
                assert dispatch.lease_expires_at is None
                assert stored.error == dispatch.error
                assert lease.status == "released" and lease.released_at is not None
                assert directive.lifecycle_state == "delivering"
                assert directive.status == "launching" and directive.launched_run_id is None
        finally:
            if drain is not None:
                if not drain.done():
                    drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
            await manager.shutdown()
            if activity is not None:
                await activity.close()
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_loop_dispatch_recovery_preserves_plan_only_before_execution(tmp_path: Path, runtime_postgres_database) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="recover-plan", started_at=datetime.now(UTC))
            directive_id = await _seed_launching_directive(sessions, fixture, label="recover-plan")
            run_id = await _admit_run(sessions, fixture, directive_id)
            repository = RunDispatchRepository()
            async with sessions.begin() as session:
                claimed = await repository.claim(session, "desktop-main:dead")
                assert claimed.run_id == run_id
            first = await RunDispatchRecovery(sessions).reconcile()
            assert run_id in first.safe_run_ids
            async with sessions.begin() as session:
                dispatch = await repository.by_run(session, run_id)
                planned = await session.get(DesktopRun, run_id)
                assert dispatch.status == "accepted"
                assert planned.workspace_anchor["slot_id"]
                claimed = await repository.claim(session, "desktop-main:replacement")
                await repository.transition(session, claimed.dispatch_id, claimed.fencing_token, "running")
            second = await RunDispatchRecovery(sessions).reconcile()
            assert run_id in second.interrupted_run_ids
            async with sessions() as session:
                dispatch = await repository.by_run(session, run_id)
            assert dispatch.status == "interrupted"
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("invalid", ["grant", "slot"])
def test_invalid_grant_or_slot_blocks_durable_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="revoked", started_at=datetime.now(UTC))
            directive_id = await _seed_launching_directive(sessions, fixture, label="revoked")
            run_id = await _admit_run(sessions, fixture, directive_id)
            async with sessions.begin() as session:
                directive = await session.get(LoopDirective, directive_id)
                if invalid == "grant":
                    grant = await session.get(LoopDelegationGrant, directive.grant_id, with_for_update=True)
                    grant.status = "revoked"
                else:
                    planned = await session.get(DesktopRun, run_id)
                    slot = await session.get(WorkspaceSlot, planned.workspace_anchor["slot_id"], with_for_update=True)
                    slot.lifecycle = "deleted"

            async def forbidden(*args, **kwargs):
                raise AssertionError("撤销授权后不得启动 Agent")

            monkeypatch.setattr(desktop_service_module, "execute_prepared_run", forbidden)
            service = object.__new__(DesktopService)
            service.session_factory = sessions
            service.bridge = MemoryStreamBridge()
            service.run_manager = SimpleNamespace()
            service.checkpointer = SimpleNamespace()
            service.store = SimpleNamespace()
            service.app_config = SimpleNamespace()

            class Assembler:
                async def assemble(self, requested_run_id):
                    return RunExecutionAssembly(
                        run_id=requested_run_id,
                        body=SimpleNamespace(context={"run_id": requested_run_id}),
                        thread_id="thread-test",
                        agent_factory=lambda: None,
                    )

            worker = DurableRunDispatchWorker(sessions, "desktop-main:revoked", Assembler(), service._start_dispatched_run, RunDispatchRepository())
            assert await worker.drain(limit=1) == 1
            async with sessions() as session:
                dispatch = await RunDispatchRepository().by_run(session, run_id)
                stored = await session.get(DesktopRun, run_id)
            assert dispatch.status == "failed_to_start"
            assert stored.status == "error"
            assert ("授权" if invalid == "grant" else "slot") in stored.error
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_occupied_planned_writer_slot_fails_without_workspace_fallback(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="slot-busy", started_at=datetime.now(UTC))
            directive_id = await _seed_launching_directive(sessions, fixture, label="slot-busy")
            run_id = await _admit_run(sessions, fixture, directive_id)
            owner_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                target = await session.get(DesktopRun, run_id)
                target.equipment = {**target.equipment, "permissions": ["write"]}
                task = await session.get(DesktopThread, fixture["context_id"])
                slot_id = target.workspace_anchor["slot_id"]
                planned_slot = await session.get(WorkspaceSlot, slot_id)
                planned_root = planned_slot.root_path
                session.add(DesktopRun(
                    run_id=owner_id, task_id=task.task_id, agent_id=f"main:{task.task_id}",
                    kind="main", status="running", origin="direct_user",
                    execution_thread_id=task.thread_id, checkpoint_ns="slot-owner",
                ))
            owner_lease = await WorkspaceLeaseManager(sessions).acquire(WorkspaceLeaseRequest(
                slot_id=slot_id, run_id=owner_id, mode=WorkspaceAccessMode.WRITE,
            ))

            service = object.__new__(DesktopService)
            service.session_factory = sessions
            prepared_paths: list[str] = []
            service.checkpointer = InMemorySaver()

            async def prepare(*args, **kwargs):
                prepared_paths.append(args[3])
                return PreparedRun(
                    body=SimpleNamespace(context={"run_id": run_id}),
                    thread_id=task.thread_id, agent_factory=lambda: None, payload={},
                )

            service._prepare = prepare

            async def forbidden(*args, **kwargs):
                raise AssertionError("capacity conflict must prevent Agent startup")

            class Assembler:
                async def assemble(self, requested_run_id):
                    return await service.assemble_run(requested_run_id)

            worker = DurableRunDispatchWorker(
                sessions, "desktop-main:slot-busy", Assembler(), forbidden,
                RunDispatchRepository(),
                on_failed=RunLifecycleFinalizer(sessions, SimpleNamespace()).abort_prepared,
            )
            assert await worker.drain(limit=1) == 1
            async with sessions.begin() as session:
                await LoopCoordinator(sessions).handle_run_settled(
                    SimpleNamespace(run_id=run_id, event_id=uuid.uuid4().hex, payload={}), session,
                )
            async with sessions() as session:
                dispatch = await RunDispatchRepository().by_run(session, run_id)
                stored = await session.get(DesktopRun, run_id)
                directive = await session.get(LoopDirective, directive_id)
            assert dispatch.status == "failed_to_start"
            assert "不兼容的活动 lease" in dispatch.error
            assert stored.status == "error"
            assert directive.lifecycle_state == "delivery_failed"
            assert directive.status == "blocked"
            assert stored.workspace_anchor["slot_id"] == owner_lease.slot_id
            assert prepared_paths == [planned_root]
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_direct_user_loop_run_binds_after_actual_start(tmp_path: Path) -> None:
    from backend.tests.test_loop_user_message_transaction import authorize_message
    from backend.tests.test_loop_failure_repair_transaction import _launch, _finish
    from focus.runtime.runs.schemas import RunStatus

    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        record = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="user-entry", started_at=datetime.now(UTC))
            accepted = await fixture["service"].user_message(fixture["context_id"], "Continue this Context")
            coordinator = LoopCoordinator(sessions)
            claim = await coordinator.claim_for_loop(fixture["loop_id"], "user-entry")
            observation, decision = await authorize_message(sessions, fixture, claim, accepted["intent_id"])
            async with sessions() as session:
                intent = await session.get(LoopUserIntent, accepted["intent_id"])
                assert intent.delivery_state == "observed" and intent.resulting_run_id is None
            record = await _launch(sessions, fixture, coordinator, claim)
            async with sessions() as session:
                intent = await session.get(LoopUserIntent, accepted["intent_id"])
                directive = await session.get(LoopDirective, decision.directive_ids[0])
                started_event = await session.scalar(select(LoopJournalEvent).where(
                    LoopJournalEvent.loop_id == fixture["loop_id"],
                    LoopJournalEvent.kind == "context.run.started",
                    LoopJournalEvent.entity_id == record.run_id,
                ))
                assert intent.delivery_state == "run_started"
                assert intent.resulting_run_id == record.run_id == directive.launched_run_id
                assert started_event is not None
                assert directive.round_id == observation.round_id
            record.status = RunStatus.success
            await _finish(sessions, fixture, coordinator, record, [])
            async with sessions() as session:
                intent = await session.get(LoopUserIntent, accepted["intent_id"])
                settled_event = await session.scalar(select(LoopJournalEvent).where(
                    LoopJournalEvent.loop_id == fixture["loop_id"],
                    LoopJournalEvent.kind == "context.run.settled",
                    LoopJournalEvent.entity_id == record.run_id,
                ))
                assert intent.delivery_state == "settled"
                assert settled_event is not None
        finally:
            if record is not None and not record.task.done():
                record.task.cancel()
                await asyncio.gather(record.task, return_exceptions=True)
                await record.loop_activity_task
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())
