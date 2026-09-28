r"""本文件对外提供 Loop Run 单一 durable 启动者的竞争与授权回归测试。

输入为真实 PostgreSQL 中的已授权 Directive、持久 Run 计划和两个并发 worker；输出为一次启动、正确生命周期，以及撤销授权后的启动拒绝。
具体工作流为模拟通用 worker 竞争同一调度行，使用真实 fencing 与 Loop 启动边界，并在 Run 结算前查询持久状态。示例：`pytest backend/tests/test_loop_execution_ownership.py -q`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from focus.runtime.stream_bridge.memory import MemoryStreamBridge
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop import service as desktop_service_module
from backend.app.desktop.agent_loop.models import LoopAction, LoopDecision, LoopDelegationGrant, LoopDirective
from backend.app.desktop.agent_loop.models import LoopUserIntent
from backend.app.desktop.agent_loop.intervention_lifecycle import InterventionLifecycleRepository
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.models import DesktopRun, DesktopThread
from backend.app.desktop.run_orchestration.admission import RunAdmissionService
from backend.app.desktop.run_orchestration.assembler import RunExecutionAssembly
from backend.app.desktop.run_orchestration.dispatch import DurableRunDispatchWorker, RunDispatchRecovery, RunDispatchRepository
from backend.app.desktop.run_orchestration.lifecycle import RunLifecycleFinalizer
from backend.app.desktop.service import DesktopService, PreparedRun
from backend.app.desktop.workspace_coordination.leases import WorkspaceLeaseManager
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot
from backend.app.desktop.workspace_coordination.schemas import WorkspaceAccessMode, WorkspaceLeaseRequest

from test_agent_loop_round_liveness import _seed_loop, _stop


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


async def _seed_launching_directive(sessions, fixture: dict, *, label: str) -> str:
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
                intent={},
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
    return directive_id


async def _admit_run(sessions, fixture: dict, directive_id: str) -> str:
    run_id = uuid.uuid4().hex
    async with sessions.begin() as session:
        directive = await session.get(LoopDirective, directive_id)
        task = await session.get(DesktopThread, fixture["context_id"])
        slot = await session.scalar(select(WorkspaceSlot).where(
            WorkspaceSlot.workspace_id == task.workspace_id,
            WorkspaceSlot.kind == "authoritative",
        ))
        assert slot is not None
        admission = await RunAdmissionService().admit(session, DesktopRun(
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
            equipment={"_durable_dispatch_execution": {"agent_role": "main", "base_prompt": "test"}},
            workspace_anchor={"slot_id": slot.slot_id},
        ))
        admission.dispatch.accepted_at = datetime(2000, 1, 1, tzinfo=UTC)
    return run_id


def test_competing_durable_workers_start_one_loop_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_direct_user_loop_run_binds_after_actual_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="user-entry", started_at=datetime.now(UTC))
            intent_id = uuid.uuid4().hex
            run_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                task = await session.get(DesktopThread, fixture["context_id"])
                slot = await session.scalar(select(WorkspaceSlot).where(
                    WorkspaceSlot.workspace_id == task.workspace_id,
                    WorkspaceSlot.kind == "authoritative",
                ))
                intent = LoopUserIntent(
                    intent_id=intent_id,
                    loop_id=fixture["loop_id"],
                    scope="context",
                    target_context_id=fixture["context_id"],
                    content="Continue this Context",
                    origin_kind="user",
                    correlation_id=f"user:{intent_id}",
                    goal_revision=1,
                    authority_revision=1,
                    observed_round_id=fixture["round_id"],
                )
                session.add(intent)
                await InterventionLifecycleRepository().register(session, intent)
                await InterventionLifecycleRepository().transition(session, intent_id, "accepted")
                admission = await RunAdmissionService().admit(session, DesktopRun(
                    run_id=run_id,
                    task_id=fixture["context_id"],
                    agent_id=f"main:{fixture['context_id']}",
                    kind="main",
                    status="pending",
                    origin="direct_user",
                    execution_thread_id=task.thread_id,
                    origin_message_id=f"message-{run_id}",
                    context_revision_id=fixture["revision_id"],
                    user_intent_id=intent_id,
                    loop_id=fixture["loop_id"],
                    round_id=fixture["round_id"],
                    equipment={"_durable_dispatch_execution": {"agent_role": "main", "base_prompt": "test"}},
                    workspace_anchor={"slot_id": slot.slot_id},
                ))
                admission.dispatch.accepted_at = datetime(2000, 1, 1, tzinfo=UTC)
            done = asyncio.get_running_loop().create_future()

            async def execute(body, thread_id, resources, factory):
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

            worker = DurableRunDispatchWorker(sessions, "desktop-main:user", Assembler(), service._start_dispatched_run, RunDispatchRepository())
            assert await worker.drain(limit=1) == 1
            await fixture["service"].bind_user_message_run(intent_id, run_id)
            async with sessions() as session:
                intent = await session.get(LoopUserIntent, intent_id)
                started_event = await session.scalar(select(LoopJournalEvent).where(
                    LoopJournalEvent.loop_id == fixture["loop_id"],
                    LoopJournalEvent.kind == "context.run.started",
                    LoopJournalEvent.entity_id == run_id,
                ))
            assert intent.delivery_state == "run_started"
            assert intent.resulting_run_id == run_id
            assert started_event is not None
            done.set_result(None)
            await asyncio.sleep(0)
            async with sessions.begin() as session:
                settled = await session.get(DesktopRun, run_id, with_for_update=True)
                settled.status = "success"
                settled.settled_at = datetime.now(UTC)
                await LoopCoordinator(sessions).handle_run_settled(
                    SimpleNamespace(run_id=run_id, event_id=uuid.uuid4().hex, payload={}), session,
                )
            async with sessions() as session:
                settled_event = await session.scalar(select(LoopJournalEvent).where(
                    LoopJournalEvent.loop_id == fixture["loop_id"],
                    LoopJournalEvent.kind == "context.run.settled",
                    LoopJournalEvent.entity_id == run_id,
                ))
            assert settled_event is not None
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_loop_dispatch_recovery_preserves_plan_only_before_execution(tmp_path: Path) -> None:
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
