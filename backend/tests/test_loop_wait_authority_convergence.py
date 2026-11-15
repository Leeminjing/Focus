"""本文件对外提供等待请求随 Loop 权威事务收敛的 PostgreSQL 回归。

输入为隔离数据库、真实等待/授权/停止入口及晚到响应；输出为旧等待失效、原子回滚和规范投影一致性的断言。
具体工作流为创建真实恢复等待并撤权，在历史实体中核对终态，
验证等待变更的锁顺序、旧请求失效和已提交回答的幂等重放。
修复前遗留数据作为显式夹具保留旧开放请求，新等待原子替换；预算恢复响应幂等重放不重复恢复或修改预算。
示例：pytest backend/tests/test_loop_wait_authority_convergence.py -q；不修改原生验收数据库。
"""

import asyncio
from datetime import UTC, datetime
import os
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.patrol.agent_loop.governance.authority_control import LoopAuthorityService
from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopRound, LoopDelegationGrant
from backend.app.desktop.patrol.agent_loop.rounds.lifecycle import create_observation_round
from backend.app.desktop.patrol.agent_loop.schemas import LoopWaitResponseRequest, RevokeLoopGrantRequest
from backend.app.desktop.patrol.agent_loop.schemas import ResumeWithCurrentMissionRequest
from backend.app.desktop.patrol.agent_loop.waits.wait_models import LoopWaitRequest, LoopWaitResponse
from backend.app.desktop.patrol.agent_loop.waits.wait_requests import LoopWaitRequestFactory, LoopWaitRequestService
from sqlalchemy import text
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop

pytestmark = pytest.mark.usefixtures("runtime_postgres_database")


async def _open_wait(sessions, fixture):
    async with sessions.begin() as session:
        loop = await session.get(AgentLoop, fixture["loop_id"], with_for_update=True)
        row = await session.get(LoopRound, loop.current_round_id)
        row.status = "error"
        return await LoopWaitRequestService().open(session, loop,
            LoopWaitRequestFactory.retry_or_stop("fixture failure"), created_by="test",
            correlation_id=uuid.uuid4().hex, round_id=row.round_id)


def _answer(request, action):
    return LoopWaitResponseRequest(request_revision=request.revision,
        idempotency_key=f"late:{request.request_id}:{action}", answer={"action": action})


def test_revocation_replaces_prior_wait_with_current_authority_wait(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="wait-revoke", started_at=datetime.now(UTC))
        try:
            wait = await _open_wait(sessions, fixture)
            await LoopAuthorityService(sessions).mutate(fixture["loop_id"], RevokeLoopGrantRequest(command="revoke"))
            active = await fixture["service"].active_wait_request(fixture["loop_id"])
            assert active["request_id"] != wait.request_id and active["kind"] == "legacy_recovery"
            async with sessions() as session:
                assert (await session.get(LoopWaitRequest, wait.request_id)).status == "superseded"
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["resolve", "cancel", "mission_recovery"])
def test_wait_mutations_block_on_loop_before_locking_request(tmp_path, operation):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="wait-lock-order", started_at=datetime.now(UTC))
        task = None
        try:
            wait = await _open_wait(sessions, fixture)

            async def mutate():
                if operation == "mission_recovery":
                    body = ResumeWithCurrentMissionRequest(confirmation="resume_with_current_mission",
                        request_revision=1, idempotency_key=f"lock-order:{wait.request_id}")
                    return await fixture["service"].resume_with_current_mission(
                        fixture["loop_id"], wait.request_id, body, actor_id="user")
                async with sessions.begin() as session:
                    if operation == "cancel":
                        return await LoopWaitRequestService().cancel(session, wait.request_id)
                    return await LoopWaitRequestService().resolve(session, wait.request_id,
                        answer={"action": "retry"}, actor_id="user", request_revision=1,
                        idempotency_key=f"lock-order:{wait.request_id}")

            async with sessions.begin() as holder:
                await holder.get(AgentLoop, fixture["loop_id"], with_for_update=True)
                holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                task = asyncio.create_task(mutate())
                deadline = asyncio.get_running_loop().time() + 10
                async with sessions.begin() as inspector:
                    while not await inspector.scalar(text(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE :holder=ANY(pg_blocking_pids(pid)))"),
                        {"holder": holder_pid}):
                        assert not task.done() and asyncio.get_running_loop().time() < deadline
                        await asyncio.sleep(0.01)
                    unlocked = await inspector.scalar(select(LoopWaitRequest).where(
                        LoopWaitRequest.request_id == wait.request_id).with_for_update(skip_locked=True))
                    assert unlocked is not None, "等待变更在等待 Loop 锁时不得已持有请求锁"
            try:
                await asyncio.wait_for(task, 10)
            except HTTPException as rejected:
                assert operation == "mission_recovery" and rejected.status_code == 409
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


