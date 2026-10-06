"""本文件对外提供派发转换与 Run 收口、线程级联删除的真实 PostgreSQL 竞态回归。
输入为逐用例隔离库、已领取 dispatch、父 Run 行锁与装配事件屏障；输出为无反向锁死、未启动收口同步终结 dispatch 和旧领取不再启动的断言。
具体工作流为通过真实 admission/claim 登记执行，让删除事务先持有 Run 锁，再并发运行生产 transition；另核对 abort_prepared 的同事务收口。
示例：pytest backend/tests/test_run_dispatch_lock_order.py；屏障仅观察父锁请求，不替换 SQL、取消另一线程或依赖固定等待秒数。
"""

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.run_orchestration.admission import RunAdmissionService
from backend.app.desktop.run_orchestration.dispatch import DurableRunDispatchWorker, RunDispatchRepository, StaleDispatchFence
from backend.app.desktop.run_orchestration.lifecycle import RunLifecycleFinalizer
from backend.app.desktop.run_orchestration.models import RunDispatch
from backend.tests.test_run_dispatch_durability import _run

pytestmark = pytest.mark.usefixtures("runtime_postgres_database")


async def _admit(sessions, path):
    task_id, workspace_id, run_id = (uuid.uuid4().hex for _ in range(3))
    async with sessions.begin() as session:
        session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(path), display_name="lock-order"))
        await session.flush()
        session.add(DesktopThread(task_id=task_id, workspace_id=workspace_id, thread_id=f"thread-{task_id}", title="lock-order"))
        await session.flush()
        result = await RunAdmissionService().admit(session, _run(task_id, run_id, run_id, "durable input"))
        dispatch_id = result.dispatch.dispatch_id
    return task_id, run_id, dispatch_id


def test_transition_obeys_parent_run_lock_order_during_thread_cascade(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        reached_parent = asyncio.Event()
        pending = None

        class ObservedSession(AsyncSession):
            async def get(self, entity, ident, **options):
                if entity is DesktopRun and options.get("with_for_update"):
                    reached_parent.set()
                return await super().get(entity, ident, **options)

        observed_sessions = async_sessionmaker(engine, class_=ObservedSession, expire_on_commit=False)
        try:
            task_id, run_id, dispatch_id = await _admit(sessions, tmp_path)
            async with sessions.begin() as session:
                claimed = await RunDispatchRepository().claim(session, "lock-order")
                token = claimed.fencing_token

            async def transition():
                async with observed_sessions.begin() as session:
                    with pytest.raises(LookupError):
                        await RunDispatchRepository().transition(session, dispatch_id, token, "running")

            async with sessions.begin() as deletion:
                await deletion.scalar(select(DesktopRun).where(DesktopRun.run_id == run_id).with_for_update())
                pending = asyncio.create_task(transition())
                await asyncio.wait_for(reached_parent.wait(), 5)
                await deletion.execute(delete(DesktopThread).where(DesktopThread.task_id == task_id))
            await asyncio.wait_for(pending, 5)
            async with sessions() as session:
                assert await session.get(DesktopRun, run_id) is None
                assert await session.get(RunDispatch, dispatch_id) is None
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("claimed", [False, True])
def test_abort_prepared_settles_dispatch_atomically_and_fences_claim(tmp_path, claimed):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            _, run_id, dispatch_id = await _admit(sessions, tmp_path)
            token = None
            if claimed:
                async with sessions.begin() as session:
                    dispatch = await RunDispatchRepository().claim(session, "abort-order")
                    token = dispatch.fencing_token
            finalizer = RunLifecycleFinalizer(sessions, SimpleNamespace())
            assert await finalizer.abort_prepared(run_id, "not started")
            async with sessions() as session:
                stored = await session.get(DesktopRun, run_id)
                dispatch = await session.get(RunDispatch, dispatch_id)
                assert stored.status == "error" and stored.settled_at is not None
                assert dispatch.status == "settled" and dispatch.settled_at is not None
                assert dispatch.lease_expires_at is None
            if claimed:
                async with sessions.begin() as session:
                    with pytest.raises(StaleDispatchFence):
                        await RunDispatchRepository().transition(session, dispatch_id, token, "running")
            async with sessions.begin() as session:
                assert await RunDispatchRepository().claim(session, "late-worker") is None
            assert not await finalizer.abort_prepared(run_id, "duplicate")
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_worker_aborted_during_assembly_does_not_start_or_rewrite_settlement(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        entered, release = asyncio.Event(), asyncio.Event()
        started, failed = [], []
        pending = None

        class Assembler:
            async def assemble(self, run_id):
                entered.set()
                await release.wait()
                return SimpleNamespace(run_id=run_id)

        async def start(assembly):
            started.append(assembly.run_id)

        async def on_failed(identity, reason):
            failed.append(identity)

        try:
            _, run_id, dispatch_id = await _admit(sessions, tmp_path)
            worker = DurableRunDispatchWorker(sessions, "late-assembly", Assembler(), start, on_failed=on_failed)
            pending = asyncio.create_task(worker.drain(limit=1))
            await asyncio.wait_for(entered.wait(), 5)
            assert await RunLifecycleFinalizer(sessions, SimpleNamespace()).abort_prepared(run_id, "assembly aborted")
            release.set()
            assert await asyncio.wait_for(pending, 5) == 1
            assert started == [] and failed == []
            async with sessions() as session:
                run = await session.get(DesktopRun, run_id)
                dispatch = await session.get(RunDispatch, dispatch_id)
                assert run.status == "error" and run.error == "assembly aborted"
                assert dispatch.status == "settled" and dispatch.attempt == 1
                assert run.settled_at is not None and dispatch.settled_at is not None
        finally:
            release.set()
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await engine.dispose()
    asyncio.run(run())
