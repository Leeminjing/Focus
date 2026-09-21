r"""本文件验证 Agent Loop round 的活性契约：领取公平性、决策终局收敛、终局短路、看门狗与存量恢复。

输入为真实 PostgreSQL 中的同工作区多 Loop、可由他人占用的与已过期的 coordinator 租约、停滞 round 的
attempt 计数、legacy 落定 decision 与 Kernel/Coordinator/Recovery 真实调用；输出为"队头被占用仍顺延领取"
"过期租约即时清除""拒绝即终结 round 并交回用户""已有落定 decision 的 round 零认知调用收敛""看门狗收敛
停滞队头而不误伤健康 round""在飞决策的 round 不被看门狗收敛""恢复收敛存量僵尸且不改写已提交事实""终结事件只追加一次且控制台可读原因"断言。
具体工作流为播种最小 Context revision 与已 settle 初始 Run、经 AgentLoopService.start 建 Loop，再直接驱动
coordinator/orchestrator/kernel/recovery 并读取只读控制台投影。
示例：`python -m pytest backend/tests/test_agent_loop_round_liveness.py -q`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop.agent_loop import (
    AgentLoopRecovery,
    AgentLoopService,
    LoopCoordinator,
    LoopCreateRequest,
    LoopKernel,
    PatrolDecisionIntent,
)
from backend.app.desktop.agent_loop.console_query import LoopConsoleQueryService
from backend.app.desktop.agent_loop.coordinator import CoordinatorClaim
from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopCoordinatorLease,
    LoopDecision,
    LoopEventOutbox,
    LoopObservation,
    LoopPatrolAttempt,
    LoopRound,
)
from backend.app.desktop.agent_loop.round_orchestration import LoopRoundOrchestrator
from backend.app.desktop.agent_loop.rounds import (
    TERMINATION_EVENT,
    UNDECIDED_ROUND_STATUSES,
    RoundStallLimits,
    stall_reasons,
    terminate_round,
)
from backend.app.desktop.context_evolution import (
    ContextRevisionContract,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionRef,
    ContextRevisionRepository,
)
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.run_orchestration import RunOutboxConsumer

from config_helpers import app_config_for


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


async def _seed_loop(sessions, tmp_path: Path, *, label: str, started_at: datetime) -> dict:
    """播种一个 workspace/context/settled 初始 Run 并启动 Loop，返回其观察轮与 intent 基线。"""
    workspace_id = f"ws-{label}-{uuid.uuid4().hex[:8]}"
    context_id = f"context-{label}-{uuid.uuid4().hex[:8]}"
    revision_id = uuid.uuid4().hex
    loop_id = uuid.uuid4().hex
    initial_run_id = f"initial-{uuid.uuid4().hex[:8]}"
    workspace_path = tmp_path / workspace_id
    workspace_path.mkdir()
    async with sessions.begin() as session:
        session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(workspace_path), display_name=label))
        await session.flush()
        session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{context_id}", title=label))
        await session.flush()
        ref = ContextRevisionRef(
            context_id=context_id,
            revision_id=revision_id,
            generation=1,
            execution_thread_id=f"thread-{context_id}",
            checkpoint_ns="",
            checkpoint_id=f"checkpoint-{context_id}",
            payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
        )
        await ContextRevisionRepository().insert(
            session,
            ContextRevisionContract(
                ref=ref,
                content_hash="a" * 64,
                projection_status=ContextRevisionProjectionStatus.VALID,
                origin_kind=ContextRevisionOriginKind.ROOT,
                created_at=datetime.now(UTC),
            ),
        )
        await ContextRevisionRepository().switch_current(session, ref, None)
        session.add(
            DesktopRun(
                run_id=initial_run_id,
                task_id=context_id,
                agent_id=f"main:{context_id}",
                kind="main",
                status="success",
                origin="direct_user",
                execution_thread_id=f"thread-{context_id}",
                context_revision_id=revision_id,
                settled_at=datetime.now(UTC),
            )
        )
    service = AgentLoopService(sessions)
    snapshot = await service.start(
        LoopCreateRequest(
            loop_id=loop_id,
            workspace_id=workspace_id,
            initial_context_id=context_id,
            initial_run_id=initial_run_id,
            holder_id=f"patrol-{label}",
            goal=f"Deliver the {label} change",
            task_contract="Stay in scope and pass the focused tests",
            acceptance_criteria=({"criterion_id": "tests", "text": "focused tests pass"},),
            capabilities=("continue_context", "request_completion"),
            context_scope=(context_id,),
            permission_scope=("read", "write"),
        )
    )
    round_id = snapshot["current_round_id"]
    async with sessions.begin() as session:
        await session.execute(update(LoopRound).where(LoopRound.round_id == round_id).values(started_at=started_at))
    return {
        "service": service,
        "loop_id": loop_id,
        "context_id": context_id,
        "revision_id": revision_id,
        "round_id": round_id,
        "snapshot": snapshot,
    }


async def _stop(service: AgentLoopService, loop_id: str) -> None:
    async with service._sessions() as session:
        loop = await session.get(AgentLoop, loop_id)
    if loop is not None and loop.status in {"running", "paused", "waiting_user"}:
        await service.control(loop_id, "stop")


def test_claim_skips_leased_head_and_claims_next_candidate(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        older = newer = None
        try:
            older = await _seed_loop(sessions, tmp_path, label="older", started_at=datetime.now(UTC) - timedelta(hours=2))
            newer = await _seed_loop(sessions, tmp_path, label="newer", started_at=datetime.now(UTC) - timedelta(hours=1))
            async with sessions.begin() as session:
                session.add(
                    LoopCoordinatorLease(
                        lease_id=uuid.uuid4().hex,
                        round_id=older["round_id"],
                        owner_id="other-coordinator",
                        fencing_token=uuid.uuid4().hex,
                        expires_at=datetime.now(UTC) + timedelta(seconds=60),
                    )
                )

            claim = await LoopCoordinator(sessions).claim("agent-loop-coordinator")

            assert claim is not None, "队头 round 被他人持有有效租约时，本协调者仍应顺延领取后续候选"
            assert claim.round_id != older["round_id"], "不得领取他人仍持有的 round"
            assert claim.round_id == newer["round_id"]
            async with sessions() as session:
                blocked = await session.get(AgentLoop, older["loop_id"])
                claimed = await session.get(AgentLoop, newer["loop_id"])
                assert blocked.health == "observing", "被阻塞的 Loop 不应被本协调者推进"
                assert claimed.health == "deciding", "被领取的 Loop 必须离开仅观察状态"
        finally:
            for fixture in (older, newer):
                if fixture is not None:
                    await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_expired_lease_is_purged_and_round_becomes_claimable(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="expired", started_at=datetime.now(UTC) - timedelta(hours=3))
            dead_lease_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(
                    LoopCoordinatorLease(
                        lease_id=dead_lease_id,
                        round_id=fixture["round_id"],
                        owner_id="dead-coordinator",
                        fencing_token=uuid.uuid4().hex,
                        expires_at=datetime.now(UTC) - timedelta(seconds=5),
                    )
                )

            claim = await LoopCoordinator(sessions).claim("agent-loop-coordinator")

            assert claim is not None and claim.round_id == fixture["round_id"]
            async with sessions() as session:
                assert await session.get(LoopCoordinatorLease, dead_lease_id) is None, "过期租约必须被清除而不是留在表中"
                live = await session.scalar(select(LoopCoordinatorLease).where(LoopCoordinatorLease.round_id == fixture["round_id"]))
                assert live is not None and live.owner_id == "agent-loop-coordinator"
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_rejected_decision_terminates_round_and_hands_back_loop(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="reject", started_at=datetime.now(UTC) - timedelta(hours=4))
            async with sessions.begin() as session:
                round_row = await session.get(LoopRound, fixture["round_id"])
                session.add(
                    DesktopRun(
                        run_id=f"active-{uuid.uuid4().hex[:8]}",
                        task_id=fixture["context_id"],
                        agent_id=f"main:{fixture['context_id']}",
                        kind="main",
                        status="running",
                        origin="delegated_patrol",
                        execution_thread_id=f"thread-{fixture['context_id']}",
                        context_revision_id=fixture["revision_id"],
                        loop_id=fixture["loop_id"],
                        round_id=fixture["round_id"],
                    )
                )
                observed_frontier_hash = round_row.frontier_hash
                observed_workspace_revision = round_row.workspace_revision
            intent = PatrolDecisionIntent(
                decision_id=uuid.uuid4().hex,
                idempotency_key=f"reject-{fixture['loop_id']}",
                loop_id=fixture["loop_id"],
                loop_revision=fixture["snapshot"]["revision"],
                round_id=fixture["round_id"],
                holder_id=fixture["snapshot"]["holder_id"],
                grant_id=fixture["snapshot"]["grant"]["grant_id"],
                grant_revision=1,
                goal_revision=1,
                observed_frontier_hash=observed_frontier_hash,
                observed_workspace_revision=observed_workspace_revision,
                rationale="Continue in the current Context.",
                actions=(
                    {
                        "action": "continue_context",
                        "context_id": fixture["context_id"],
                        "context_revision_id": fixture["revision_id"],
                        "message": "Run the focused checks.",
                    },
                ),
            )

            result = await LoopKernel(sessions).commit(intent)

            assert result.status == "rejected"
            assert "已有活动 Run" in (result.reason or "")
            async with sessions() as session:
                round_row = await session.get(LoopRound, fixture["round_id"])
                loop = await session.get(AgentLoop, fixture["loop_id"])
                events = list(
                    (
                        await session.scalars(
                            select(LoopEventOutbox).where(
                                LoopEventOutbox.loop_id == fixture["loop_id"],
                                LoopEventOutbox.event_type == TERMINATION_EVENT,
                            )
                        )
                    ).all()
                )
            assert round_row.status == "error", "被拒绝的 round 必须收敛为终态，不得停留在可领取状态"
            assert round_row.settled_at is not None
            assert round_row.decision_id == result.decision_id
            assert loop.status == "waiting_user"
            assert loop.health == "degraded"
            assert "已有活动 Run" in (loop.waiting_reason or "")
            assert len(events) == 1, "拒绝终结必须追加一条可观测事件"
            assert events[0].payload["category"] == "rejected"
            assert events[0].payload["reason"] == "Loop 已有活动 Run"
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_legacy_decided_round_is_converged_without_cognition(tmp_path: Path, monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="legacy", started_at=datetime.now(UTC) - timedelta(hours=5))
            decision_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(
                    LoopDecision(
                        decision_id=decision_id,
                        loop_id=fixture["loop_id"],
                        round_id=fixture["round_id"],
                        holder_id=fixture["snapshot"]["holder_id"],
                        intent={},
                        rationale="legacy rejected decision",
                        status="rejected",
                        idempotency_key=f"patrol:{fixture['round_id']}:legacy",
                        rejection={"reason": "Loop 已有活动 Run"},
                    )
                )

            orchestrator = LoopRoundOrchestrator(
                sessions,
                app_config_for("patrol-test", None),
                LoopKernel(sessions),
                None,
            )
            claim = CoordinatorClaim(
                lease_id=uuid.uuid4().hex,
                loop_id=fixture["loop_id"],
                round_id=fixture["round_id"],
                fencing_token=uuid.uuid4().hex,
            )

            result = await orchestrator.process(claim)
            repeated = await orchestrator.process(claim)

            assert result is None and repeated is None
            async with sessions() as session:
                observations = await session.scalar(
                    select(func.count()).select_from(LoopObservation).where(LoopObservation.round_id == fixture["round_id"])
                )
                attempts = await session.scalar(
                    select(func.count()).select_from(LoopPatrolAttempt).where(LoopPatrolAttempt.round_id == fixture["round_id"])
                )
                decisions = await session.scalar(
                    select(func.count()).select_from(LoopDecision).where(LoopDecision.round_id == fixture["round_id"])
                )
                round_row = await session.get(LoopRound, fixture["round_id"])
                loop = await session.get(AgentLoop, fixture["loop_id"])
            assert observations == 0, "已有落定决策的 round 不得再产生观察"
            assert attempts == 0, "已有落定决策的 round 不得再产生认知调用"
            assert decisions == 1, "重复领取必须幂等：同一 round 至多一条决策"
            assert round_row.status == "error", "存量僵死 round 必须被收敛为终态"
            assert loop.status == "waiting_user"
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("终局 round 不得再触发认知模型构造")

    monkeypatch.setattr(LoopRoundOrchestrator, "_decision_model", _forbidden)
    asyncio.run(run())


def test_termination_refuses_rounds_that_already_carry_committed_authority(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="authority", started_at=datetime.now(UTC) - timedelta(hours=8))
            async with sessions.begin() as session:
                round_row = await session.get(LoopRound, fixture["round_id"], with_for_update=True)
                round_row.status = "ready"
                round_row.decision_id = uuid.uuid4().hex

            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"], with_for_update=True)
                round_row = await session.get(LoopRound, fixture["round_id"], with_for_update=True)
                refused = await terminate_round(
                    session,
                    loop,
                    round_row,
                    category="patrol_failed",
                    reason="陈旧的 Patrol 失败路径",
                    allowed_statuses=UNDECIDED_ROUND_STATUSES,
                )
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"], with_for_update=True)
                round_row = await session.get(LoopRound, fixture["round_id"], with_for_update=True)
                accepted = await terminate_round(
                    session,
                    loop,
                    round_row,
                    category="budget",
                    reason="hard budget 已耗尽，未启动新的 Run",
                    allowed_statuses=("ready",),
                )

            assert refused is False, "已提交权威事实（ready、决策已落定）的 round 不得被失败路径收敛"
            assert accepted is True, "声明了 ready 前置状态的调度路径仍应能收敛"
            async with sessions() as session:
                round_row = await session.get(LoopRound, fixture["round_id"])
                loop = await session.get(AgentLoop, fixture["loop_id"])
                events = list(
                    (
                        await session.scalars(
                            select(LoopEventOutbox).where(
                                LoopEventOutbox.loop_id == fixture["loop_id"],
                                LoopEventOutbox.event_type == TERMINATION_EVENT,
                            )
                        )
                    ).all()
                )
            assert round_row.status == "error"
            assert loop.status == "waiting_user"
            assert len(events) == 1, "被拒绝的收敛不得留下终结事件"
            assert events[0].payload["category"] == "budget"
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_superseded_decision_converges_round_to_terminal(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="supersede", started_at=datetime.now(UTC) - timedelta(hours=9))
            overridden = await fixture["service"].override(
                fixture["loop_id"],
                "Prioritize correctness",
                "Keep the implementation lane authoritative",
                [{"criterion_id": "tests", "text": "focused tests pass"}],
            )
            stale_round_id = overridden["current_round_id"]
            async with sessions() as session:
                round_row = await session.get(LoopRound, stale_round_id)
                observed_frontier_hash = round_row.frontier_hash
                observed_workspace_revision = round_row.workspace_revision
            intent = PatrolDecisionIntent(
                decision_id=uuid.uuid4().hex,
                idempotency_key=f"stale-{fixture['loop_id']}",
                loop_id=fixture["loop_id"],
                loop_revision=fixture["snapshot"]["revision"],
                round_id=stale_round_id,
                holder_id=fixture["snapshot"]["holder_id"],
                grant_id=fixture["snapshot"]["grant"]["grant_id"],
                grant_revision=1,
                goal_revision=1,
                observed_frontier_hash=observed_frontier_hash,
                observed_workspace_revision=observed_workspace_revision,
                rationale="Continue in the current Context.",
                actions=(
                    {
                        "action": "continue_context",
                        "context_id": fixture["context_id"],
                        "context_revision_id": fixture["revision_id"],
                        "message": "Run the focused checks.",
                    },
                ),
            )

            result = await LoopKernel(sessions).commit(intent)

            assert result.status == "superseded"
            async with sessions() as session:
                round_row = await session.get(LoopRound, stale_round_id)
                events = list(
                    (
                        await session.scalars(
                            select(LoopEventOutbox).where(
                                LoopEventOutbox.loop_id == fixture["loop_id"],
                                LoopEventOutbox.event_type == TERMINATION_EVENT,
                            )
                        )
                    ).all()
                )
            assert round_row.status == "error", "被取代的决策同样必须终结该 round"
            assert round_row.settled_at is not None
            assert round_row.decision_id == result.decision_id
            assert len(events) == 1 and events[0].payload["category"] == "superseded"
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_candidate_statement_locks_only_the_round_table() -> None:
    statement = LoopCoordinator._candidate_statement(datetime.now(UTC))

    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "FOR UPDATE OF loop_rounds SKIP LOCKED" in sql
    assert "LEFT OUTER JOIN loop_coordinator_leases" in sql


def test_stall_limits_defaults_bound_normal_cognition_calls() -> None:
    limits = RoundStallLimits()
    now = datetime.now(UTC)

    assert (limits.max_round_seconds, limits.max_patrol_attempts, limits.max_no_progress) == (1800, 12, 5)
    assert stall_reasons(status="observed", started_at=now - timedelta(seconds=16), attempts=1, no_progress_count=0, now=now, limits=limits) == ()
    assert stall_reasons(status="observed", started_at=now, attempts=11, no_progress_count=0, now=now, limits=limits) == ()
    assert stall_reasons(status="observed", started_at=now, attempts=12, no_progress_count=0, now=now, limits=limits) == ("patrol_attempts",)
    assert stall_reasons(status="observed", started_at=now - timedelta(seconds=1800), attempts=0, no_progress_count=0, now=now, limits=limits) == ("round_seconds",)
    assert "no_progress" not in stall_reasons(status="observed", started_at=now, attempts=0, no_progress_count=9, now=now, limits=limits)
    assert "no_progress" in stall_reasons(status="observed", started_at=now, attempts=1, no_progress_count=5, now=now, limits=limits)


def test_concurrent_claims_lease_distinct_rounds_and_fence_stale_owners(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        first = second = None
        try:
            first = await _seed_loop(sessions, tmp_path, label="concurrent-a", started_at=datetime.now(UTC) - timedelta(hours=3))
            second = await _seed_loop(sessions, tmp_path, label="concurrent-b", started_at=datetime.now(UTC) - timedelta(hours=2))
            expected = {first["round_id"], second["round_id"]}

            claims = await asyncio.gather(
                LoopCoordinator(sessions).claim("coordinator-a"),
                LoopCoordinator(sessions).claim("coordinator-b"),
            )

            assert all(claim is not None for claim in claims)
            claimed = [claim for claim in claims if claim is not None]
            assert {claim.round_id for claim in claimed} == expected
            assert len({claim.lease_id for claim in claimed}) == 2
            async with sessions() as session:
                leases = list(
                    (
                        await session.scalars(
                            select(LoopCoordinatorLease).where(LoopCoordinatorLease.round_id.in_(sorted(expected)))
                        )
                    ).all()
                )
            assert len(leases) == 2, "每个 round 至多一个租约"
            stale = CoordinatorClaim(
                lease_id=claimed[0].lease_id,
                loop_id=claimed[0].loop_id,
                round_id=claimed[0].round_id,
                fencing_token="stale-fencing-token",
            )
            assert await LoopCoordinator(sessions).release(stale) is False, "未持有租约者不得释放他人租约"
        finally:
            for fixture in (first, second):
                if fixture is not None:
                    await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_watchdog_converges_stalled_head_without_touching_healthy_round(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        stalled = healthy = None
        try:
            stalled = await _seed_loop(sessions, tmp_path, label="stalled", started_at=datetime.now(UTC) - timedelta(minutes=45))
            healthy = await _seed_loop(sessions, tmp_path, label="healthy", started_at=datetime.now(UTC) - timedelta(minutes=5))
            expired_lease_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(
                    LoopCoordinatorLease(
                        lease_id=expired_lease_id,
                        round_id=stalled["round_id"],
                        owner_id="dead-coordinator",
                        fencing_token=uuid.uuid4().hex,
                        expires_at=datetime.now(UTC) - timedelta(seconds=30),
                    )
                )
                for index in range(3):
                    session.add(
                        LoopPatrolAttempt(
                            patrol_attempt_id=uuid.uuid4().hex,
                            loop_id=stalled["loop_id"],
                            round_id=stalled["round_id"],
                            attempt=index + 1,
                            execution_thread_id=f"thread-{stalled['context_id']}",
                            checkpoint_ns="",
                            observation_hash="b" * 64,
                            status="success",
                        )
                    )
            coordinator = LoopCoordinator(
                sessions,
                stall_limits=RoundStallLimits(max_round_seconds=1800, max_patrol_attempts=3, max_no_progress=5),
            )

            terminated = await coordinator.maintain_rounds()

            assert stalled["round_id"] in terminated
            async with sessions() as session:
                stalled_round = await session.get(LoopRound, stalled["round_id"])
                stalled_loop = await session.get(AgentLoop, stalled["loop_id"])
                healthy_round = await session.get(LoopRound, healthy["round_id"])
                healthy_loop = await session.get(AgentLoop, healthy["loop_id"])
            assert stalled_round.status == "error" and stalled_round.settled_at is not None
            assert stalled_loop.status == "waiting_user" and stalled_loop.health == "degraded"
            assert "无进展" in (stalled_loop.waiting_reason or "")
            assert "patrol_attempts" in (stalled_loop.waiting_reason or ""), "原因必须包含越界维度"
            async with sessions() as session:
                attempts = await session.scalar(
                    select(func.count()).select_from(LoopPatrolAttempt).where(LoopPatrolAttempt.round_id == stalled["round_id"])
                )
                leftover_lease = await session.get(LoopCoordinatorLease, expired_lease_id)
            assert attempts == 3, "看门狗收敛不得产生任何新的认知调用"
            assert leftover_lease is None, "收敛必须释放该 round 的领取名额（清除其租约）"
            assert healthy_round.status == "observed", "未越界的健康 round 不得被误收敛"
            assert healthy_loop.status == "running"

            claim = await LoopCoordinator(sessions).claim("agent-loop-coordinator")

            assert claim is not None, "队头被收敛后必须能领取后续候选"
            assert claim.round_id != stalled["round_id"]
        finally:
            for fixture in (stalled, healthy):
                if fixture is not None:
                    await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_watchdog_converges_stalled_publishing_round(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="publishing", started_at=datetime.now(UTC) - timedelta(hours=4))
            async with sessions.begin() as session:
                round_row = await session.get(LoopRound, fixture["round_id"], with_for_update=True)
                round_row.status = "publishing"
                round_row.decision_id = uuid.uuid4().hex
            coordinator = LoopCoordinator(sessions, stall_limits=RoundStallLimits(max_round_seconds=1800, max_patrol_attempts=12, max_no_progress=5))

            terminated = await coordinator.maintain_rounds()

            assert fixture["round_id"] in terminated, "发布/采用阶段停滞的 round 同样必须有界收敛"
            async with sessions() as session:
                round_row = await session.get(LoopRound, fixture["round_id"])
                loop = await session.get(AgentLoop, fixture["loop_id"])
            assert round_row.status == "error"
            assert "round_seconds" in (loop.waiting_reason or "")
            assert loop.status == "waiting_user"
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_watchdog_keeps_round_whose_decision_is_still_in_flight(tmp_path: Path) -> None:
    """在飞决策不算落定：等待发布组件的 round 留给其所有者；已终止决策的僵尸 round 仍必须收敛。"""
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        in_flight = zombie = None
        try:
            in_flight = await _seed_loop(sessions, tmp_path, label="in-flight", started_at=datetime.now(UTC))
            zombie = await _seed_loop(sessions, tmp_path, label="settled-zombie", started_at=datetime.now(UTC))
            async with sessions.begin() as session:
                for fixture, decision_status, round_status in (
                    (in_flight, "publishing", "publishing"),
                    (zombie, "committed", "observed"),
                ):
                    session.add(
                        LoopDecision(
                            decision_id=uuid.uuid4().hex,
                            loop_id=fixture["loop_id"],
                            round_id=fixture["round_id"],
                            holder_id=fixture["snapshot"]["holder_id"],
                            intent={},
                            rationale=f"{decision_status} decision",
                            status=decision_status,
                            idempotency_key=f"patrol:{fixture['round_id']}:{decision_status}",
                        )
                    )
                    round_row = await session.get(LoopRound, fixture["round_id"], with_for_update=True)
                    round_row.status = round_status
            coordinator = LoopCoordinator(
                sessions,
                stall_limits=RoundStallLimits(max_round_seconds=1800, max_patrol_attempts=12, max_no_progress=5),
            )

            terminated = await coordinator.maintain_rounds()

            assert in_flight["round_id"] not in terminated, "决策仍在 publishing 不算落定，看门狗不得收敛该 round"
            async with sessions() as session:
                in_flight_round = await session.get(LoopRound, in_flight["round_id"])
                in_flight_loop = await session.get(AgentLoop, in_flight["loop_id"])
            assert in_flight_round.status == "publishing"
            assert in_flight_loop.status == "running" and in_flight_loop.waiting_reason is None
            assert zombie["round_id"] in terminated, "已终止决策却仍停留在可领取状态的 round 仍必须收敛"
            async with sessions() as session:
                zombie_loop = await session.get(AgentLoop, zombie["loop_id"])
            assert "已有落定决策" in (zombie_loop.waiting_reason or "")
        finally:
            for fixture in (in_flight, zombie):
                if fixture is not None:
                    await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_termination_is_idempotent_and_emits_one_event(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="idempotent", started_at=datetime.now(UTC) - timedelta(hours=6))
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                round_row = await session.get(LoopRound, fixture["round_id"], with_for_update=True)
                first = await terminate_round(session, loop, round_row, category="watchdog", reason="Round 无进展（patrol_attempts），已收敛为终态", allowed_statuses=UNDECIDED_ROUND_STATUSES)
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                round_row = await session.get(LoopRound, fixture["round_id"], with_for_update=True)
                second = await terminate_round(session, loop, round_row, category="watchdog", reason="Round 无进展（patrol_attempts），已收敛为终态", allowed_statuses=UNDECIDED_ROUND_STATUSES)

            assert first is True and second is False
            async with sessions() as session:
                events = list(
                    (
                        await session.scalars(
                            select(LoopEventOutbox).where(
                                LoopEventOutbox.loop_id == fixture["loop_id"],
                                LoopEventOutbox.event_type == TERMINATION_EVENT,
                            )
                        )
                    ).all()
                )
            assert len(events) == 1, "同一 round 的终结事件只追加一次"
            assert events[0].idempotency_key == f"round-terminated:{fixture['round_id']}"
            assert events[0].payload["category"] == "watchdog"
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_recovery_converges_legacy_zombie_and_preserves_committed_facts(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        zombie = healthy = None
        try:
            zombie = await _seed_loop(sessions, tmp_path, label="zombie", started_at=datetime.now(UTC) - timedelta(minutes=20))
            healthy = await _seed_loop(sessions, tmp_path, label="survivor", started_at=datetime.now(UTC) - timedelta(minutes=10))
            decision_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(
                    LoopDecision(
                        decision_id=decision_id,
                        loop_id=zombie["loop_id"],
                        round_id=zombie["round_id"],
                        holder_id=zombie["snapshot"]["holder_id"],
                        intent={},
                        rationale="legacy rejected decision",
                        status="rejected",
                        idempotency_key=f"patrol:{zombie['round_id']}:legacy",
                        rejection={"reason": "Loop 已有活动 Run"},
                    )
                )

            report = await AgentLoopRecovery(sessions, LoopCoordinator(sessions), RunOutboxConsumer(sessions)).reconcile()

            assert report.stalled_rounds >= 1
            async with sessions() as session:
                zombie_round = await session.get(LoopRound, zombie["round_id"])
                zombie_loop = await session.get(AgentLoop, zombie["loop_id"])
                survivor_round = await session.get(LoopRound, healthy["round_id"])
                survivor_loop = await session.get(AgentLoop, healthy["loop_id"])
                preserved = await session.get(LoopDecision, decision_id)
                leftovers = list(
                    (
                        await session.scalars(
                            select(LoopCoordinatorLease).where(
                                LoopCoordinatorLease.round_id.in_([zombie["round_id"], healthy["round_id"]])
                            )
                        )
                    ).all()
                )
            assert zombie_round.status == "error" and zombie_round.decision_id == decision_id
            assert zombie_loop.status == "waiting_user"
            assert preserved.status == "rejected" and preserved.rejection == {"reason": "Loop 已有活动 Run"}
            assert survivor_round.status == "observed", "恢复不得触碰仍在 running 且健康的 round"
            assert survivor_loop.status == "running"
            assert leftovers == []

            claim = await LoopCoordinator(sessions).claim("agent-loop-coordinator")

            assert claim is not None
            assert claim.round_id != zombie["round_id"], "被收敛的 round 不得再成为候选"
        finally:
            for fixture in (zombie, healthy):
                if fixture is not None:
                    await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_console_projection_exposes_termination_reason(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="console", started_at=datetime.now(UTC) - timedelta(hours=7))
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                round_row = await session.get(LoopRound, fixture["round_id"], with_for_update=True)
                await terminate_round(session, loop, round_row, category="watchdog", reason="Round 无进展（patrol_attempts、round_seconds），已收敛为终态", allowed_statuses=UNDECIDED_ROUND_STATUSES)

            async with sessions() as session:
                console = await LoopConsoleQueryService().read(session, fixture["loop_id"])

            assert console["status"] == "waiting_user"
            assert console["health"] == "degraded"
            assert "无进展" in (console["waiting_reason"] or "")
            assert console["current_round"]["status"] == "error"
            assert console["current_round"]["settled_at"] is not None
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())