async def _legacy_successor(sessions, fixture, status):
    async with sessions.begin() as session:
        loop = await session.get(AgentLoop, fixture["loop_id"], with_for_update=True)
        prior = await session.get(LoopRound, loop.current_round_id)
        successor = await create_observation_round(session, loop, prior, "0" * 64)
        session.add(successor)
        loop.current_round_id = successor.round_id
        loop.status = status
        return successor.round_id


@pytest.mark.parametrize("status", ["running", "waiting_user"])
@pytest.mark.parametrize("action", ["retry", "stop"])
def test_legacy_open_wait_cannot_control_successor_round(tmp_path, status, action):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="old-wait", started_at=datetime.now(UTC))
        try:
            wait = await _open_wait(sessions, fixture)
            successor = await _legacy_successor(sessions, fixture, status)
            with pytest.raises(HTTPException) as rejected:
                await fixture["service"].resolve_wait_request(
                    fixture["loop_id"], wait.request_id, _answer(wait, action), actor_id="user")
            assert rejected.value.status_code == 409
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                assert loop.status == status and loop.current_round_id == successor
                assert await session.scalar(select(func.count()).select_from(LoopWaitResponse).where(
                    LoopWaitResponse.request_id == wait.request_id)) == 0
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


def test_open_current_wait_supersedes_legacy_request_and_keeps_current_replay(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="old-replace", started_at=datetime.now(UTC))
        try:
            old = await _open_wait(sessions, fixture)
            successor = await _legacy_successor(sessions, fixture, "running")
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"], with_for_update=True)
                waits = LoopWaitRequestService()
                draft = LoopWaitRequestFactory.clarification("current question")
                current = await waits.open(session, loop, draft, created_by="test", correlation_id="current")
                replay = await waits.open(session, loop, draft, created_by="test", correlation_id="current-replay")
                assert current.request_id != old.request_id and current.round_id == successor
                assert replay.request_id == current.request_id
                assert (await session.get(LoopWaitRequest, old.request_id)).status == "superseded"
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


def test_committed_budget_retry_replay_cannot_repeat_recovery(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="budget-replay", started_at=datetime.now(UTC))
        service = fixture["service"]
        first = None
        try:
            for _ in range(2):
                async with sessions.begin() as session:
                    loop = await session.get(AgentLoop, fixture["loop_id"], with_for_update=True)
                    request = await LoopWaitRequestService().open(session, loop,
                        LoopWaitRequestFactory.budget_action("budget choice", {}), created_by="test",
                        correlation_id=uuid.uuid4().hex)
                body = LoopWaitResponseRequest(request_revision=1, idempotency_key=f"budget:{request.request_id}",
                    answer={"action": "retry"})
                assert (await service.resolve_wait_request(fixture["loop_id"], request.request_id, body, actor_id="user"))["created"]
                if first is None:
                    first = (request.request_id, body)
            current_round_id = (await service.get(fixture["loop_id"]))["current_round_id"]
            replay = await service.resolve_wait_request(fixture["loop_id"], *first, actor_id="user")
            assert not replay["created"]
            async with sessions() as session:
                grant = await session.scalar(select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == fixture["loop_id"], LoopDelegationGrant.status == "active"))
                loop = await session.get(AgentLoop, fixture["loop_id"])
                assert grant.budgets["max_input_tokens"] is None
                assert loop.status == "running" and loop.current_round_id == current_round_id
        finally:
            await _stop(service, fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())
