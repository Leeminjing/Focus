"""全局 Context 队列公平、独立结算和高优先 Writer 工作区互斥的 PostgreSQL 验收。"""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.patrol.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.patrol.agent_loop.resources.context_run_pool import ContextRunPool
from backend.app.desktop.patrol.agent_loop.execution.dispatch import LoopWaveDispatcher
from backend.app.desktop.patrol.agent_loop.models import LoopDirective, LoopUserIntent
from backend.app.desktop.persistence.models import DesktopRun
from backend.app.desktop.workspaces.coordination import WorkspaceLeaseManager, WorkspaceLeaseRequest, WorkspaceSlot
from backend.tests.test_loop_runtime_pools import _create_wave


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


async def _user_priority(sessions, snapshot):
    async with sessions.begin() as session:
        session.add(LoopUserIntent(intent_id=uuid.uuid4().hex, loop_id=snapshot["loop_id"],
            scope="portfolio", content="New user input", status="observed", correlation_id=uuid.uuid4().hex,
            goal_revision=1, authority_revision=1, observed_round_id=snapshot["current_round_id"]))


def test_scoped_wakes_use_global_user_priority_without_starving_background(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixtures = []
        calls = []

        class Coordinator:
            async def dispatch_ready(self, claim, dispatcher, capacity):
                calls.append(claim.loop_id)
                assert capacity == 1
                return ()

        pool = ContextRunPool(sessions, Coordinator(), object(), concurrency=1)
        try:
            for index in range(9):
                fixture = await _create_wave(sessions, tmp_path, count=1)
                fixtures.append(fixture)
                if index:
                    await _user_priority(sessions, fixture[1])
            background = fixtures[0][1]["loop_id"]
            user_ids = {item[1]["loop_id"] for item in fixtures[1:]}
            for _ in range(24):
                # Even a background-only wake must notice a newer user Round.
                assert await pool.drain(background) == 1
                await asyncio.gather(*(task for _, _, task in pool._tasks.values()))
            assert all(identity in user_ids for identity in calls[:3])
            assert calls[3::4] == [background] * 6
            assert set(calls) == user_ids | {background}
        finally:
            await pool.close()
            for service, snapshot, _, _ in fixtures:
                await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("shell_cleanup_pending", [False, True])
def test_terminal_old_run_only_blocks_new_writer_for_actual_workspace_conflict(tmp_path, shell_cleanup_pending):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, contexts, directive_ids = await _create_wave(sessions, tmp_path, count=1, writing=True)
        old_id, new_id = uuid.uuid4().hex, uuid.uuid4().hex
        launched = []
        leases = WorkspaceLeaseManager(sessions)
        old_lease = None

        async def launch(directive, message, slot_id):
            # Exercise the actual final lease boundary even if planning races.
            grant = await leases.acquire(WorkspaceLeaseRequest(slot_id=slot_id, run_id=new_id, mode="write"))
            launched.append(grant)
            return new_id

        pool = ContextRunPool(sessions, LoopCoordinator(sessions), LoopWaveDispatcher(sessions, launch), concurrency=1)
        try:
            await _user_priority(sessions, snapshot)
            async with sessions.begin() as session:
                slot = await session.scalar(select(WorkspaceSlot).where(
                    WorkspaceSlot.workspace_id == snapshot["workspace_id"], WorkspaceSlot.kind == "authoritative"))
                for run_id in (old_id, new_id):
                    session.add(DesktopRun(run_id=run_id, task_id=contexts[0][0], agent_id="writer", kind="main",
                        status="interrupted", origin="delegated_patrol", execution_thread_id=f"execution-{run_id}",
                        loop_id=snapshot["loop_id"], round_id=snapshot["current_round_id"],
                        workspace_anchor={"slot_id": slot.slot_id} if run_id == old_id else {},
                        workspace_result={"shell_cleanup": {"confirmed": False}} if run_id == old_id and shell_cleanup_pending else {}))
            old_lease = await leases.acquire(WorkspaceLeaseRequest(slot_id=slot.slot_id, run_id=old_id, mode="write")) if not shell_cleanup_pending else None
            # Terminal, unsettled usage is outside Context execution capacity.
            assert await pool._active_run_count() == 0
            await asyncio.gather(*(pool.drain(snapshot["loop_id"]) for _ in range(32)))
            await asyncio.gather(*(task for _, _, task in pool._tasks.values()))
            assert not launched
            async with sessions() as session:
                directive = await session.get(LoopDirective, directive_ids[0])
                assert directive.status == "created" and directive.attempt == 0
            if old_lease is not None:
                await leases.release(old_lease.lease_id, old_lease.fencing_token)
                old_lease = None
            async with sessions.begin() as session:
                old = await session.get(DesktopRun, old_id)
                old.workspace_result = {}
                # Deliberately leave settled_at NULL: model accounting still
                # has work to do after physical workspace ownership is free.
            assert await pool.drain(snapshot["loop_id"]) == 1
            await asyncio.gather(*(task for _, _, task in pool._tasks.values()))
            assert len(launched) == 1
        finally:
            await pool.close()
            if old_lease is not None:
                await leases.release(old_lease.lease_id, old_lease.fencing_token)
            for grant in launched:
                await leases.release(grant.lease_id, grant.fencing_token)
            await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())
