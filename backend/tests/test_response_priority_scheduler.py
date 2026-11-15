"""请求级公平、取消/用量隔离及同轮新 Observation 准入回归。

事件屏障控制真实请求入口的顺序，不以固定延迟或 Coordinator 领取顺序代替请求优先级。
"""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from backend.app.desktop.patrol.agent_loop.coordinator import CoordinatorClaim, LoopCoordinatorRuntime
from backend.app.desktop.patrol.agent_loop.resources.request_admission import RequestAdmission
from backend.app.desktop.patrol.agent_loop.rounds.round_orchestration import LoopRoundOrchestrator, StructuredPatrolDecisionModel
from backend.tests.config_helpers import app_config_for


class Reply(BaseModel):
    value: str


def _model(admission, *, priority=False):
    model = StructuredPatrolDecisionModel(app_config_for("scheduler-test", None))
    model.bind_request_admission(admission, priority=priority)
    return model


@pytest.mark.parametrize("concurrency", [1, 4])
def test_actual_patrol_requests_prioritize_users_and_bound_background_wait(concurrency):
    async def run():
        admission = RequestAdmission(concurrency)
        release, started = asyncio.Event(), asyncio.Event()
        order = []
        active = 0
        peak = 0

        class Provider:
            def __init__(self, label):
                self.label = label

            async def ainvoke(self, *args, **kwargs):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                order.append(self.label)
                if len(order) == concurrency:
                    started.set()
                await release.wait()
                await asyncio.sleep(0)
                active -= 1
                return SimpleNamespace(content='{"value":"ok"}')

        async def request(label, priority=False):
            return await _model(admission, priority=priority)._invoke_once(Provider(label), [], "prompt_json", Reply)

        holders = [asyncio.create_task(request(f"holder-{i}")) for i in range(concurrency)]
        await started.wait()
        background = [asyncio.create_task(request(f"background-{i}")) for i in range(16)]
        await asyncio.sleep(0)
        users = [asyncio.create_task(request(f"user-{i}", True)) for i in range(64)]
        await asyncio.sleep(0)
        assert order == [f"holder-{i}" for i in range(concurrency)]
        release.set()
        await asyncio.wait_for(asyncio.gather(*holders, *background, *users), 5)
        assert peak == concurrency
        assert order[concurrency:concurrency + 3] == ["user-0", "user-1", "user-2"]
        assert [item for item in order if item.startswith("background-")] == [f"background-{i}" for i in range(16)]
        streak = 0
        for label in order[concurrency:order.index("background-15") + 1]:
            streak = streak + 1 if label.startswith("user-") else 0
            assert streak <= 3

    asyncio.run(run())


@pytest.mark.parametrize("cancel_after_grant", [False, True])
def test_cancelled_queued_request_releases_capacity_without_consuming_budget(cancel_after_grant):
    async def run():
        admission = RequestAdmission(1)
        reserved = []

        class Receipts:
            async def reserve(self, *args, **kwargs):
                reserved.append(True)

        class Provider:
            async def ainvoke(self, *args, **kwargs):
                raise AssertionError("cancelled request must not reach provider")

        model = _model(admission, priority=True)
        model.bind_usage_receipts(Receipts())
        async with admission.slot():
            task = asyncio.create_task(model._invoke_once(Provider(), [], "prompt_json", Reply))
            await asyncio.sleep(0)
            if not cancel_after_grant:
                task.cancel()
        # The waiter is granted synchronously by __aexit__, before resuming.
        if cancel_after_grant:
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        async with asyncio.timeout(1):
            async with admission.slot():
                pass
        assert not reserved and model.call_count == model.usage.model_calls == 0
        assert model.attempt_metadata == []

    asyncio.run(run())


def test_request_settlement_does_not_hold_decision_capacity():
    async def run():
        admission = RequestAdmission(1)
        settling, release = asyncio.Event(), asyncio.Event()
        called = []

        class Receipts:
            async def reserve(self, *args, **kwargs):
                return "receipt"

            async def settle(self, receipt, measured, reported):
                settling.set()
                await release.wait()

        class Provider:
            async def ainvoke(self, *args, **kwargs):
                called.append(True)
                return SimpleNamespace(content='{"value":"ok"}')

        old_model = _model(admission)
        old_model.bind_usage_receipts(Receipts())
        old = asyncio.create_task(old_model._invoke_once(Provider(), [], "prompt_json", Reply))
        try:
            await asyncio.wait_for(settling.wait(), 1)
            result = await asyncio.wait_for(_model(admission, priority=True)._invoke_once(Provider(), [], "prompt_json", Reply), 1)
            assert result.value == "ok" and len(called) == 2
            assert not old.done()
        finally:
            release.set()
            await old
        assert old_model.usage.model_calls == 1

    asyncio.run(run())


