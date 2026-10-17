"""Validate shared build ownership, cancellation and durable segment checkpoints."""

import asyncio
import logging

import pytest
from sqlalchemy import select

from backend.app.desktop.agent_loop.context_expansion.models import (
    LoopSegmentProjectionRecord,
    LoopSemanticIndexArtifact,
)
from backend.app.desktop.agent_loop.models import LoopBudgetUsage
from backend.tests.incremental_index_support import ModelProbe, messages, observation, revision
from backend.tests.test_incremental_revision_index import exercise


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


@pytest.mark.parametrize("cancel_waiter", [None, 0, 1])
def test_identical_authorized_builds_share_work_and_cancel_independently(tmp_path, cancel_waiter):
    async def run(sessions, seed):
        entered, release = asyncio.Event(), asyncio.Event()

        async def gate(role, payload):
            if role == "projector":
                entered.set()
                await release.wait()

        probe = ModelProbe(gate=gate)
        service = probe.service(sessions)
        target = await revision(sessions, seed["context_id"], messages(12))
        obs = observation(seed, target)
        tasks = [asyncio.create_task(service.build(obs)) for _ in range(2)]
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert len(service._inflight_builds) == 1
            assert next(iter(service._inflight_builds.values())).waiters == 2
            if cancel_waiter is not None:
                tasks[cancel_waiter].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await tasks[cancel_waiter]
                assert probe.active == 1
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            successful = [r for r in results if not isinstance(r, BaseException)]
            assert all(r.blocker_code is None for r in successful)
            if cancel_waiter is None:
                assert results[0] == results[1]
            assert len(probe.calls) == 3
            assert not service._inflight_builds and probe.active == 0
            async with sessions() as session:
                assert (await session.get(LoopBudgetUsage, seed["loop_id"])).model_calls == 3
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    exercise(tmp_path, run)


def test_last_cancelled_waiter_drains_shared_work_and_allows_retry(tmp_path):
    async def run(sessions, seed):
        entered = asyncio.Event()

        async def gate(role, payload):
            if role == "verifier":
                entered.set()
                await asyncio.Event().wait()

        probe = ModelProbe(gate=gate)
        service = probe.service(sessions)
        target = await revision(sessions, seed["context_id"], messages(12))
        obs = observation(seed, target)
        tasks = [asyncio.create_task(service.build(obs)) for _ in range(2)]
        try:
            await asyncio.wait_for(entered.wait(), 5)
            for task in tasks:
                task.cancel()
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
            assert not service._inflight_builds and probe.active == 0
            async with sessions() as session:
                assert (await session.get(LoopBudgetUsage, seed["loop_id"])).model_calls == 1
            probe.gate = None
            assert (await service.build(obs)).blocker_code is None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    exercise(tmp_path, run)


def test_shared_build_keeps_the_input_snapshot_from_admission(tmp_path):
    async def run(sessions, seed):
        probe = ModelProbe()
        service = probe.service(sessions)
        target = await revision(sessions, seed["context_id"], messages(12))
        obs = observation(seed, target)
        task = asyncio.create_task(service.build(obs))
        await asyncio.sleep(0)
        obs.grant["context_scope"] = ["foreign"]
        result = await task
        assert result.blocker_code is None, result.blocker_summary
        assert len(probe.calls) == 3

    exercise(tmp_path, run)


@pytest.mark.parametrize("difference", ["scope", "budget"])
def test_different_authorizations_never_join_running_build(tmp_path, difference):
    async def run(sessions, seed):
        entered, release = asyncio.Event(), asyncio.Event()

        async def gate(role, payload):
            if role == "projector":
                entered.set()
                await release.wait()

        probe = ModelProbe(gate=gate)
        service = probe.service(sessions)
        target = await revision(sessions, seed["context_id"], messages(12))
        task = asyncio.create_task(service.build(observation(seed, target)))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            other = observation(
                seed, target,
                scope=["foreign"] if difference == "scope" else None,
                budget={"max_model_calls": 0} if difference == "budget" else None,
            )
            result = await asyncio.wait_for(service.build(other), 5)
            assert result.blocker_code == (
                "portfolio_index_budget" if difference == "budget" else "portfolio_index_failed"
            )
            release.set()
            assert (await task).blocker_code is None
            assert len(probe.calls) == 3
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    exercise(tmp_path, run)


