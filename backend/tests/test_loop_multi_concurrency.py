r"""本文件验证多 Loop 与单 Loop 内各监督类别的独立并发和长时收敛。

输入为生产 LoopCoordinatorRuntime、两个活动 Loop、受控阻塞的 Patrol/Context/发布组件、快速 Run/Fact/Curator 组件和有界失败；
输出为真实 per-Loop Supervisor 拓扑、跨 Loop 公平进展、单 Loop 内互不阻塞及重启收敛断言。具体工作流为通过生产 registry
按 Loop 装配组件，并用 asyncio Event 驱动确定性端口。示例：`pytest backend/tests/test_loop_multi_concurrency.py`。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from backend.app.desktop.agent_loop.supervisor import LoopSupervisor, SupervisorComponent
from backend.app.desktop.agent_loop.coordinator import CoordinatorClaim, LoopCoordinatorRuntime


def test_production_runtime_builds_one_scoped_supervisor_per_active_loop() -> None:
    async def run() -> None:
        calls: dict[str, int] = defaultdict(int)
        blocked = asyncio.Event()
        reached = asyncio.Event()
        claims = {
            "loop-a": CoordinatorClaim("lease-a", "loop-a", "round-a", "1"),
            "loop-b": CoordinatorClaim("lease-b", "loop-b", "round-b", "1"),
        }

        class Coordinator:
            async def recover(self):
                return 0

            async def running_loop_ids(self):
                return ("loop-a", "loop-b")

            async def claim_for_loop(self, loop_id, _owner):
                return claims.pop(loop_id, None)

            async def release(self, _claim):
                return True

            async def handle_run_settled(self, _event, _session):
                return None

        class RunEvents:
            async def recover(self):
                return 0

            async def drain(self, _consumer, _handler, *, loop_id=None):
                calls[f"run:{loop_id}"] += 1
                _signal()
                return 0

        class ScopedQueue:
            def __init__(self, name):
                self.name = name

            async def drain(self, loop_id=None):
                calls[f"{self.name}:{loop_id}"] += 1
                _signal()
                return 0

            async def close(self):
                return None

            async def recover(self):
                return 0

        class Facts(ScopedQueue):
            async def reconcile(self):
                return 0

            async def project_loop(self, loop_id):
                return await self.drain(loop_id)

        class Orchestrator:
            async def process(self, claim):
                calls[f"round:{claim.loop_id}"] += 1
                if claim.loop_id == "loop-a":
                    await blocked.wait()
                _signal()
                return "done"

        def _signal():
            required = tuple(f"{name}:{loop_id}" for loop_id in ("loop-a", "loop-b") for name in ("run", "workers", "publication", "contexts", "facts"))
            if all(calls[item] for item in required) and calls["round:loop-b"]:
                reached.set()

        runtime = LoopCoordinatorRuntime(
            Coordinator(),
            RunEvents(),
            orchestrator=Orchestrator(),
            workers=ScopedQueue("workers"),
            portfolio_publications=ScopedQueue("publication"),
            context_runs=ScopedQueue("contexts"),
            fact_projector=Facts("facts"),
            poll_seconds=0.001,
            max_concurrent_loops=2,
        )
        await runtime.start()
        try:
            await asyncio.wait_for(reached.wait(), timeout=1)
            assert runtime.supervised_loop_ids == ("loop-a", "loop-b")
            assert not blocked.is_set()
            assert all(calls[f"{name}:loop-a"] for name in ("run", "workers", "publication", "contexts", "facts"))
        finally:
            blocked.set()
            await runtime.close()

    asyncio.run(run())


def test_two_loops_and_all_within_loop_work_categories_progress_independently() -> None:
    async def run() -> None:
        counts: dict[str, int] = defaultdict(int)
        publication_release = asyncio.Event()
        context_release = asyncio.Event()
        reached = asyncio.Event()

        def fast(name: str):
            async def tick() -> None:
                counts[name] += 1
                required = ("loop-a:run_events", "loop-a:facts", "loop-a:curators", "loop-b:run_events", "loop-b:facts", "loop-b:curators", "loop-b:publication", "loop-b:contexts")
                if all(counts[item] >= 3 for item in required):
                    reached.set()
            return tick

        async def blocked_publication() -> None:
            counts["loop-a:publication"] += 1
            await publication_release.wait()

        async def blocked_context() -> None:
            counts["loop-a:contexts"] += 1
            await context_release.wait()

        supervisors = (
            LoopSupervisor("loop-a", (
                SupervisorComponent("run_events", fast("loop-a:run_events"), 0.001),
                SupervisorComponent("facts", fast("loop-a:facts"), 0.001),
                SupervisorComponent("curators", fast("loop-a:curators"), 0.001),
                SupervisorComponent("contexts", blocked_context, 0.001),
                SupervisorComponent("publication", blocked_publication, 0.001),
            )),
            LoopSupervisor("loop-b", tuple(SupervisorComponent(name, fast(f"loop-b:{name}"), 0.001) for name in ("run_events", "facts", "curators", "contexts", "publication"))),
        )
        for supervisor in supervisors:
            await supervisor.start()
        try:
            await asyncio.wait_for(reached.wait(), timeout=1)
            assert counts["loop-a:publication"] == 1
            assert counts["loop-a:contexts"] == 1
            assert counts["loop-a:run_events"] >= 3
            assert counts["loop-a:facts"] >= 3
            assert counts["loop-a:curators"] >= 3
            assert all(counts[f"loop-b:{name}"] >= 3 for name in ("run_events", "facts", "curators", "contexts", "publication"))
        finally:
            publication_release.set()
            context_release.set()
            await asyncio.gather(*(supervisor.close() for supervisor in supervisors))

    asyncio.run(run())


def test_supervisor_soak_bounds_backpressure_and_recovers_after_restart() -> None:
    async def run() -> None:
        queue: asyncio.Queue[int] = asyncio.Queue(maxsize=32)
        produced = 0
        consumed: set[int] = set()
        projected: set[int] = set()
        publication_failures = 0
        completed = asyncio.Event()
        restart_boundary = asyncio.Event()

        async def produce() -> None:
            nonlocal produced
            if produced >= 240 or queue.full():
                return
            produced += 1
            queue.put_nowait(produced)
            if produced >= 64:
                restart_boundary.set()

        async def consume() -> None:
            if not queue.empty():
                identity = queue.get_nowait()
                consumed.add(identity)
                projected.add(identity)
                queue.task_done()
            if produced >= 240 and queue.empty():
                completed.set()

        async def flaky_publication() -> None:
            nonlocal publication_failures
            publication_failures += 1
            if publication_failures % 17 == 0:
                raise RuntimeError("bounded publication failure")

        components = (
            SupervisorComponent("producer", produce, 0.001),
            SupervisorComponent("run_events", consume, 0.001),
            SupervisorComponent("fact_projection", consume, 0.001),
            SupervisorComponent("publication", flaky_publication, 0.001),
        )
        first = LoopSupervisor("soak-1", components)
        await first.start()
        await asyncio.wait_for(restart_boundary.wait(), timeout=2)
        await first.close()
        second = LoopSupervisor("soak-2", components)
        await second.start()
        try:
            await asyncio.wait_for(completed.wait(), timeout=3)
            assert queue.qsize() <= 32
            assert consumed == projected
            assert len(consumed) == len(set(consumed))
            assert len(consumed) == produced
            assert publication_failures > 17
            states = {item.name: item for item in second.states()}
            assert states["run_events"].last_error is None
            assert states["fact_projection"].last_error is None
        finally:
            await second.close()

    asyncio.run(run())
