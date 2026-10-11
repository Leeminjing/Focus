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

from backend.app.desktop.agent_loop.coordinator import CoordinatorClaim, LoopCoordinatorRuntime
from backend.app.desktop.agent_loop.models import AgentLoop, LoopRound
from backend.app.desktop.agent_loop.round_consequences import RoundConsequenceReader
from backend.app.desktop.agent_loop.rounds import terminate_round
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop


def test_preemption_returns_while_finally_retains_the_cognition_barrier():
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
