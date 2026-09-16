r"""本文件验证唯一 RunLauncher、持久 Run identity、活跃 main fencing、原子终态与 outbox 恢复。

输入为并发注册请求、假 PreparedRun/Request、已完成 RunRecord 和假 checkpoint；输出为单次 launch、
单持久 Run、完整历史字段、非根 namespace checkpoint publication、事务回滚后可重试的 settlement，
以及重启后恰好一次消费断言。具体工作流为使用隔离 PostgreSQL 建立 Context revision，再分别穿过
registrar、launcher、finalizer 和 consumer。
示例：`pytest backend/tests/test_unified_run_orchestration.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
import os
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.context_evolution import (
    ContextRevisionContract,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionRef,
    ContextRevisionRepository,
)
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.run_orchestration import (
    RunLauncher,
    RunLifecycleFinalizer,
    RunOutboxConsumer,
    RunOutboxDelivery,
    RunOutboxEvent,
    RunOutboxRepository,
    RunRegistrar,
    RunRegistrationConflict,
    RunRegistrationRequest,
)
from focus.runtime.runs.manager import RunRecord
from focus.runtime.runs.schemas import DisconnectMode, RunStatus


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


class _Checkpoint:
    def __init__(self, checkpoint_id: str) -> None:
        self.config = {"configurable": {"checkpoint_id": checkpoint_id}}


class _Checkpointer:
    def __init__(self, checkpoint_id: str) -> None:
        self.checkpoint_id = checkpoint_id
        self.configs = []

    async def aget_tuple(self, config):
        self.configs.append(config)
        return _Checkpoint(self.checkpoint_id)


class _FailingOutbox(RunOutboxRepository):
    async def enqueue_settled(self, session, run_id, payload):
        raise RuntimeError("simulated outbox failure")


async def _seed(sessions, suffix: str):
    workspace_id = f"ws-run-{suffix}"
    context_id = f"context-run-{suffix}"
    revision_id = uuid.uuid4().hex
    ref = ContextRevisionRef(
        context_id=context_id,
        revision_id=revision_id,
        generation=1,
        execution_thread_id=f"thread-{context_id}",
        checkpoint_ns="context-revision-shadow",
        checkpoint_id=f"base-{revision_id}",
        payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
    )
    contract = ContextRevisionContract(
        ref=ref,
        content_hash="a" * 64,
        projection_status=ContextRevisionProjectionStatus.VALID,
        origin_kind=ContextRevisionOriginKind.ROOT,
        created_at=datetime.now(UTC),
    )
    repository = ContextRevisionRepository()
    async with sessions.begin() as session:
        session.add(
            DesktopWorkspace(
                workspace_id=workspace_id,
                path=f"/tmp/{workspace_id}",
                display_name="Run orchestration",
            )
        )
        await session.flush()
        session.add(
            DesktopThread(
                task_id=context_id,
                workspace_id=workspace_id,
                thread_id=ref.execution_thread_id,
                title="Run Context",
            )
        )
        await session.flush()
        await repository.insert(session, contract)
        await repository.switch_current(session, ref, None)
    return workspace_id, context_id, ref


def _registration(context_id: str, ref: ContextRevisionRef, run_id: str, key: str):
    return RunRegistrationRequest(
        run_id=run_id,
        task_id=context_id,
        agent_id=f"main:{context_id}",
        kind="main",
        origin="delegated_patrol",
        execution_thread_id=ref.execution_thread_id,
        checkpoint_ns=ref.checkpoint_ns,
        context_revision_id=ref.revision_id,
        context_checkpoint_id=ref.checkpoint_id,
        origin_message_id="message-1",
        directive_id="directive-1",
        loop_id="loop-1",
        round_id="round-1",
        action_id="action-1",
        equipment={"model_name": "test-model", "permissions": ["read"]},
        workspace_anchor={"revision": "workspace-r1"},
        idempotency_key=key,
        input_messages=({"id": "message-1", "role": "human", "content": "Continue."},),
        model_name="test-model",
    )


def test_launcher_calls_existing_start_run_once(monkeypatch) -> None:
    calls = []
    attached = []
    expected = object()

    async def fake_start(body, thread_id, request, agent_factory=None):
        calls.append((body, thread_id, agent_factory))
        return expected

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                desktop_service=SimpleNamespace(attach_run_sync=attached.append)
            )
        )
    )
    prepared = SimpleNamespace(
        body={"input": {}},
        thread_id="thread-1",
        agent_factory=lambda: None,
        payload={},
    )
    result = asyncio.run(RunLauncher(fake_start).launch(prepared, request))
    assert result is expected
    assert calls == [(prepared.body, "thread-1", prepared.agent_factory)]
    assert attached == [expected]


def test_concurrent_registration_returns_one_idempotent_run_and_fences_second_main() -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        registrar = RunRegistrar(sessions)
        workspace_id, context_id, ref = await _seed(sessions, uuid.uuid4().hex[:8])
        try:
            first, second = await asyncio.gather(
                registrar.register(_registration(context_id, ref, uuid.uuid4().hex, "launch-1")),
                registrar.register(_registration(context_id, ref, uuid.uuid4().hex, "launch-1")),
            )
            assert first.run_id == second.run_id
            with pytest.raises(RunRegistrationConflict):
                await registrar.register(
                    _registration(context_id, ref, uuid.uuid4().hex, "launch-2")
                )
            async with sessions() as session:
                rows = list(
                    (
                        await session.scalars(
                            select(DesktopRun).where(DesktopRun.task_id == context_id)
                        )
                    ).all()
                )
                assert len(rows) == 1
                persisted = rows[0]
                assert persisted.origin == "delegated_patrol"
                assert persisted.directive_id == "directive-1"
                assert persisted.equipment["model_name"] == "test-model"
                assert persisted.workspace_anchor["revision"] == "workspace-r1"
        finally:
            async with sessions.begin() as session:
                await session.execute(
                    delete(DesktopWorkspace).where(
                        DesktopWorkspace.workspace_id == workspace_id
                    )
                )
            await engine.dispose()

    asyncio.run(run())


def test_finalization_is_atomic_idempotent_and_restart_consumer_drains() -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workspace_id, context_id, ref = await _seed(sessions, uuid.uuid4().hex[:8])
        run_id = uuid.uuid4().hex
        registrar = RunRegistrar(sessions)
        await registrar.register(_registration(context_id, ref, run_id, "settle-1"))
        record = RunRecord(
            run_id=run_id,
            thread_id=ref.execution_thread_id,
            status=RunStatus.success,
            on_disconnect=DisconnectMode.cancel,
            model_call_count=3,
            prompt_input_tokens=91,
            prompt_output_tokens=23,
            prompt_cache_hit_tokens=17,
        )
        failing = RunLifecycleFinalizer(
            sessions,
            _Checkpointer("final-checkpoint"),
            outbox=_FailingOutbox(),
        )
        try:
            with pytest.raises(RuntimeError, match="outbox failure"):
                await failing.finalize(record, {"revision": "workspace-r2"})
            async with sessions() as session:
                persisted = await session.get(DesktopRun, run_id)
                current = await ContextRevisionRepository().current(session, context_id)
                assert persisted.status == "pending"
                assert persisted.settled_at is None
                assert current.ref == ref

            outbox = RunOutboxRepository()
            checkpointer = _Checkpointer("final-checkpoint")
            finalizer = RunLifecycleFinalizer(
                sessions,
                checkpointer,
                outbox=outbox,
            )
            settlement = await finalizer.finalize(record, {"revision": "workspace-r2"})
            again = await finalizer.finalize(record, {"revision": "ignored"})
            assert settlement.context_publication == "published"
            assert again.idempotent is True
            assert again.event_id == settlement.event_id
            async with sessions.begin() as session:
                event = await session.get(RunOutboxEvent, settlement.event_id)
                event.status = "claimed"
                event.claimed_by = "dead-consumer"

            consumer = RunOutboxConsumer(sessions, outbox)
            assert await consumer.recover() == 1

            async def handle(event, session):
                persisted = await session.get(DesktopRun, event.run_id)
                persisted.workspace_result = {
                    **persisted.workspace_result,
                    "coordinator_wakes": persisted.workspace_result.get(
                        "coordinator_wakes", 0
                    )
                    + 1,
                }

            assert await consumer.drain("loop-coordinator", handle) >= 1
            assert await consumer.drain("loop-coordinator", handle) == 0
            async with sessions() as session:
                persisted = await session.get(DesktopRun, run_id)
                current = await ContextRevisionRepository().current(session, context_id)
                receipts = list((await session.scalars(select(RunOutboxDelivery))).all())
                assert persisted.status == "success"
                assert persisted.final_checkpoint_id == "final-checkpoint"
                assert persisted.model_call_count == 3
                assert persisted.prompt_input_tokens == 91
                assert persisted.prompt_output_tokens == 23
                assert persisted.workspace_result["coordinator_wakes"] == 1
                assert current.ref.checkpoint_id == "final-checkpoint"
                assert current.ref.checkpoint_ns == "context-revision-shadow"
                assert checkpointer.configs[-1]["configurable"] == {
                    "thread_id": ref.execution_thread_id,
                    "checkpoint_ns": "context-revision-shadow",
                }
                assert any(receipt.event_id == settlement.event_id for receipt in receipts)
        finally:
            async with sessions.begin() as session:
                await session.execute(
                    delete(DesktopWorkspace).where(
                        DesktopWorkspace.workspace_id == workspace_id
                    )
                )
            await engine.dispose()

    asyncio.run(run())
