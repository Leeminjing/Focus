"""本文件验证成功决策登记、按 Loop 串行沉淀和汇合吸收晚到来源。

输入为独立 PostgreSQL 的真实观察、成功权威凭据、领域结果和受控模型；输出为 FIFO、不可变输入、
一次吸收及无虚构 Observation 的断言。工作流先冻结再授权，竞争领取并恢复，最后核对汇合发布。
示例：python -m pytest backend/tests/test_overlap_progress_queue.py -q；不会调用真实模型或修改桌面库。
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_agent_loop_round_liveness import _seed_loop, _stop
from test_round_task_progress import _Checkpointer
from test_task_progress_validation_recovery import _Model

from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopDecision, LoopObservation, LoopRound
from backend.app.desktop.patrol.agent_loop.observations.observation_capture import LoopObservationService
from backend.app.desktop.patrol.agent_loop.task_progress.models import LoopProgressReceipt, LoopProgressWork, LoopTaskProgress
from backend.app.desktop.patrol.agent_loop.task_progress.repository import TaskProgressRepository
from backend.app.desktop.patrol.agent_loop.task_progress.runtime import TaskProgressRuntime
from backend.app.desktop.patrol.agent_loop.task_progress.work_queue import TaskProgressQueue
from backend.app.desktop.runs.evidence.repository import DomainResultRepository


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


async def _authorize(session, fixture, observation):
    identity = uuid.uuid4().hex
    row = LoopDecision(decision_id=identity, loop_id=fixture["loop_id"], round_id=fixture["round_id"],
        observation_id=observation.decision_inputs_ref, authority_committed_at=datetime.now(UTC),
        holder_id="test-holder", intent={}, rationale="test authority", evidence=[],
        status="committed", idempotency_key=identity)
    session.add(row)
    await session.flush()
    round_row = await session.get(LoopRound, fixture["round_id"])
    round_row.decision_id = identity
    await session.flush()
    return await TaskProgressQueue().enqueue_observation(session, fixture["loop_id"], observation.decision_inputs_ref)


def test_commit_only_work_then_join_absorbs_late_result_without_rewriting_observation(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="overlap-memory", started_at=datetime.now(UTC))
        try:
            envelope = await LoopObservationService(sessions, _Checkpointer()).capture(fixture["loop_id"], fixture["round_id"])
            repo, queue = TaskProgressRepository(), TaskProgressQueue()
            async with sessions.begin() as session:
                assert await session.scalar(select(func.count()).select_from(LoopProgressWork)
                    .where(LoopProgressWork.loop_id == fixture["loop_id"])) == 0
                frozen = await repo.inputs(session, envelope.decision_inputs_ref)
                work = await _authorize(session, fixture, envelope)
                work_id = work.work_id
                assert (await queue.enqueue_observation(session, fixture["loop_id"], envelope.decision_inputs_ref)).work_id == work_id
            async with sessions.begin() as session:
                late = await DomainResultRepository().record(session, kind="test", source_id=uuid.uuid4().hex,
                    loop_id=fixture["loop_id"], context_id=fixture["context_id"], payload={"status": "verified"})
                loop = await session.get(AgentLoop, fixture["loop_id"])
                assert not await queue.ensure_join_ready(session, loop, (fixture["round_id"],))
            model = _Model()
            runtime = TaskProgressRuntime(sessions, None, model_factory=lambda _: model)
            assert await runtime.drain(loop_id=fixture["loop_id"]) == 1
            async with sessions.begin() as session:
                assert (await session.get(LoopProgressWork, work_id)).state == "published"
                assert await session.get(LoopProgressReceipt, (fixture["loop_id"], late)) is None
                loop = await session.get(AgentLoop, fixture["loop_id"])
                assert not await queue.ensure_join_ready(session, loop, (fixture["round_id"],))
                join = await session.scalar(select(LoopProgressWork).where(LoopProgressWork.loop_id == loop.loop_id,
                    LoopProgressWork.observation_id.is_(None)))
                assert join is not None
                join_id = join.work_id
            assert await runtime.drain(loop_id=fixture["loop_id"]) == 1
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                assert await queue.ensure_join_ready(session, loop, (fixture["round_id"],))
                assert await repo.inputs(session, envelope.decision_inputs_ref) == frozen
                head = await repo.current(session, loop.loop_id)
                assert head.generation == 2 and head.observation_id is None and head.work_id == join_id
                assert head.contribution["round_id"] is None
                assert head.contribution["round_ids"] == [fixture["round_id"]]
                assert await session.get(LoopProgressReceipt, (loop.loop_id, late)) is not None
                assert await session.scalar(select(func.count()).select_from(LoopObservation)
                    .where(LoopObservation.loop_id == loop.loop_id)) == 1
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


def test_superseded_legacy_work_converges_unknown_then_accepts_actual_usage_once(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="lost-memory", started_at=datetime.now(UTC))
        try:
            envelope = await LoopObservationService(sessions, _Checkpointer()).capture(fixture["loop_id"], fixture["round_id"])
            async with sessions.begin() as session:
                work = await _authorize(session, fixture, envelope)
                work_id = work.work_id
                usage = await session.get(LoopBudgetUsage, fixture["loop_id"])
                before = (usage.model_calls, usage.input_tokens, usage.output_tokens)
            runtime = TaskProgressRuntime(sessions, None)
            claimed = await runtime._claim(loop_id=fixture["loop_id"])
            await runtime._reserve(*claimed, envelope.model_dump(mode="json"), 100, 100)
            async with sessions.begin() as session:
                await TaskProgressQueue().supersede_observation(session, fixture["loop_id"], envelope.decision_inputs_ref)
                work = await session.get(LoopProgressWork, work_id)
                assert work.state == "superseding" and work.fence > claimed[1]
                work.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            assert await runtime._claim(loop_id=fixture["loop_id"]) is None
            async with sessions() as session:
                work = await session.get(LoopProgressWork, work_id)
                assert work.state == "superseded" and work.usage[0]["settlement"] == "unknown"
                assert await session.scalar(select(func.count()).select_from(LoopProgressReceipt)
                    .where(LoopProgressReceipt.loop_id == fixture["loop_id"])) == 0
            await runtime._record_usage(*claimed, _Model())
            await runtime._record_usage(*claimed, _Model())
            async with sessions() as session:
                usage = await session.get(LoopBudgetUsage, fixture["loop_id"])
                assert (usage.model_calls - before[0], usage.input_tokens - before[1], usage.output_tokens - before[2]) == (1, 17, 9)
                work = await session.get(LoopProgressWork, work_id)
                assert work.state == "superseded" and work.usage[0]["settlement"] == "actual"
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_queue_cannot_skip_claimed_or_blocked_predecessor_and_retry_preserves_binding(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="fifo-memory", started_at=datetime.now(UTC))
        try:
            envelope = await LoopObservationService(sessions, _Checkpointer()).capture(fixture["loop_id"], fixture["round_id"])
            async with sessions.begin() as session:
                first = await _authorize(session, fixture, envelope)
                first_id = first.work_id
                session.add(LoopProgressWork(work_id=uuid.uuid4().hex, loop_id=fixture["loop_id"],
                    boundary_key=uuid.uuid4().hex, round_ids=[fixture["round_id"]],
                    source_manifest=first.source_manifest, created_at=datetime.now(UTC) + timedelta(seconds=1)))
            runtime = TaskProgressRuntime(sessions, None, model_factory=lambda _: _Model())
            claims = await asyncio.gather(runtime._claim(loop_id=fixture["loop_id"]), runtime._claim(loop_id=fixture["loop_id"]))
            assert sum(claim is not None for claim in claims) == 1
            async with sessions.begin() as session:
                first = await session.get(LoopProgressWork, first_id)
                binding = dict(first.consolidation_payload)
                original_fence = first.fence
                first.state = "blocked"
                first.lease_expires_at = None
            assert await runtime._claim(loop_id=fixture["loop_id"]) is None
            await runtime.retry(first_id, loop_id=fixture["loop_id"])
            claimed = await runtime._claim(loop_id=fixture["loop_id"])
            assert claimed == (first_id, original_fence + 1)
            async with sessions() as session:
                assert (await session.get(LoopProgressWork, first_id)).consolidation_payload == binding
            await runtime._consolidate(*claimed)
            assert await runtime.drain(loop_id=fixture["loop_id"]) == 1
            async with sessions() as session:
                published = tuple((await session.scalars(select(LoopTaskProgress).where(
                    LoopTaskProgress.loop_id == fixture["loop_id"]).order_by(LoopTaskProgress.generation))).all())
                assert len(published) == 3
                assert published[2].previous_progress_id == published[1].progress_id
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())
