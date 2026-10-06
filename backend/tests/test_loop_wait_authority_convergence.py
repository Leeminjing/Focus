"""本文件对外提供等待请求随 Loop 权威事务收敛的 PostgreSQL 回归。

输入为隔离数据库、真实等待/用户消息/授权/停止入口及晚到响应；输出为旧等待失效、原子回滚和规范投影一致性的断言。
具体工作流为创建真实恢复等待，受理新消息或撤权，在历史实体和 journal 中核对终态，
拒绝旧重试/停止且不改变当前事务；异常注入验证全部事实回滚，并发控制只允许合法串行结果。
修复前遗留数据作为显式夹具保留旧开放请求，新等待原子替换；已提交预算响应的重放不能覆盖后续真实预算选择。
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

from backend.app.desktop.agent_loop.authority_control import LoopAuthorityService
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.agent_loop.live_projection_projector import LoopLiveSnapshotProjector
from backend.app.desktop.agent_loop.models import AgentLoop, LoopRound, LoopUserIntent, LoopDelegationGrant
from backend.app.desktop.agent_loop.rounds import create_observation_round
from backend.app.desktop.agent_loop.schemas import LoopWaitResponseRequest, RevokeLoopGrantRequest
from backend.app.desktop.agent_loop.schemas import ResumeWithCurrentMissionRequest
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.wait_models import LoopWaitRequest, LoopWaitResponse
from backend.app.desktop.agent_loop.wait_requests import LoopWaitRequestFactory, LoopWaitRequestService
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


@pytest.mark.parametrize("action", ["retry", "stop"])
def test_new_message_supersedes_wait_and_rejects_late_action(tmp_path, action):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="wait-message", started_at=datetime.now(UTC))
        service = fixture["service"]
        try:
            wait = await _open_wait(sessions, fixture)
            accepted = await service.user_message(fixture["context_id"], "new authorized input")
            snapshot = await service.get(fixture["loop_id"])
            assert snapshot["status"] == "running" and snapshot["wait_request"] is None
            with pytest.raises(HTTPException) as rejected:
                await service.resolve_wait_request(fixture["loop_id"], wait.request_id, _answer(wait, action), actor_id="user")
            assert rejected.value.status_code == 409
            assert rejected.value.detail["code"] == "wait_request_conflict"
            async with sessions() as session:
                stored = await session.get(LoopWaitRequest, wait.request_id)
                loop = await session.get(AgentLoop, fixture["loop_id"])
                projected = await LoopLiveSnapshotProjector().project(session, loop.loop_id)
                assert stored.status == "superseded" and stored.revision == wait.revision + 1
                assert stored.resolved_at is not None and stored.round_id == fixture["round_id"]
                assert loop.status == "running" and loop.current_round_id == accepted["round_id"]
                assert projected.wait_requests[wait.request_id].state["status"] == "superseded"
                assert await session.scalar(select(func.count()).select_from(LoopWaitResponse).where(
                    LoopWaitResponse.request_id == wait.request_id)) == 0
                newer = await LoopWaitRequestService().open(session, loop,
                    LoopWaitRequestFactory.clarification("new round question"), created_by="test",
                    correlation_id=uuid.uuid4().hex, round_id=loop.current_round_id)
                assert newer.request_id != wait.request_id and newer.round_id == accepted["round_id"]
                await session.rollback()
        finally:
            await _stop(service, fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


def test_wait_supersession_rolls_back_with_message_admission(tmp_path, monkeypatch):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="wait-rollback", started_at=datetime.now(UTC))
        service = fixture["service"]
        try:
            wait = await _open_wait(sessions, fixture)
            original = LoopEventJournal.append

            async def fail(journal, session, loop_id, draft):
                if draft.kind == "loop.runtime.converged":
                    raise RuntimeError("rollback admission")
                return await original(journal, session, loop_id, draft)

            monkeypatch.setattr(LoopEventJournal, "append", fail)
            with pytest.raises(RuntimeError, match="rollback admission"):
                await service.user_message(fixture["context_id"], "not committed")
            async with sessions() as session:
                stored = await session.get(LoopWaitRequest, wait.request_id)
                loop = await session.get(AgentLoop, fixture["loop_id"])
                assert stored.status == "open" and stored.revision == 1 and stored.resolved_at is None
                assert loop.status == "waiting_user" and loop.current_round_id == fixture["round_id"]
                assert await session.scalar(select(func.count()).select_from(LoopUserIntent).where(
                    LoopUserIntent.loop_id == loop.loop_id)) == 0
                assert await session.scalar(select(func.count()).select_from(LoopJournalEvent).where(
                    LoopJournalEvent.entity_id == wait.request_id,
                    LoopJournalEvent.kind == "loop.wait.request_superseded")) == 0
            monkeypatch.setattr(LoopEventJournal, "append", original)
        finally:
            await _stop(service, fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


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


def test_message_and_old_stop_have_only_authorized_serial_outcomes(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="wait-race", started_at=datetime.now(UTC))
        service = fixture["service"]
        try:
            wait = await _open_wait(sessions, fixture)
            accepted, response = await asyncio.gather(
                service.user_message(fixture["context_id"], "concurrent new input"),
                service.resolve_wait_request(fixture["loop_id"], wait.request_id, _answer(wait, "stop"), actor_id="user"),
                return_exceptions=True)
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                stored = await session.get(LoopWaitRequest, wait.request_id)
                if isinstance(accepted, dict):
                    assert isinstance(response, HTTPException) and response.status_code == 409
                    assert loop.status == "running" and loop.current_round_id == accepted["round_id"]
                    assert stored.status == "superseded"
                else:
                    assert accepted is None or (isinstance(accepted, HTTPException) and accepted.status_code == 409)
                    assert isinstance(response, dict) and response["created"]
                    assert loop.status == "stopped" and stored.status == "resolved"
                    assert await session.scalar(select(func.count()).select_from(LoopUserIntent).where(
                        LoopUserIntent.loop_id == loop.loop_id)) == 0
        finally:
            await _stop(service, fixture["loop_id"])
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


def test_committed_budget_answer_replay_cannot_reapply_old_budget(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="budget-replay", started_at=datetime.now(UTC))
        service = fixture["service"]
        first = None
        try:
            for limit in (10000, 20000):
                async with sessions.begin() as session:
                    loop = await session.get(AgentLoop, fixture["loop_id"], with_for_update=True)
                    request = await LoopWaitRequestService().open(session, loop,
                        LoopWaitRequestFactory.budget_action("budget choice", {}), created_by="test",
                        correlation_id=uuid.uuid4().hex)
                body = LoopWaitResponseRequest(request_revision=1, idempotency_key=f"budget:{request.request_id}",
                    answer={"action": "revise_budget", "budgets": {"max_input_tokens": limit}})
                assert (await service.resolve_wait_request(fixture["loop_id"], request.request_id, body, actor_id="user"))["created"]
                if first is None:
                    first = (request.request_id, body)
            accepted = await service.user_message(fixture["context_id"], "keep current budget")
            replay = await service.resolve_wait_request(fixture["loop_id"], *first, actor_id="user")
            assert not replay["created"]
            async with sessions() as session:
                grant = await session.scalar(select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == fixture["loop_id"], LoopDelegationGrant.status == "active"))
                loop = await session.get(AgentLoop, fixture["loop_id"])
                assert grant.budgets["max_input_tokens"] == 20000
                assert loop.status == "running" and loop.current_round_id == accepted["round_id"]
        finally:
            await _stop(service, fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())
