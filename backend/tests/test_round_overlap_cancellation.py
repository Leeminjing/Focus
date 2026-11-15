"""本文件对外提供重叠轮取消通知与授权前终止证明的定向回归。

输入为可控认知 finally、真实 PostgreSQL Round；输出为及时受理、清理屏障及历史缺权威诊断断言。
工作流为保持旧认知清理挂起并重复取消，再释放真实清理；另核对裸终态不能替代耐久终止证明。
示例：pytest backend/tests/test_round_overlap_cancellation.py。
"""

import asyncio
from datetime import UTC, datetime
import os
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.patrol.agent_loop.coordinator import CoordinatorClaim, LoopCoordinator, LoopCoordinatorRuntime
from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopRound, LoopPatrolAttempt
from backend.app.desktop.patrol.agent_loop.rounds.round_consequences import RoundConsequenceReader
from backend.app.desktop.patrol.agent_loop.rounds.lifecycle import terminate_round
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop


def test_preemption_returns_while_old_cognition_cleanup_is_tracked():
    async def run():
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        released = []

        async def release_claim(claim):
            released.append(claim.lease_id)

        async def work(claim):
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        runtime = LoopCoordinatorRuntime(SimpleNamespace(release=release_claim), SimpleNamespace())
        runtime._perform_claim = work
        claim = CoordinatorClaim("lease", "loop", "round", "1", "v1")
        task = asyncio.create_task(runtime._execute_claim(claim))
        await started.wait()
        payload = {"round_id": "round", "fencing_token": 2, "worker_request_ids": []}
        assert not await asyncio.wait_for(runtime.preempt_observation(payload), 1)
        await cleaning.wait()
        assert not task.done() and not released
        assert not await asyncio.wait_for(runtime.preempt_observation(payload), 1)
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, 1)
        assert released == ["lease"] and not runtime._claims

    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_claim_new_observation_while_old_attempt_and_cancellation_outbox_remain_unsettled(tmp_path):
    async def run():
        from backend.app.desktop.patrol.agent_loop.observations.observation_capture import LoopObservationService
        from backend.app.desktop.patrol.agent_loop.patrol import PortfolioPatrol
        from backend.tests.test_round_task_progress import _Checkpointer

        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="cleanup", started_at=datetime.now(UTC))
        coordinator = LoopCoordinator(sessions)
        claims = []
        try:
            observations = LoopObservationService(sessions, _Checkpointer())
            first = await observations.capture(fixture["loop_id"], fixture["round_id"])
            old = await coordinator.claim_for_loop(fixture["loop_id"], "old-owner")
            claims.append(old)
            patrol = PortfolioPatrol(sessions, object())
            attempt = await patrol._begin(first, fixture["snapshot"]["holder_id"], fencing_token=int(old.fencing_token))
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"], with_for_update=True)
                row = await session.get(LoopRound, fixture["round_id"], with_for_update=True)
                second = await observations.freeze_version(session, loop, row, first)
            current = await coordinator.claim_for_loop(fixture["loop_id"], "new-owner")
            assert current is not None and current.observation_id == second.observation_id
            claims.append(current)
            assert int(current.fencing_token) > int(old.fencing_token)
            async with sessions() as session:
                old_attempt = await session.get(LoopPatrolAttempt, attempt.patrol_attempt_id)
                assert old_attempt.status == "superseded" and old_attempt.completed_at is None
            assert not await coordinator.release(old)
            await patrol._finish(attempt, "error", {}, "cancelled")
        finally:
            for claim in claims:
                await coordinator.release(claim)
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_undecided_terminal_round_requires_durable_preauthorization_proof(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="term-proof", started_at=datetime.now(UTC))
        try:
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"], with_for_update=True)
                row = await session.get(LoopRound, loop.current_round_id)
                row.status = "error"
                await session.flush()
                missing = await RoundConsequenceReader().read(session, row)
                assert not missing.stable and "missing_patrol_decision" in missing.diagnostics
                row.status = "observed"
                assert await terminate_round(session, loop, row, category="test", reason="explicit preauthorization stop",
                    allowed_statuses={"observed"}, wait_for_user=False)
                await session.flush()
                terminated = await RoundConsequenceReader().read(session, row)
                assert terminated.stable and not terminated.diagnostics
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())