def test_saturated_background_preparation_does_not_take_patrol_request_capacity():
    async def run():
        orchestrator = object.__new__(LoopRoundOrchestrator)
        runtime = LoopCoordinatorRuntime(None, None, orchestrator=orchestrator,
                                         max_concurrent_loops=1, max_background_preparations=1)
        started, release, next_started = (asyncio.Event() for _ in range(3))

        async def slow_preparation():
            started.set()
            await release.wait()

        async def next_preparation():
            next_started.set()

        class Provider:
            async def ainvoke(self, *args, **kwargs):
                return SimpleNamespace(content='{"value":"ok"}')

        first = asyncio.create_task(orchestrator._prepare(slow_preparation))
        await started.wait()
        second = asyncio.create_task(orchestrator._prepare(next_preparation))
        await asyncio.sleep(0)
        try:
            assert not next_started.is_set()
            result = await asyncio.wait_for(_model(runtime._decision_requests, priority=True)._invoke_once(
                Provider(), [], "prompt_json", Reply), 1)
            assert result.value == "ok" and not next_started.is_set()
        finally:
            release.set()
            await asyncio.gather(first, second)
            await runtime.close()
        assert next_started.is_set()

    asyncio.run(run())


def test_superseded_preparation_cleanup_does_not_block_new_preparation():
    async def run():
        orchestrator = object.__new__(LoopRoundOrchestrator)
        orchestrator.bind_preparation_admission(RequestAdmission(1))
        started, cleaning, release, next_started = (asyncio.Event() for _ in range(4))

        async def old_preparation():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        async def new_preparation():
            next_started.set()

        old = asyncio.create_task(orchestrator._prepare(old_preparation))
        try:
            await started.wait()
            old.cancel()
            await asyncio.wait_for(cleaning.wait(), 1)
            await asyncio.wait_for(orchestrator._prepare(new_preparation), 1)
            assert next_started.is_set() and not old.done()
        finally:
            release.set()
            await asyncio.gather(old, return_exceptions=True)

    asyncio.run(run())


def test_new_observation_runs_while_old_same_round_cleanup_is_pending():
    async def run():
        started, cleaning, release, new_started = (asyncio.Event() for _ in range(4))
        claims = [CoordinatorClaim("old", "loop", "round", "1", "o1"),
                  CoordinatorClaim("new", "loop", "round", "2", "o2")]
        released = []

        class Coordinator:
            async def claim_for_loop(self, *args):
                return claims.pop(0) if claims else None

            async def release(self, claim):
                released.append(claim.lease_id)

        class Orchestrator:
            async def process(self, claim):
                if claim.observation_id == "o2":
                    new_started.set()
                    return True
                try:
                    started.set()
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()

        runtime = LoopCoordinatorRuntime(Coordinator(), None, orchestrator=Orchestrator(), max_concurrent_loops=1)
        try:
            await runtime._process_round("loop")
            await asyncio.wait_for(started.wait(), 1)
            assert not await runtime.preempt_observation({"round_id": "round", "fencing_token": 2})
            await cleaning.wait()
            await runtime._process_round("loop")
            await asyncio.wait_for(new_started.wait(), 1)
            assert "old" not in released and not release.is_set()
        finally:
            release.set()
            await runtime.close()
        assert sorted(released) == ["new", "old"] and not runtime._claims

    asyncio.run(run())


def test_stopped_loop_cancels_detached_claim_without_repeatedly_interrupting_cleanup():
    async def run():
        started, cleaning, release = (asyncio.Event() for _ in range(3))
        released = []
        claim = CoordinatorClaim("lease", "loop", "round", "1")

        class Coordinator:
            async def claim_for_loop(self, *args):
                return claim

            async def running_loop_ids(self):
                return ()

            async def release(self, identity):
                released.append(identity)

        class Orchestrator:
            async def process(self, identity):
                try:
                    started.set()
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()

        runtime = LoopCoordinatorRuntime(Coordinator(), None, orchestrator=Orchestrator())
        try:
            await runtime._process_round("loop")
            await asyncio.wait_for(started.wait(), 1)
            await runtime._reconcile_supervisors()
            await asyncio.wait_for(cleaning.wait(), 1)
            await runtime._reconcile_supervisors()
            assert len(runtime._round_tasks) == 1 and not released
        finally:
            release.set()
            await runtime.close()
        assert released == [claim] and not runtime._round_tasks

    asyncio.run(run())
