r"""本文件对外提供 durable Main Run admission、dispatch lease/fencing 与 restart recovery 回归测试。

输入为两个独立任务、并发幂等 Run 请求、过期 claim 和 running/pending 重启状态；输出为唯一 accepted dispatch、任务级并发、旧 fence 拒绝及安全恢复分类断言。
具体工作流为在隔离 PostgreSQL 中登记 Run，模拟两代 worker 领取，再执行恢复并检查原始输入仍保留。示例：`pytest backend/tests/test_run_dispatch_durability.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.run_orchestration.admission import RunAdmissionService
from backend.app.desktop.run_orchestration.assembler import RunExecutionAssembly
from backend.app.desktop.run_orchestration.dispatch import DurableRunDispatchWorker, RunDispatchRecovery, RunDispatchRepository, StaleDispatchFence
from backend.app.desktop.run_orchestration.models import RunDispatch


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_admission_claim_fencing_and_recovery_are_task_scoped(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workspace_id = uuid.uuid4().hex
        task_a = uuid.uuid4().hex
        task_b = uuid.uuid4().hex
        admission = RunAdmissionService()
        repository = RunDispatchRepository()
        try:
            async with sessions.begin() as session:
                now = datetime.now(UTC)
                await session.execute(update(RunDispatch).where(RunDispatch.status.in_(("accepted", "claimed", "running"))).values(status="settled", settled_at=now, lease_expires_at=None))
                await session.execute(update(DesktopRun).where(DesktopRun.status.in_(("pending", "running"))).values(status="success", settled_at=now))
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="dispatch"))
                await session.flush()
                session.add_all([
                    DesktopThread(task_id=task_a, workspace_id=workspace_id, thread_id=f"thread-{task_a}", title="A"),
                    DesktopThread(task_id=task_b, workspace_id=workspace_id, thread_id=f"thread-{task_b}", title="B"),
                ])

            run_a = _run(task_a, "a" * 32, "request-a", "你\x00好 A")
            run_b = _run(task_b, "b" * 32, "request-b", "你好 B")
            async with sessions.begin() as session:
                accepted_a = await admission.admit(session, run_a)
            async with sessions.begin() as session:
                duplicate = await admission.admit(session, _run(task_a, "c" * 32, "request-a", "重复 A"))
                accepted_b = await admission.admit(session, run_b)
            assert accepted_a.created is True
            assert duplicate.created is False
            assert duplicate.run.run_id == run_a.run_id
            assert accepted_b.created is True

            async with sessions.begin() as session:
                first_claim = await repository.claim(session, "worker-1", lease_seconds=10)
                first_identity = (first_claim.dispatch_id, first_claim.fencing_token)
                first_claim.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            async with sessions.begin() as session:
                second_claim = await repository.claim(session, "worker-2", lease_seconds=10)
                assert second_claim.dispatch_id == first_identity[0]
                assert second_claim.fencing_token > first_identity[1]
                second_identity = (second_claim.dispatch_id, second_claim.fencing_token)
            async with sessions.begin() as session:
                with pytest.raises(StaleDispatchFence):
                    await repository.transition(session, first_identity[0], first_identity[1], "running")
            async with sessions.begin() as session:
                await repository.transition(session, second_identity[0], second_identity[1], "running")

            recovery = RunDispatchRecovery(sessions)
            report = await recovery.reconcile()
            assert run_a.run_id in report.interrupted_run_ids
            assert run_b.run_id in report.safe_run_ids
            async with sessions() as session:
                restored_a = await session.get(DesktopRun, run_a.run_id)
                restored_b = await session.get(DesktopRun, run_b.run_id)
                dispatch_a = await session.get(RunDispatch, accepted_a.dispatch.dispatch_id)
                assert restored_a.input_messages[0]["content"] == r"你\u0000好 A"
                assert restored_a.equipment["_persistence_safety"]["run_id"] == run_a.run_id
                assert restored_a.equipment["_persistence_safety"]["fields"]["input_messages"]["affected_paths"] == ["$[0].content"]
                assert restored_b.input_messages[0]["content"] == "你好 B"
                assert dispatch_a.status == "interrupted"
            repeated = await recovery.reconcile()
            assert run_a.run_id in repeated.interrupted_run_ids
            assert run_b.run_id in repeated.safe_run_ids
            async with sessions() as session:
                dispatch_a = await session.get(RunDispatch, accepted_a.dispatch.dispatch_id)
                assert dispatch_a.status == "interrupted"
                assert len(tuple((await session.scalars(select(RunDispatch).where(RunDispatch.run_id == run_a.run_id))).all())) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_worker_fences_before_start_and_reports_start_failure(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workspace_id = uuid.uuid4().hex
        task_id = uuid.uuid4().hex
        repository = RunDispatchRepository()
        admission = RunAdmissionService()
        started: list[str] = []
        failed: list[tuple[str, str]] = []
        try:
            async with sessions.begin() as session:
                await session.execute(update(RunDispatch).where(RunDispatch.status == "accepted").values(status="settled", settled_at=datetime.now(UTC)))
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="worker"))
                await session.flush()
                session.add(DesktopThread(task_id=task_id, workspace_id=workspace_id, thread_id=f"thread-{task_id}", title="worker"))
            admitted = _run(task_id, "d" * 32, "request-worker", "durable input")
            async with sessions.begin() as session:
                await admission.admit(session, admitted)

            class Assembler:
                async def assemble(self, run_id: str) -> RunExecutionAssembly:
                    return RunExecutionAssembly(run_id=run_id, body=object(), thread_id="thread", agent_factory=lambda: None)

            async def fail_after_running(assembly: RunExecutionAssembly):
                async with sessions() as session:
                    dispatch = await session.get(RunDispatch, uuid.uuid5(uuid.NAMESPACE_URL, f"focus:run-dispatch:{assembly.run_id}").hex)
                    assert dispatch.status == "running"
                raise RuntimeError("agent construction failed")

            async def on_failed(run_id: str, reason: str) -> None:
                failed.append((run_id, reason))

            worker = DurableRunDispatchWorker(
                sessions,
                "worker-1",
                Assembler(),
                fail_after_running,
                repository,
                on_started=lambda value: started.append(str(value)),
                on_failed=on_failed,
            )
            assert await worker.drain(limit=1) == 1
            async with sessions() as session:
                dispatch = await session.scalar(select(RunDispatch).where(RunDispatch.run_id == admitted.run_id))
                restored = await session.get(DesktopRun, admitted.run_id)
                assert dispatch.status == "failed_to_start"
                assert restored.status == "error"
                assert restored.input_messages[0]["content"] == "durable input"
            assert started == []
            assert failed == [(admitted.run_id, "agent construction failed")]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_concurrent_admission_converges_on_one_run_and_dispatch(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workspace_id = uuid.uuid4().hex
        task_id = uuid.uuid4().hex
        admission = RunAdmissionService()
        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="concurrent"))
                await session.flush()
                session.add(DesktopThread(task_id=task_id, workspace_id=workspace_id, thread_id=f"thread-{task_id}", title="concurrent"))

            async def admit(run_id: str) -> tuple[str, bool]:
                async with sessions.begin() as session:
                    result = await admission.admit(session, _run(task_id, run_id, "shared-request", "same request"))
                    return result.run.run_id, result.created

            first, second = await asyncio.gather(admit("e" * 32), admit("f" * 32))
            assert first[0] == second[0]
            assert sorted((first[1], second[1])) == [False, True]
            async with sessions() as session:
                runs = tuple((await session.scalars(select(DesktopRun).where(DesktopRun.idempotency_key == "shared-request"))).all())
                dispatches = tuple((await session.scalars(select(RunDispatch).where(RunDispatch.run_id == first[0]))).all())
                assert len(runs) == 1
                assert len(dispatches) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def _run(task_id: str, run_id: str, key: str, content: str) -> DesktopRun:
    return DesktopRun(
        run_id=run_id,
        task_id=task_id,
        agent_id=f"main:{task_id}",
        kind="main",
        status="pending",
        origin="direct_user",
        execution_thread_id=f"thread-{task_id}",
        checkpoint_ns="",
        idempotency_key=key,
        input_messages=[{"role": "user", "content": content}],
        equipment={"_durable_dispatch_execution": {"agent_role": "main", "base_prompt": "test"}},
        workspace_anchor={},
    )
