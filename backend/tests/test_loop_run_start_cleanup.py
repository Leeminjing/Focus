"""启动事务提交前失败时，真实 Agent task 和活动记录 worker 必须先退出，再传播异常。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from focus.runtime.runs.manager import RunManager
from focus.runtime.runs.schemas import RunStatus
from focus.runtime.stream_bridge.memory import MemoryStreamBridge

from backend.app.desktop.patrol.agent_loop.execution.run_execution import LoopRunExecutionBoundary
from backend.app.desktop.runs.orchestration.assembler import RunExecutionAssembly
from backend.app.desktop.runs.orchestration.executor import RunExecutionResources


@pytest.mark.parametrize("failure", ["cancel", "timeout", "error"])
def test_start_failure_waits_for_agent_and_closes_activity_before_propagating(failure, monkeypatch):
    async def run():
        committing = asyncio.Event()
        agent_started = asyncio.Event()
        agent_exited = asyncio.Event()
        activity = None
        record = None

        @asynccontextmanager
        async def transaction():
            yield None
            await agent_started.wait()
            committing.set()
            if failure == "error":
                raise RuntimeError("startup commit failed")
            await asyncio.Future()

        boundary = LoopRunExecutionBoundary(SimpleNamespace(begin=transaction))

        async def identity(session, run_id, *, lock):
            return SimpleNamespace(
                loop_id="loop", task_id="context", run_id=run_id,
                origin_message_id="message", origin="delegated_patrol",
            ), None, None

        monkeypatch.setattr(boundary, "_identity", identity)
        manager = RunManager()
        resources = RunExecutionResources(
            bridge=MemoryStreamBridge(), run_manager=manager,
            checkpointer=None, store=None, app_config=None,
        )
        assembly = RunExecutionAssembly(
            run_id="startup-cleanup", body=None, thread_id="thread", agent_factory=None,
        )

        async def agent():
            agent_started.set()
            try:
                await asyncio.Future()
            finally:
                # Cleanup must await asynchronous Agent teardown before closing its bridge.
                await asyncio.sleep(0)
                assert not activity._closed
                agent_exited.set()

        async def execute(body, thread_id, adapted, factory):
            nonlocal activity, record
            activity = adapted.bridge
            record = manager.create(thread_id, run_id=assembly.run_id)
            record.task = asyncio.create_task(agent())
            return record

        startup = asyncio.create_task(boundary.start(assembly, resources, execute))
        try:
            await asyncio.wait_for(committing.wait(), timeout=2)
            if failure == "timeout":
                # Exercise wait_for's cancellation of an already-running startup call.
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(startup, timeout=0)
            elif failure == "cancel":
                startup.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await startup
            else:
                with pytest.raises(RuntimeError, match="startup commit failed"):
                    await startup

            assert record.task.done(), "Agent survived failed startup"
            assert agent_exited.is_set(), "Agent teardown was not awaited"
            assert record.status == RunStatus.interrupted
            assert activity._closed
            assert activity._worker.done(), "Activity journal worker survived failed startup"
            assert not hasattr(record, "loop_activity_task")
        finally:
            if not startup.done():
                startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
            if record is not None and not record.task.done():
                manager.cancel(record.run_id)
                await asyncio.gather(record.task, return_exceptions=True)
            if activity is not None:
                await activity.close()

    asyncio.run(run())
