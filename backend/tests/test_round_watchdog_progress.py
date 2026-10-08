"""本文件验证看门狗按同一 Round 的耐久进展计时。

输入为隔离 PostgreSQL 中的旧轮次、新结算模型和阶段/Worker 进展；输出为完整空闲窗口和并发重查断言。
具体工作流为经正式服务创建 Loop，保存进展，调用共享看门狗；固定时间验证 30 分钟边界。
初始观察事件、相同 phase 心跳、别轮进展、租约和未知预留不延长计时；用量锁串行化模型结算与最终判定。
示例：python -m pytest backend/tests/test_round_watchdog_progress.py -q。
"""

import asyncio
from datetime import UTC, datetime, timedelta
import os
import uuid

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.context_expansion.models import LoopIndexBudgetReservation
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.agent_loop.models import AgentLoop, LoopCoordinatorLease, LoopPatrolAttempt, LoopRound, LoopWorkerRequest
from backend.app.desktop.agent_loop.patrol_session_models import LoopPatrolPhaseTransition, LoopPatrolSession
from backend.app.desktop.agent_loop.round_events import RoundStateEventRecorder
from backend.app.desktop.agent_loop.rounds import RoundStallLimits, select_stalled_rounds, stall_reasons, terminate_stalled_rounds
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


async def _progress(session, fixture, source, at):
    loop_id, round_id = fixture["loop_id"], fixture["round_id"]
    if source == "round":
        row = await session.get(LoopRound, round_id)
        row.status = "curated"
        await RoundStateEventRecorder().record(session, row)
    elif source == "worker":
        session.add(LoopWorkerRequest(worker_request_id=uuid.uuid4().hex, loop_id=loop_id,
            round_id=round_id, kind="lane_curator", scope={}, status="success", completed_at=at))
    elif source == "patrol":
        session.add(LoopPatrolAttempt(patrol_attempt_id=uuid.uuid4().hex, loop_id=loop_id,
            round_id=round_id, attempt=1, execution_thread_id="patrol-test", checkpoint_ns="",
            observation_hash="h", status="success", completed_at=at))
    elif source == "model":
        session.add(LoopIndexBudgetReservation(reservation_id=uuid.uuid4().hex, loop_id=loop_id,
            grant_revision=1, model_calls=1, input_tokens=10, output_tokens=10,
            actual_usage={"owner_kind": "round", "owner_id": round_id, "round_id": round_id,
                          "model_calls": 1}, settled_at=at))
    elif source == "phase":
        session_id = uuid.uuid4().hex
        session.add(LoopPatrolSession(session_id=session_id, loop_id=loop_id, round_id=round_id,
            fencing_token=1, revision=2, current_phase="dispatching_curators"))
        await session.flush()
        session.add(LoopPatrolPhaseTransition(transition_id=uuid.uuid4().hex, session_id=session_id,
            loop_id=loop_id, round_id=round_id, revision=2, from_phase="freezing_observation",
            to_phase="dispatching_curators", safe_summary="进入索引准备", occurred_at=at))
    await session.flush()
    if source == "round":
        await session.execute(update(LoopJournalEvent).where(
            LoopJournalEvent.loop_id == loop_id, LoopJournalEvent.entity_id == round_id,
            LoopJournalEvent.kind == "loop.round.state_changed").values(occurred_at=at))


@pytest.mark.parametrize("source", ["round", "worker", "patrol", "model", "phase"])
def test_old_round_with_recent_progress_gets_a_full_idle_window(tmp_path, source):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(UTC)
        fixture = await _seed_loop(sessions, tmp_path, label=f"progress-{source}", started_at=now - timedelta(hours=1))
        try:
            async with sessions.begin() as session:
                await _progress(session, fixture, source, now)
            async with sessions.begin() as session:
                terminated = await terminate_stalled_rounds(session, RoundStallLimits(),
                    now + timedelta(seconds=1799), category="watchdog")
            assert fixture["round_id"] not in terminated, "最近模型结算或阶段/Worker 推进必须重置无进展计时"
            async with sessions() as session:
                assert (await session.get(AgentLoop, fixture["loop_id"])).status == "running"
            async with sessions.begin() as session:
                terminated = await terminate_stalled_rounds(session, RoundStallLimits(),
                    now + timedelta(seconds=1800), category="watchdog")
            assert fixture["round_id"] in terminated, "进展后持续空闲 30 分钟仍必须被收敛"
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


