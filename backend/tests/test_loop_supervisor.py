r"""本文件对外提供 LoopSupervisor 的独立进度、故障隔离、暂停屏障和停止收敛回归。

输入为阻塞 round、失败 worker 与快速 Run-event 组件；输出为互不阻塞的 tick、独立 failure state、暂停后
不启动新工作及 close 后全部终止的断言。具体工作流为用 asyncio Event 和可推进的事件循环时钟驱动确定性组件，
核对配置续租检查期限内的取消与唯一释放，而不访问数据库。
示例：`pytest backend/tests/test_loop_supervisor.py`。
"""

from __future__ import annotations

import asyncio

from backend.app.desktop.agent_loop.supervisor import LoopSupervisor, SupervisorComponent
from backend.app.desktop.agent_loop.coordinator import CoordinatorClaim, LoopCoordinatorRuntime


def test_workspace_cleanup_runs_without_active_loop_supervisors():
    async def run():
        cleaned = asyncio.Event()
        class Coordinator:
            async def recover(self):
                pass
            async def running_loop_ids(self):
                return ()
        class RunEvents:
            async def recover(self):
                pass
        class Cleanup:
            async def drain(self):
                cleaned.set()
        runtime = LoopCoordinatorRuntime(Coordinator(), RunEvents(), workspace_cleanup=Cleanup())
        async def no_preemptions():
            return 0
        runtime._drain_preemptions = no_preemptions
        await runtime.start()
        try:
            await asyncio.wait_for(cleaned.wait(), 1)
            assert runtime.supervised_loop_ids == ()
            assert "workspace_cleanup" in {row["name"] for row in runtime.diagnostic_snapshot()["components"]}
        finally:
            await runtime.close()
    asyncio.run(run())


def test_wake_during_tick_is_retained_coalesced_and_targeted():
    async def run():
        entered, release, repeated = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = {"rounds": 0, "workers": 0}

        async def rounds():
            calls["rounds"] += 1
            if calls["rounds"] == 1:
                entered.set()
                await release.wait()
            else:
                repeated.set()

        async def workers():
            calls["workers"] += 1

        supervisor = LoopSupervisor("test", (
            SupervisorComponent("rounds", rounds, 30),
            SupervisorComponent("workers", workers, 30),
        ))
        await supervisor.start()
        try:
            await asyncio.wait_for(entered.wait(), 1)
            for _ in range(100):
                supervisor.wake("rounds")
            release.set()
            await asyncio.wait_for(repeated.wait(), 1)
            for _ in range(20):
                await asyncio.sleep(0)
            assert calls == {"rounds": 2, "workers": 1}
        finally:
            await supervisor.close()

    asyncio.run(run())


def test_wake_respects_pause_and_failure_backoff():
    async def run():
        entered, retried = asyncio.Event(), asyncio.Event()
        calls = 0

        async def tick():
            nonlocal calls
            calls += 1
            entered.set()
            if calls == 1:
                raise RuntimeError("retry")
            retried.set()

        supervisor = LoopSupervisor("test", (SupervisorComponent("events", tick, 0.05),))
        await supervisor.start()
        try:
            await entered.wait()
            for _ in range(50):
                supervisor.wake("events")
                await asyncio.sleep(0)
            assert calls == 1
            supervisor.pause()
            await asyncio.sleep(0.12)
            assert calls == 1
            supervisor.resume()
            await asyncio.wait_for(retried.wait(), 1)
        finally:
            await supervisor.close()

    asyncio.run(run())


def test_polling_recovers_without_notification():
    async def run():
        progressed = asyncio.Event()
        calls = 0

        async def tick():
            nonlocal calls
            calls += 1
            if calls == 2:
                progressed.set()

        supervisor = LoopSupervisor("test", (SupervisorComponent("events", tick, 0.01),))
        await supervisor.start()
        try:
            await asyncio.wait_for(progressed.wait(), 1)
        finally:
            await supervisor.close()

    asyncio.run(run())


def test_new_loop_is_admitted_by_committed_hint_without_registry_poll():
    async def run():
        desired = []
        checked, admitted = asyncio.Event(), asyncio.Event()

        class Coordinator:
            async def recover(self):
                pass

            async def running_loop_ids(self):
                checked.set()
                return tuple(desired)

            async def claim_for_loop(self, identity, _owner):
                assert identity == "new-loop"
                admitted.set()
                return None

        class RunEvents:
            async def recover(self):
                pass

            async def drain(self, *_args, **_kwargs):
                return 0

        runtime = LoopCoordinatorRuntime(Coordinator(), RunEvents(), poll_seconds=30)

        async def no_preemptions():
            return 0

        runtime._drain_preemptions = no_preemptions
        await runtime.start()
        try:
            checked.clear()
            await asyncio.wait_for(checked.wait(), 1)
            desired.append("new-loop")
            runtime._work_committed('{"loop_id":"new-loop","components":["rounds"]}')
            await asyncio.wait_for(admitted.wait(), 1)
            assert runtime.supervised_loop_ids == ("new-loop",)
        finally:
            await runtime.close()

    asyncio.run(run())


def test_index_prewarm_is_supervised_and_cancelled_independently():
    async def run():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class Orchestrator:
            async def prewarm_indexes(self, loop_id):
                assert loop_id == "loop-1"
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        runtime = LoopCoordinatorRuntime(None, None, orchestrator=Orchestrator())
        component = next(c for c in runtime._components_for_loop("loop-1") if c.name == "index_prewarm")
        supervisor = LoopSupervisor("test", [component])
        await supervisor.start()
        await asyncio.wait_for(entered.wait(), 1)
        await supervisor.close()
        assert cancelled.is_set()

    asyncio.run(run())


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


def test_simulated_clock_cancellation_closes_within_configured_renewal_window(monkeypatch) -> None:
    async def run():
        loop = asyncio.get_running_loop()
        clock = [loop.time()]
        started = asyncio.Event()
        cancelled = asyncio.Event()
        released = []
        claim = CoordinatorClaim("lease", "loop", "round", "fence")

        class Coordinator:
            renewal_interval = 3.0

            async def renew(self, identity):
                assert identity == claim
                return False

            async def release(self, identity):
                released.append(identity)

        class Orchestrator:
            async def process(self, identity):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        runtime = LoopCoordinatorRuntime(Coordinator(), None, orchestrator=Orchestrator(), poll_seconds=0.1)
        with monkeypatch.context() as patch:
            patch.setattr(loop, "time", lambda: clock[0])
            task = asyncio.create_task(runtime._execute_claim(claim))
            try:
                await started.wait()
                await asyncio.sleep(0)
                began = clock[0]
                clock[0] += Coordinator.renewal_interval + 0.001
                for _ in range(20):
                    await asyncio.sleep(0)
                    if task.done():
                        break
                assert task.done() and cancelled.is_set()
                assert released == [claim]
                assert clock[0] - began <= Coordinator.renewal_interval + 0.1
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
