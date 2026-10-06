"""本文件对外提供 Coordinator 候选快照与新租约提交竞争的 PostgreSQL 回归。
输入为隔离数据库中的两条真实 Round、生产领取事务和事务级 advisory 屏障；输出为顺延领取及拒绝覆盖已有租约断言。
具体工作流为让第二候选 SELECT 已建立快照但尚未锁 Round，第一协调者提交租约后释放屏障，再核对第二领取的后继候选。
示例：pytest backend/tests/test_coordinator_claim_snapshot_race.py -q；使用精确锁等待证据，不依赖固定睡眠或替换租约查询。
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import String, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.agent_loop.models import LoopCoordinatorLease
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop

pytestmark = pytest.mark.usefixtures("runtime_postgres_database")


async def _wait_for_barrier(connection, identity):
    async with asyncio.timeout(5):
        while not await connection.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' AND objid=:identity AND NOT granted)"), {"identity": identity}):
            await asyncio.sleep(0)


@pytest.mark.parametrize("scoped", [False, True])
def test_claim_rechecks_new_lease_and_continues_to_next_unleased_round(tmp_path, scoped):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        identity = int(uuid.uuid4().hex[:7], 16)
        first = second = pending = None

        class SnapshotCoordinator(LoopCoordinator):
            @staticmethod
            def _candidate_statement(now, loop_id=None):
                return LoopCoordinator._candidate_statement(now, loop_id).where(func.pg_advisory_xact_lock(identity).cast(String) == "")

        try:
            first = await _seed_loop(sessions, tmp_path, label="snapshot-a", started_at=datetime.now(UTC) - timedelta(hours=3))
            second = await _seed_loop(sessions, tmp_path, label="snapshot-b", started_at=datetime.now(UTC) - timedelta(hours=2))
            async with engine.connect() as barrier:
                await barrier.execute(select(func.pg_advisory_lock(identity)))
                try:
                    coordinator = SnapshotCoordinator(sessions)
                    pending = asyncio.create_task(coordinator.claim_for_loop(first["loop_id"], "snapshot-reader") if scoped else coordinator.claim("snapshot-reader"))
                    await _wait_for_barrier(barrier, identity)
                    committed = await LoopCoordinator(sessions).claim("first-owner")
                    assert committed is not None and committed.round_id == first["round_id"]
                finally:
                    await barrier.execute(select(func.pg_advisory_unlock(identity)))
                claimed = await asyncio.wait_for(pending, 5)
                assert (claimed is None) if scoped else (claimed is not None and claimed.round_id == second["round_id"])
                async with sessions() as session:
                    leases = tuple((await session.scalars(select(LoopCoordinatorLease))).all())
                    owners = {row.round_id: row.owner_id for row in leases}
                    assert owners[first["round_id"]] == "first-owner"
                    assert owners.get(second["round_id"]) == (None if scoped else "snapshot-reader")
                    assert len(leases) == (1 if scoped else 2)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            for fixture in (first, second):
                if fixture is not None:
                    await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())