def test_unrelated_activity_and_unsettled_receipts_do_not_hide_a_stall(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(UTC)
        fixture = await _seed_loop(sessions, tmp_path, label="idle", started_at=now - timedelta(hours=1))
        try:
            async with sessions.begin() as session:
                other_round = uuid.uuid4().hex
                session.add(LoopRound(round_id=other_round, loop_id=fixture["loop_id"], number=2,
                    authority_revision=1, goal_revision=1, frontier_hash="f", workspace_revision=1, status="settled"))
                await session.flush()
                await _progress(session, {**fixture, "round_id": other_round}, "model", now)
                session.add(LoopIndexBudgetReservation(reservation_id=uuid.uuid4().hex, loop_id=fixture["loop_id"],
                    grant_revision=1, model_calls=1, input_tokens=10, output_tokens=10,
                    actual_usage={"round_id": fixture["round_id"], "receipt_state": "unknown"}))
                session.add(LoopCoordinatorLease(lease_id=uuid.uuid4().hex, round_id=fixture["round_id"],
                    owner_id="heartbeat", fencing_token="token", expires_at=now - timedelta(seconds=1)))
                session.add(LoopJournalEvent(event_id=uuid.uuid4().hex, loop_id=fixture["loop_id"],
                    sequence=1000, kind="loop.accounting.updated", entity_type="accounting",
                    entity_id=fixture["loop_id"], entity_revision=1000,
                    payload={"round_id": fixture["round_id"]}, idempotency_key=uuid.uuid4().hex))
                await _progress(session, fixture, "phase", now)
                await session.execute(update(LoopPatrolPhaseTransition).where(
                    LoopPatrolPhaseTransition.round_id == fixture["round_id"]).values(from_phase="dispatching_curators"))
            async with sessions.begin() as session:
                candidates = await select_stalled_rounds(session, RoundStallLimits(), now)
            assert fixture["round_id"] in {c.round_id for c in candidates}
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


def test_progress_does_not_reset_attempt_or_no_progress_limits():
    now = datetime.now(UTC)
    assert stall_reasons(status="observed", started_at=now - timedelta(hours=1), last_progress_at=now,
        attempts=12, no_progress_count=5, now=now, limits=RoundStallLimits()) == ("patrol_attempts", "no_progress")
    assert stall_reasons(status="curated", started_at=now.replace(tzinfo=None),
        last_progress_at=(now - timedelta(hours=2)).replace(tzinfo=None),
        attempts=0, no_progress_count=0, now=now, limits=RoundStallLimits()) == ()


def test_watchdog_rechecks_progress_after_candidate_scan(tmp_path, monkeypatch):
    import backend.app.desktop.agent_loop.rounds as rounds

    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(UTC)
        fixture = await _seed_loop(sessions, tmp_path, label="race", started_at=now - timedelta(hours=1))
        original = rounds.select_stalled_rounds
        scanned = False

        async def scan(*args, **kwargs):
            nonlocal scanned
            candidates = await original(*args, **kwargs)
            if not scanned:
                scanned = True
                assert fixture["round_id"] in {c.round_id for c in candidates}
                async with sessions.begin() as session:
                    await _progress(session, fixture, "worker", now)
            return candidates

        monkeypatch.setattr(rounds, "select_stalled_rounds", scan)
        try:
            async with sessions.begin() as session:
                terminated = await rounds.terminate_stalled_rounds(session, RoundStallLimits(), now, category="watchdog")
            assert fixture["round_id"] not in terminated, "候选扫描后完成的 Worker 不能被陈旧判定误杀"
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())
