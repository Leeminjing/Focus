r"""本文件验证 LoopSupervisor 的独立进度、故障隔离、暂停屏障和停止收敛。

输入为阻塞 round、失败 worker 与快速 Run-event 组件；输出为互不阻塞的 tick、独立 failure state、暂停后
不启动新工作及 close 后全部终止的断言。具体工作流为用 asyncio Event 驱动确定性组件而不访问数据库。
示例：`pytest backend/tests/test_loop_supervisor.py`。
"""

from __future__ import annotations

import asyncio

from backend.app.desktop.agent_loop.supervisor import LoopSupervisor, SupervisorComponent
from backend.app.desktop.agent_loop.coordinator import CoordinatorClaim, LoopCoordinatorRuntime


def test_components_progress_and_fail_independently() -> None:
    async def run() -> None:
        blocked = asyncio.Event()
        progressed = asyncio.Event()
        calls = {"fast": 0, "failed": 0}

        async def slow_round() -> None:
            await blocked.wait()

        async def fast_events() -> None:
            calls["fast"] += 1
            if calls["fast"] >= 3:
                progressed.set()

        async def failing_worker() -> None:
            calls["failed"] += 1
            raise RuntimeError("worker failed")

        supervisor = LoopSupervisor("loop-1", (
            SupervisorComponent("round", slow_round, 0.001),
            SupervisorComponent("run_events", fast_events, 0.001),
            SupervisorComponent("workers", failing_worker, 0.001),
        ))
        await supervisor.start()
        try:
            await asyncio.wait_for(progressed.wait(), timeout=1)
            assert calls["fast"] >= 3
            assert calls["failed"] >= 1
            states = {item.name: item for item in supervisor.states()}
            assert states["workers"].last_error == "worker failed"
            assert states["run_events"].last_error is None
        finally:
            await supervisor.close()
        assert all(not item.running for item in supervisor.states())

    asyncio.run(run())


def test_pause_prevents_new_ticks_and_resume_continues() -> None:
    async def run() -> None:
        calls = 0
        reached = asyncio.Event()

        async def tick() -> None:
            nonlocal calls
            calls += 1
            if calls >= 2:
                reached.set()

        supervisor = LoopSupervisor("loop-2", (SupervisorComponent("events", tick, 0.002),))
        await supervisor.start()
        try:
            await asyncio.wait_for(reached.wait(), timeout=1)
            supervisor.pause()
            await asyncio.sleep(0.01)
            paused_calls = calls
            await asyncio.sleep(0.02)
            assert calls == paused_calls
            supervisor.resume()
            await asyncio.sleep(0.01)
            assert calls > paused_calls
        finally:
            await supervisor.close()

    asyncio.run(run())


def test_runtime_admits_other_loops_while_one_round_waits() -> None:
    async def run() -> None:
        claims = [CoordinatorClaim("lease-1", "loop-1", "round-1", "fence-1"), CoordinatorClaim("lease-2", "loop-2", "round-2", "fence-2")]
        first_blocked = asyncio.Event()
        second_finished = asyncio.Event()

        class Coordinator:
            async def recover(self):
                return 0

            async def running_loop_ids(self):
                return ("loop-1", "loop-2")

            async def claim_for_loop(self, loop_id, _owner):
                return next((claims.pop(index) for index, claim in enumerate(claims) if claim.loop_id == loop_id), None)

            async def release(self, _claim):
                return True

            async def handle_run_settled(self, _event, _session):
                return None

        class RunEvents:
            async def recover(self):
                return 0

            async def drain(self, _consumer, _handler, *, loop_id=None):
                return 0

        class Orchestrator:
            async def process(self, claim):
                if claim.loop_id == "loop-1":
                    await first_blocked.wait()
                else:
                    second_finished.set()
                return "done"

        runtime = LoopCoordinatorRuntime(Coordinator(), RunEvents(), orchestrator=Orchestrator(), poll_seconds=0.001, max_concurrent_loops=2)
        await runtime.start()
        try:
            await asyncio.wait_for(second_finished.wait(), timeout=1)
            assert not first_blocked.is_set()
        finally:
            await runtime.close()

    asyncio.run(run())


def test_runtime_renews_long_claim_and_cancels_on_renewal_loss() -> None:
    async def run() -> None:
        claim = CoordinatorClaim("lease-1", "loop-1", "round-1", "fence-1")
        cancelled = asyncio.Event()

        class Coordinator:
            renewal_interval = 0.005

            def __init__(self):
                self.renewals = 0
                self.claimed = False

            async def recover(self):
                return 0

            async def running_loop_ids(self):
                return ("loop-1",)

            async def claim_for_loop(self, _loop_id, _owner):
                if not self.claimed:
                    self.claimed = True
                    return claim
                return None

            async def renew(self, _claim):
                self.renewals += 1
                return self.renewals < 3

            async def release(self, _claim):
                return True

            async def handle_run_settled(self, _event, _session):
                return None

        class RunEvents:
            async def recover(self):
                return 0

            async def drain(self, _consumer, _handler, *, loop_id=None):
                return 0

        class Orchestrator:
            async def process(self, _claim):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        coordinator = Coordinator()
        runtime = LoopCoordinatorRuntime(coordinator, RunEvents(), orchestrator=Orchestrator(), poll_seconds=0.001)
        await runtime.start()
        try:
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            assert coordinator.renewals == 3
        finally:
            await runtime.close()

    asyncio.run(run())