def test_failed_interpretation_keeps_segments_for_a_new_service(tmp_path, caplog):
    async def run(sessions, seed):
        async def gate(role, payload):
            if role == "interpretation":
                raise ValueError("stale interpretation attempt")

        probe = ModelProbe(gate=gate)
        target = await revision(sessions, seed["context_id"], messages(36))
        obs = observation(seed, target)
        result = await probe.service(sessions).build(obs)
        assert result.blocker_code == "portfolio_index_failed"
        assert len(probe.local_calls) == 6
        async with sessions() as session:
            records = (await session.scalars(select(LoopSegmentProjectionRecord).where(
                LoopSegmentProjectionRecord.context_id == target.ref.context_id
            ))).all()
            assert len(records) == 3
            assert not await session.scalar(select(LoopSemanticIndexArtifact.index_id).where(
                LoopSemanticIndexArtifact.revision_id == target.ref.revision_id
            ))
        probe.gate = None
        probe.calls.clear()
        result = await probe.service(sessions).build(obs)
        assert result.blocker_code is None, result.blocker_summary
        assert not probe.local_calls and len(probe.interpretation_calls) == 1
        assert set(result.indexes[0].inheritance.record_ids) == {r.record_id for r in records}
        metrics = [r for r in caplog.records if r.msg.startswith("semantic_index_revision")]
        assert metrics[-1].local_cache_hits == 3 and metrics[-1].projected_segments == 0

    with caplog.at_level(logging.INFO):
        exercise(tmp_path, run)


def test_segment_failure_preserves_successful_siblings_for_retry(tmp_path):
    async def run(sessions, seed):
        probe = ModelProbe()
        service = probe.service(sessions, concurrency=1, max_attempts=1)
        target = await revision(sessions, seed["context_id"], messages(24))
        project = service._project_segment
        committed = asyncio.Event()

        async def fail_second(frozen, segment):
            if segment.ordinal == 1:
                await committed.wait()
                raise ValueError("stale second segment")
            result = await project(frozen, segment)
            committed.set()
            return result

        service._project_segment = fail_second
        obs = observation(seed, target)
        failed = await service.build(obs)
        assert failed.blocker_code == "portfolio_index_failed" and len(probe.calls) == 2
        probe.calls.clear()
        rebuilt = await probe.service(sessions).build(obs)
        assert rebuilt.blocker_code is None, rebuilt.blocker_summary
        assert len(probe.local_calls) == 2 and len(probe.interpretation_calls) == 1
        assert probe.local_calls[0][1]["segments"][0]["messages"][0]["message_id"] == "m-12"

    exercise(tmp_path, run)


def test_runtime_duration_does_not_replay_immutable_stage_duration(tmp_path, caplog):
    async def run(sessions, seed):
        probe = ModelProbe()
        service = probe.service(sessions)
        target = await revision(sessions, seed["context_id"], messages(12))
        obs = observation(seed, target)
        first = await service.build(obs)
        replay = await service.build(obs)
        assert first.stage_record == replay.stage_record
        metrics = [r for r in caplog.records if r.msg.startswith("semantic_index_build")]
        assert len(metrics) == 2 and all(r.duration_ms >= 0 for r in metrics)
        assert all(not r.joined_build for r in metrics)
        invocations = [r for r in caplog.records if r.msg.startswith("semantic_index_model_invocation")]
        assert len(invocations) == 3
        assert all(r.execution_ms >= r.admission_ms >= 0 and r.queue_wait_ms >= 0 for r in invocations)
        assert {r.index_phase for r in invocations} == {"local_projection", "local_verifier", "interpretation"}

    with caplog.at_level(logging.INFO):
        exercise(tmp_path, run)
