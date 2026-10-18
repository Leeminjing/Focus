"""Run 收尾失败后的在线重试、补偿与取消回归；不重新启动执行器。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.desktop.service import DesktopService


@asynccontextmanager
async def _transaction():
    yield object()


def _service():
    service = DesktopService.__new__(DesktopService)
    service._sync_tasks = set()
    service._run_sync_tasks = {}
    service._pending_run_finalizations = {}
    service._closing = False
    service.session_factory = SimpleNamespace(begin=_transaction)
    service._run_dispatch_repository = SimpleNamespace(settle_by_run=AsyncMock())
    service.context_patrol = SimpleNamespace(notify_stable_context_checkpoint=AsyncMock(), close=AsyncMock())
    service._run_dispatch_wakeup = asyncio.Event()
    service._run_dispatch_task = None
    service._watcher = None
    return service


@pytest.mark.parametrize("failures", [1, 5])
def test_transient_finalization_failure_retries_without_rerunning_agent(monkeypatch, failures):
    delays = []
    original_sleep = asyncio.sleep

    async def sleep(delay):
        delays.append(delay)
        await original_sleep(0)

    async def scenario():
        service = _service()
        persisted = {"status": "running", "settled": False}
        executions = []
        attempts = []

        async def execute():
            executions.append("executed")

        async def finalize(record):
            attempts.append(record.run_id)
            if len(attempts) <= failures:
                raise RuntimeError("transient database outage")
            persisted.update(status="success", settled=True)
            return SimpleNamespace(kind="main", task_id="context")

        service.run_lifecycle = SimpleNamespace(finalize=finalize)
        record = SimpleNamespace(run_id="completed-run", task=asyncio.create_task(execute()))
        service.attach_run_sync(record)
        await asyncio.wait_for(asyncio.gather(*service._sync_tasks), timeout=2)

        assert persisted == {"status": "success", "settled": True}
        assert attempts == [record.run_id] * (failures + 1)
        assert executions == ["executed"]
        assert not service._sync_tasks
        assert not service._pending_run_finalizations
        assert not service._run_sync_tasks
        assert service._run_dispatch_wakeup.is_set()
        service.context_patrol.notify_stable_context_checkpoint.assert_awaited_once_with("context")
        assert delays == [0.001, 0.002, 0.004, 0.004, 0.004][:failures]

    monkeypatch.setattr("backend.app.desktop.service._RUN_FINALIZATION_RETRY_INITIAL_SECONDS", 0.001)
    monkeypatch.setattr("backend.app.desktop.service._RUN_FINALIZATION_RETRY_MAX_SECONDS", 0.004)
    monkeypatch.setattr("backend.app.desktop.service.asyncio.sleep", sleep)
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_before_start", [True, False])
def test_dispatch_loop_compensates_lost_sync_and_deduplicates_attachments(cancel_before_start):
    async def scenario():
        service = _service()
        executing = asyncio.Event()
        release = asyncio.Event()
        executions = []

        async def execute():
            executions.append("executed")
            executing.set()
            await release.wait()

        record = SimpleNamespace(run_id="lost-sync", task=asyncio.create_task(execute()))
        service.run_lifecycle = SimpleNamespace(finalize=AsyncMock(return_value=SimpleNamespace(kind="main", task_id="context")))
        service.attach_run_sync(record)
        original = service._run_sync_tasks[record.run_id]
        service.attach_run_sync(record)
        assert service._run_sync_tasks[record.run_id] is original
        if not cancel_before_start:
            await executing.wait()
        original.cancel()
        await asyncio.gather(original, return_exceptions=True)
        assert not record.task.done()  # Losing the observer must not stop the Agent.
        assert service._pending_run_finalizations == {record.run_id: record}
        assert not service._sync_tasks

        drained = asyncio.Event()

        async def drain():
            drained.set()

        service.conversation_inputs = SimpleNamespace(drain=drain)
        service._run_dispatch_worker = SimpleNamespace(drain=AsyncMock())
        service._run_dispatch_task = asyncio.create_task(service._run_dispatch_loop())
        try:
            await asyncio.wait_for(drained.wait(), timeout=2)
            replacement = service._run_sync_tasks[record.run_id]
            assert replacement is not original
            release.set()
            await asyncio.wait_for(replacement, timeout=2)
            assert executions == ["executed"]
            service.run_lifecycle.finalize.assert_awaited_once_with(record)
            assert not service._pending_run_finalizations
        finally:
            await service.close()

    asyncio.run(scenario())


def test_lost_backoff_task_keeps_terminal_record_for_online_compensation(monkeypatch):
    async def scenario():
        service = _service()
        failed = asyncio.Event()
        attempts = []

        async def finalize(record):
            attempts.append(record)
            if len(attempts) == 1:
                failed.set()
                raise RuntimeError("transient database outage")
            return SimpleNamespace(kind="main", task_id="context")

        service.run_lifecycle = SimpleNamespace(finalize=finalize)
        record = SimpleNamespace(run_id="retry-lost", task=asyncio.create_task(asyncio.sleep(0)))
        service.attach_run_sync(record)
        original = service._run_sync_tasks[record.run_id]
        await asyncio.wait_for(failed.wait(), timeout=2)
        original.cancel()
        await asyncio.gather(original, return_exceptions=True)
        # Compensation uses the retained record even when RunManager no longer has it.
        service.run_manager = SimpleNamespace(get=lambda _: None)
        service._resume_pending_run_finalizations()
        replacement = service._run_sync_tasks[record.run_id]
        assert replacement is not original
        await asyncio.wait_for(replacement, timeout=2)
        assert attempts == [record, record]
        assert not service._pending_run_finalizations

    monkeypatch.setattr("backend.app.desktop.service._RUN_FINALIZATION_RETRY_INITIAL_SECONDS", 60)
    asyncio.run(scenario())


def test_close_cancels_backoff_without_losing_pending_record(monkeypatch):
    async def scenario():
        service = _service()
        failed = asyncio.Event()

        async def finalize(record):
            failed.set()
            raise RuntimeError("database remains unavailable")

        service.run_lifecycle = SimpleNamespace(finalize=finalize)
        record = SimpleNamespace(run_id="pending-restart", task=asyncio.create_task(asyncio.sleep(0)))
        service.attach_run_sync(record)
        task = service._run_sync_tasks[record.run_id]
        await asyncio.wait_for(failed.wait(), timeout=2)
        await asyncio.wait_for(service.close(), timeout=2)
        assert task.cancelled()
        assert service._pending_run_finalizations == {record.run_id: record}
        service._resume_pending_run_finalizations()
        assert not service._sync_tasks

    monkeypatch.setattr("backend.app.desktop.service._RUN_FINALIZATION_RETRY_INITIAL_SECONDS", 60)
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["cancelled", "error", "cancelled_activity"])
def test_abnormal_execution_exit_still_finalizes(outcome):
    async def scenario():
        service = _service()

        async def execute():
            if outcome == "error":
                raise RuntimeError("unexpected Agent task error")
            if outcome == "cancelled":
                raise asyncio.CancelledError

        record = SimpleNamespace(run_id="abnormal-exit", task=asyncio.create_task(execute()))
        if outcome == "cancelled_activity":
            record.loop_activity_task = asyncio.create_task(asyncio.sleep(0))
            record.loop_activity_task.cancel()
        service.run_lifecycle = SimpleNamespace(finalize=AsyncMock(return_value=SimpleNamespace(kind="worker", task_id="context")))
        service.attach_run_sync(record)
        await asyncio.wait_for(asyncio.gather(*service._sync_tasks), timeout=2)
        service.run_lifecycle.finalize.assert_awaited_once_with(record)
        service.context_patrol.notify_stable_context_checkpoint.assert_not_awaited()
        assert not service._pending_run_finalizations

    asyncio.run(scenario())
