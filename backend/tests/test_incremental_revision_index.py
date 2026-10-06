r"""本文件对外提供 Revision 继承索引的真实 PostgreSQL 合同测试。

输入为已迁移隔离数据库、真实来源边及计数模型；输出为追加继承、来源重绑定、回退、质量、预算与原子竞争断言。
具体工作流为先通过 Observation 服务冻结模型调用来源，再发布 R1／R2，经局部继承及必需综合构建，从新 session 检查 index／record、分阶段成本和 Loop 用量；历史 v3/v4 按原身份恢复。
示例：python -m pytest backend/tests/test_incremental_revision_index.py -q。
"""

import asyncio
import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.context_expansion.artifact_repository import (
    SemanticDerivationArtifactRepository,
)
from backend.app.desktop.agent_loop.context_expansion.models import (
    LoopIndexBudgetReservation,
    LoopSegmentProjectionRecord,
    LoopSemanticIndexArtifact,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_index import (
    RevisionSemanticIndex,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_indexer import (
    ProtocolSafeRevisionSegmenter,
    RevisionSemanticIndexer,
)
from backend.app.desktop.agent_loop.models import LoopBudgetUsage, LoopDelegationGrant
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.context_evolution.models import ContextPublicationReceipt
from backend.tests.incremental_index_support import (
    Checkpoints,
    ModelProbe,
    messages,
    observation,
    revision,
)
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def exercise(tmp_path, operation):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        seed = await _seed_loop(
            sessions, tmp_path, label="inc", started_at=datetime.now(UTC)
        )
        async with sessions.begin() as session:
            grant = await session.scalar(
                select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == seed["loop_id"]
                )
            )
            grant.budgets = {
                **grant.budgets,
                "max_model_calls": 10000,
                "max_input_tokens": 10000000,
                "max_output_tokens": 10000000,
            }
        await LoopObservationService(sessions, Checkpoints()).capture(
            seed["loop_id"], seed["round_id"]
        )
        try:
            await operation(sessions, seed)
        finally:
            await _stop(seed["service"], seed["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def _phase_costs(calls):
    import json

    phases = {}
    for phase, selected in (
        ("local", [(r, p) for r, p in calls if r != "interpretation"]),
        ("interpretation", [(r, p) for r, p in calls if r == "interpretation"]),
        ("total", calls),
    ):
        phases[phase] = {
            "calls": len(selected),
            "payload_bytes": sum(len(json.dumps(p).encode()) for _, p in selected),
            "fake_input_tokens": sum(
                len(json.dumps(p).encode()) // 4 for _, p in selected
            ),
            "fake_output_tokens": len(selected) * 10,
        }
    return phases


def test_long_history_reuses_100_segments_and_rebinds_all_evidence(tmp_path):
    async def run(sessions, seed):
        probe = ModelProbe()
        large_policy = {"expansion_resources": {"max_request_input_tokens": 1_000_000}}
        service = probe.service(sessions)
        history = messages(1200)
        first = await revision(sessions, seed["context_id"], history)
        result = await service.build(observation(seed, first, budget=large_policy))
        assert result.blocker_code is None, result.blocker_summary
        old = result.indexes[0]
        assert len(probe.local_calls) == 200 and old.quality_state == "complete"
        new = await revision(
            sessions, seed["context_id"], history + messages(2, 1200), parent=first
        )
        probe.calls.clear()
        result = await service.build(observation(seed, new, budget=large_policy))
        assert result.blocker_code is None, result.blocker_summary
        current = result.indexes[0]
        incremental_calls = tuple(probe.calls)
        assert current.inheritance.mode == "incremental"
        assert len(current.inheritance.reused_segment_ids) == 100
        assert len(probe.local_calls) == 2
        assert [
            m["message_id"] for m in probe.calls[0][1]["segments"][0]["messages"]
        ] == ["m-1200", "m-1201"]
        assert all(
            ref.source == new.ref
            for unit in current.semantic_units
            for ref in unit.evidence_refs
        )
        assert current.coverage.covered_message_count == 1202
        async with sessions() as session:
            old_row = await session.get(LoopSemanticIndexArtifact, old.index_id)
            assert RevisionSemanticIndex.model_validate(old_row.payload) == old
            usage = await session.get(LoopBudgetUsage, seed["loop_id"])
            assert usage.model_calls == 204
        probe.calls.clear()
        await service.build(observation(seed, new, budget=large_policy))
        assert probe.calls == []
        async with sessions() as session:
            assert (
                await session.get(LoopBudgetUsage, seed["loop_id"])
            ).model_calls == 204
        cold_probe = ModelProbe(version="cold-comparison")
        cold = await cold_probe.service(sessions).build(
            observation(seed, new, budget=large_policy)
        )
        assert cold.blocker_code is None, cold.blocker_summary
        assert cold.indexes[0].semantic_units == current.semantic_units
        assert cold.indexes[0].coverage == current.coverage
        assert len(cold_probe.local_calls) == 202
        import json
        from pathlib import Path

        Path(".tmp/index-performance.json").write_text(
            json.dumps(
                {
                    "history_messages": 1202,
                    "stable_segments": 100,
                    "fixture": "deterministic local facts, completed empty interpretation, no global verifier needed",
                    "cold_phases": _phase_costs(cold_probe.calls),
                    "incremental_phases": _phase_costs(incremental_calls),
                    "cold_calls": len(cold_probe.calls),
                    "cold_local_calls": len(cold_probe.local_calls),
                    "cold_interpretation_calls": len(cold_probe.interpretation_calls),
                    "incremental_calls": len(incremental_calls),
                    "incremental_local_calls": 2,
                    "incremental_interpretation_calls": sum(
                        role == "interpretation" for role, _ in incremental_calls
                    ),
                    "cold_payload_bytes": sum(
                        len(json.dumps(p).encode()) for _, p in cold_probe.calls
                    ),
                    "incremental_payload_bytes": sum(
                        len(json.dumps(p).encode()) for _, p in incremental_calls
                    ),
                    "cold_fake_input_tokens": sum(
                        len(json.dumps(p).encode()) // 4 for _, p in cold_probe.calls
                    ),
                    "incremental_fake_input_tokens": sum(
                        len(json.dumps(p).encode()) // 4 for _, p in incremental_calls
                    ),
                    "exact_hit_calls": 0,
                    "full_history_reads": True,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    exercise(tmp_path, run)


@pytest.mark.parametrize(
    "count,append,reused,recomputed",
    [(13, 2, 1, 1), (12, 1, 1, 1), (0, 1, 0, 1), (12, 0, 1, 0), (0, 0, 0, 0)],
)
def test_append_tail_boundaries(tmp_path, count, append, reused, recomputed):
    async def run(sessions, seed):
        probe = ModelProbe()
        service = probe.service(sessions)
        first = await revision(sessions, seed["context_id"], messages(count))
        before = await service.build(observation(seed, first))
        assert before.blocker_code is None, before.blocker_summary
        new = await revision(
            sessions, seed["context_id"], messages(count + append), parent=first
        )
        probe.calls.clear()
        result = await service.build(observation(seed, new))
        assert result.blocker_code is None, result.blocker_summary
        receipt = result.indexes[0].inheritance
        assert (
            len(receipt.reused_segment_ids),
            len(receipt.recomputed_segment_ids),
        ) == (reused, recomputed)
        assert len(probe.local_calls) == recomputed * 2

    exercise(tmp_path, run)


def test_curator_retrieves_inherited_failure_and_new_fix(tmp_path):
    async def run(sessions, seed):
        from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import (
            AuthorizedSemanticRetriever,
            PlanningRetrievalSession,
            PortfolioIndexCatalog,
            RetrievalBudget,
            SemanticRetrievalQuery,
        )
        from backend.app.desktop.agent_loop.patrol_runtime import (
            CuratorCoordinationStage,
        )
        from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope

        probe = ModelProbe()
        service = probe.service(sessions)
        history = messages(12)
        history[0]["content"] = "Authentication tests failed before the fix"
        first = await revision(sessions, seed["context_id"], history)
        old = (await service.build(observation(seed, first))).indexes[0]
        added = messages(1, 12)
        added[0]["content"] = "Authentication tests passed after the fix"
        target = await revision(
            sessions, seed["context_id"], history + added, parent=first
        )
        frozen = observation(seed, target)
        envelope = LoopObservationEnvelope(
            **vars(frozen), loop_revision=1, goal_revision=1, workspace={"revision": 1}
        )
        probe.calls.clear()
        payload = await CuratorCoordinationStage(
            sessions, index_service=service
        )._derivation_input(envelope)
        assert len(probe.local_calls) == 2
        repository = SemanticDerivationArtifactRepository()
        async with sessions() as session:
            indexes = await repository.indexes_by_ids(
                session, payload["semantic_index_ids"]
            )
        current = indexes[0]
        assert current.inheritance.mode == "incremental"
        assert len(current.inheritance.reused_segment_ids) == 1
        assert current.coverage.covered_message_count == 13
        catalog = PortfolioIndexCatalog.model_validate(
            payload["portfolio_index_catalog"]
        )
        planning = PlanningRetrievalSession.create(
            observation_hash="e" * 64,
            frontier_hash=envelope.observed_frontier_hash,
            catalog=catalog,
            planner_version="curator-consumer-test",
            retrieval_version=AuthorizedSemanticRetriever.VERSION,
            budget=RetrievalBudget(
                max_queries=1,
                max_candidates=16,
                max_exact_reads=16,
                max_model_calls=0,
                max_tokens=10000,
            ),
        )
        candidates = AuthorizedSemanticRetriever().retrieve(
            planning,
            SemanticRetrievalQuery.create(
                text="Authentication tests fix", kinds=("semantic_unit",), limit=16
            ),
            indexes,
        )
        statements = {candidate.descriptor for candidate in candidates}
        assert history[0]["content"] in statements
        assert added[0]["content"] in statements
        assert all(candidate.source == target.ref for candidate in candidates)
        assert all(
            ref.source == first.ref
            for unit in old.semantic_units
            for ref in unit.evidence_refs
        )
        probe.calls.clear()
        assert (await service.build(envelope)).indexes == indexes
        assert probe.calls == []

    exercise(tmp_path, run)


@pytest.mark.parametrize(
    "change", ["text", "reorder", "delete", "multimodal", "role", "tool"]
)
def test_same_ids_do_not_authorize_changed_prefix(tmp_path, change):
    async def run(sessions, seed):
        probe = ModelProbe()
        service = probe.service(sessions)
        history = messages(14)
        first = await revision(sessions, seed["context_id"], history)
        await service.build(observation(seed, first))
        changed = [dict(m) for m in history]
        if change == "text":
            changed[0]["content"] = "different"
        elif change == "reorder":
            changed[0], changed[1] = changed[1], changed[0]
        elif change == "delete":
            changed.pop()
        elif change == "role":
            changed[0]["role"] = "assistant"
        elif change == "multimodal":
            changed[0]["content"] = [{"type": "text", "text": "changed"}]
        else:
            changed[0].update(
                role="assistant", tool_calls=[{"id": "t1", "name": "read", "args": {}}]
            )
            changed[1].update(role="tool", tool_call_id="t1")
        new = await revision(sessions, seed["context_id"], changed, parent=first)
        result = await service.build(observation(seed, new))
        assert result.blocker_code is None, result.blocker_summary
        assert result.indexes[0].inheritance.full_build_reason == "non_append_history"

    exercise(tmp_path, run)


@pytest.mark.parametrize(
    "origin", ["compression", "compression_restore", "curation", "definition_update"]
)
def test_non_append_evolution_boundaries_are_full(tmp_path, origin):
    async def run(sessions, seed):
        probe = ModelProbe()
        service = probe.service(sessions)
        first = await revision(sessions, seed["context_id"], messages(12))
        await service.build(observation(seed, first))
        new = await revision(
            sessions, seed["context_id"], messages(13), parent=first, origin=origin
        )
        result = await service.build(observation(seed, new))
        assert result.blocker_code is None, result.blocker_summary
        assert result.indexes[0].inheritance.full_build_reason == "evolution_boundary"

    exercise(tmp_path, run)


def test_skipped_unindexed_revisions_and_uncommitted_source(tmp_path):
    async def run(sessions, seed):
        probe = ModelProbe()
        service = probe.service(sessions)
        first = await revision(sessions, seed["context_id"], messages(12))
        await service.build(observation(seed, first))
        middle = await revision(
            sessions, seed["context_id"], messages(13), parent=first
        )
        new = await revision(sessions, seed["context_id"], messages(14), parent=middle)
        result = await service.build(observation(seed, new))
        assert result.indexes[0].inheritance.mode == "incremental"
        async with sessions.begin() as session:
            await session.execute(
                delete(ContextPublicationReceipt).where(
                    ContextPublicationReceipt.revision_id == middle.ref.revision_id
                )
            )
        newest = await revision(
            sessions, seed["context_id"], messages(15), parent=middle
        )
        result = await service.build(observation(seed, newest))
        assert result.indexes[0].inheritance.full_build_reason == "uncommitted_source"

    exercise(tmp_path, run)


@pytest.mark.parametrize(
    "invalid,verdict",
    [
        ("foreign", "supported"),
        ("quote", "supported"),
        ("empty", "supported"),
        (None, "unsupported"),
        (None, "unknown"),
    ],
)
def test_quality_and_rejection_records_survive_inheritance(tmp_path, invalid, verdict):
    async def run(sessions, seed):
        probe = ModelProbe(invalid=invalid, verdict=verdict)
        service = probe.service(sessions)
        first = await revision(sessions, seed["context_id"], messages(12))
        result = await service.build(observation(seed, first))
        assert result.blocker_code is None, result.blocker_summary
        old = result.indexes[0]
        assert old.quality_state == "degraded"
        new = await revision(sessions, seed["context_id"], messages(12), parent=first)
        probe.calls.clear()
        result = await service.build(observation(seed, new))
        assert result.blocker_code is None, result.blocker_summary
        assert result.indexes[0].quality_state == "degraded" and not probe.local_calls
        assert len(probe.interpretation_calls) == 1
        assert result.indexes[0].rejected_units == old.rejected_units

    exercise(tmp_path, run)


@pytest.mark.parametrize(
    "kind", ["stale", "scope", "zero_budget", "invalid_tool", "overflow"]
)
def test_fatal_boundaries_never_publish_partial_indexes(tmp_path, kind):
    async def run(sessions, seed):
        probe = ModelProbe()
        service = probe.service(
            sessions, catalog_max_descriptor_chars=1 if kind == "overflow" else None
        )
        history = messages(2)
        if kind == "invalid_tool":
            history[0].update(
                role="assistant",
                tool_calls=[{"id": "missing", "name": "read", "args": {}}],
            )
        target = await revision(sessions, seed["context_id"], history)
        obs = observation(
            seed,
            target,
            budget={"max_model_calls": 0} if kind == "zero_budget" else None,
            scope=["other"] if kind == "scope" else None,
            hash_override="f" * 64 if kind == "stale" else None,
        )
        result = await service.build(obs)
        assert result.blocker_code is not None
        async with sessions() as session:
            assert not await session.scalar(
                select(LoopSemanticIndexArtifact.index_id).where(
                    LoopSemanticIndexArtifact.revision_id == target.ref.revision_id
                )
            )
            assert not await session.scalar(
                select(LoopSegmentProjectionRecord.record_id).where(
                    LoopSegmentProjectionRecord.context_id == target.ref.context_id
                )
            )
        if kind in {"stale", "scope", "zero_budget", "invalid_tool"}:
            assert probe.calls == []

    exercise(tmp_path, run)


def test_corrupted_projection_record_is_an_integrity_blocker(tmp_path):
    async def run(sessions, seed):
        probe = ModelProbe()
        service = probe.service(sessions)
        first = await revision(sessions, seed["context_id"], messages(12))
        result = await service.build(observation(seed, first))
        record_id = result.indexes[0].inheritance.record_ids[0]
        async with sessions.begin() as session:
            row = await session.get(LoopSegmentProjectionRecord, record_id)
            row.payload = {**row.payload, "fallback": True}
        new = await revision(sessions, seed["context_id"], messages(13), parent=first)
        probe.calls.clear()
        result = await service.build(observation(seed, new))
        assert (
            result.blocker_code == "portfolio_index_failed"
            and "integrity" in result.blocker_summary
        )
        assert probe.calls == []

    exercise(tmp_path, run)


def test_concurrent_builds_return_persisted_winner_and_bill_both(tmp_path):
    async def run(sessions, seed):
        arrivals = 0
        ready = asyncio.Event()

        async def gate(role, payload):
            nonlocal arrivals
            if role == "projector":
                arrivals += 1
                if arrivals == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 5)

        a = ModelProbe(variant=" A", gate=gate)
        b = ModelProbe(variant=" B", gate=gate)
        first = await revision(sessions, seed["context_id"], messages(2))
        results = await asyncio.gather(
            a.service(sessions).build(observation(seed, first)),
            b.service(sessions).build(observation(seed, first)),
        )
        assert all(r.blocker_code is None for r in results), [
            r.blocker_summary for r in results
        ]
        assert results[0].indexes[0].index_id == results[1].indexes[0].index_id
        async with sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(LoopSemanticIndexArtifact)
                    .where(
                        LoopSemanticIndexArtifact.revision_id == first.ref.revision_id
                    )
                )
                == 1
            )
            assert (
                await session.get(LoopBudgetUsage, seed["loop_id"])
            ).model_calls == 6
            for result in results:
                stored = await SemanticDerivationArtifactRepository().indexes_by_ids(
                    session, (result.indexes[0].index_id,)
                )
                assert stored == (result.indexes[0],)

    exercise(tmp_path, run)


@pytest.mark.parametrize("config", ["model", "segmenter", "grounding"])
def test_effective_configuration_changes_force_full_build(
    tmp_path, config, monkeypatch
):
    async def run(sessions, seed):
        from backend.app.desktop.agent_loop.context_expansion.semantic_grounding import (
            SemanticGroundingValidator,
        )

        probe = ModelProbe()
        service = probe.service(sessions)
        first = await revision(sessions, seed["context_id"], messages(12))
        await service.build(observation(seed, first))
        if config == "model":
            probe.version = "v2"
        elif config == "grounding":
            monkeypatch.setattr(SemanticGroundingValidator, "VERSION", "changed-rules")
        newer = service if config == "model" else probe.service(sessions)
        if config == "segmenter":
            newer._indexer = RevisionSemanticIndexer(
                ProtocolSafeRevisionSegmenter(max_messages=6)
            )
        target = await revision(
            sessions, seed["context_id"], messages(13), parent=first
        )
        probe.calls.clear()
        result = await newer.build(observation(seed, target))
        assert result.blocker_code is None, result.blocker_summary
        assert result.indexes[0].inheritance.mode == "full"
        assert len(probe.local_calls) == len(result.indexes[0].segments) * 2

    exercise(tmp_path, run)


def test_missing_proofs_and_records_trigger_full_build(tmp_path):
    async def run(sessions, seed):
        probe = ModelProbe()
        service = probe.service(sessions)
        first = await revision(sessions, seed["context_id"], messages(12))
        built = await service.build(observation(seed, first))
        async with sessions.begin() as session:
            await session.execute(
                delete(LoopSegmentProjectionRecord).where(
                    LoopSegmentProjectionRecord.record_id
                    == built.indexes[0].inheritance.record_ids[0]
                )
            )
        new = await revision(sessions, seed["context_id"], messages(13), parent=first)
        result = await service.build(observation(seed, new))
        assert (
            result.indexes[0].inheritance.full_build_reason
            == "missing_projection_records"
        )

    exercise(tmp_path, run)


def test_bounded_search_and_multi_source_do_not_inherit(tmp_path):
    async def run(sessions, seed):
        probe = ModelProbe()
        service = probe.service(sessions)
        first = await revision(sessions, seed["context_id"], messages(12))
        await service.build(observation(seed, first))
        parent = first
        for _ in range(65):
            parent = await revision(
                sessions, seed["context_id"], messages(12), parent=parent
            )
        result = await service.build(observation(seed, parent))
        assert (
            result.indexes[0].inheritance.full_build_reason
            == "ancestry_search_exhausted"
        )
        target = await revision(
            sessions, seed["context_id"], messages(13), sources=[first.ref, parent.ref]
        )
        result = await service.build(observation(seed, target))
        assert result.indexes[0].inheritance.full_build_reason == "evolution_boundary"

    exercise(tmp_path, run)


def test_hypothesis_and_missing_verdict_are_never_promoted(tmp_path):
    async def run(sessions, seed):
        from backend.app.desktop.agent_loop.context_expansion.portfolio_index import (
            PortfolioSemanticIndexService,
        )
        from backend.tests.incremental_index_support import Checkpoints

        hypothesis = ModelProbe(authority="hypothesis")
        first = await revision(sessions, seed["context_id"], messages(1))
        result = await hypothesis.service(sessions).build(observation(seed, first))
        assert result.indexes[0].semantic_units[0].authority == "hypothesis"
        assert len(hypothesis.local_calls) == 1
        missing = ModelProbe()
        service = PortfolioSemanticIndexService(
            sessions,
            Checkpoints(),
            semantic_projector_factory=missing.factory("projector"),
        )
        new = await revision(sessions, seed["context_id"], messages(2), parent=first)
        result = await service.build(observation(seed, new))
        assert result.indexes[0].quality_state == "degraded"
        assert {r.code for r in result.indexes[0].rejected_units} == {
            "claim_support_missing"
        }

    exercise(tmp_path, run)


def test_cancellation_and_provider_failure_preserve_actual_usage(tmp_path):
    async def run(sessions, seed):
        started = asyncio.Event()
        release = asyncio.Event()

        async def gate(role, payload):
            if role == "verifier":
                started.set()
                await release.wait()

        probe = ModelProbe(gate=gate)
        service = probe.service(sessions)
        first = await revision(sessions, seed["context_id"], messages(2))
        task = asyncio.create_task(service.build(observation(seed, first)))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with sessions() as session:
            rows = (
                await session.scalars(
                    select(LoopIndexBudgetReservation).where(
                        LoopIndexBudgetReservation.loop_id == seed["loop_id"]
                    )
                )
            ).all()
            assert rows and all(row.settled_at is not None for row in rows)
            assert (
                await session.get(LoopBudgetUsage, seed["loop_id"])
            ).model_calls == 1
            assert not await session.scalar(
                select(LoopSemanticIndexArtifact.index_id).where(
                    LoopSemanticIndexArtifact.revision_id == first.ref.revision_id
                )
            )
        failing = ModelProbe(fail=True)
        result = await failing.service(sessions).build(observation(seed, first))
        assert result.blocker_code == "portfolio_index_failed"
        async with sessions() as session:
            assert (
                await session.get(LoopBudgetUsage, seed["loop_id"])
            ).model_calls == 3

    exercise(tmp_path, run)


def test_tool_atom_and_single_request_window_are_not_split(tmp_path):
    async def run(sessions, seed):
        history = [
            {
                "id": "call",
                "role": "assistant",
                "content": "Read files",
                "tool_calls": [
                    {"id": f"tool-{i}", "name": "read", "args": {}} for i in range(14)
                ],
            }
        ]
        history += [
            {
                "id": f"result-{i}",
                "role": "tool",
                "tool_call_id": f"tool-{i}",
                "content": "result",
            }
            for i in range(14)
        ]
        probe = ModelProbe()
        service = probe.service(sessions)
        first = await revision(sessions, seed["context_id"], history)
        result = await service.build(observation(seed, first))
        assert result.blocker_code is None, result.blocker_summary
        assert len(result.indexes[0].segments) == 1 and len(probe.local_calls) == 2
        huge = [dict(m) for m in history]
        huge[0]["content"] = "x" * 100000
        new = await revision(sessions, seed["context_id"], huge, parent=first)
        probe.calls.clear()
        result = await service.build(
            observation(
                seed,
                new,
                budget={"expansion_resources": {"max_request_input_tokens": 1024}},
            )
        )
        assert result.blocker_code == "portfolio_index_budget" and probe.calls == []

    exercise(tmp_path, run)


@pytest.mark.parametrize("schema_version", ["v3", "v4"])
def test_historical_index_and_planning_session_remain_readable(
    tmp_path, schema_version
):
    async def run(sessions, seed):
        from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import (
            PlanningRetrievalSession,
            PortfolioIndexCatalog,
            RetrievalBudget,
        )

        first = await revision(sessions, seed["context_id"], messages(2))
        raw = RevisionSemanticIndexer().index(
            source=first.ref,
            source_content_hash=first.content_hash,
            context_role="primary",
            active_objective="old session",
            raw_messages=tuple(messages(2)),
        )
        excluded = {
            "index_id",
            "coverage",
            "inheritance",
            "index_schema_version",
            "segmenter_version",
        }
        values = {
            n: getattr(raw, n) for n in type(raw).model_fields if n not in excluded
        }
        legacy = RevisionSemanticIndex.create(
            **values,
            index_schema_version=f"revision-semantic-index-{schema_version}",
            segmenter_version="protocol-safe-segmenter-v1",
        )
        repository = SemanticDerivationArtifactRepository()
        catalog = PortfolioIndexCatalog.create(
            frontier_hash="f" * 64, indexes=(legacy,)
        )
        planning = PlanningRetrievalSession.create(
            observation_hash="e" * 64,
            frontier_hash="f" * 64,
            catalog=catalog,
            planner_version="old-planner",
            retrieval_version="old-retriever",
            budget=RetrievalBudget(
                max_queries=1,
                max_candidates=10,
                max_exact_reads=10,
                max_model_calls=1,
                max_tokens=10000,
            ),
        )
        async with sessions.begin() as session:
            await repository.put_index(session, legacy)
            await repository.save_session(
                session, planning, loop_id=seed["loop_id"], round_id=seed["round_id"]
            )
            row = await session.get(LoopSemanticIndexArtifact, legacy.index_id)
            payload = dict(row.payload)
            payload.pop("inheritance")
            row.payload = payload
        probe = ModelProbe()
        result = await probe.service(sessions).build(observation(seed, first))
        assert result.blocker_code is None, result.blocker_summary
        assert (
            result.indexes[0].index_id != legacy.index_id
            and len(probe.local_calls) == 2
        )
        async with sessions() as session:
            saved = await repository.get_session(session, planning.session_id)
            assert await repository.indexes_by_ids(
                session, saved.authorized_index_ids
            ) == (legacy,)

    exercise(tmp_path, run)


def test_frozen_revision_and_control_memories_are_unchanged_by_indexing(tmp_path):
    async def run(sessions, seed):
        from sqlalchemy import text

        from backend.app.desktop.context_evolution import ContextRevisionRepository

        first = await revision(sessions, seed["context_id"], messages(12))
        captured = observation(seed, first)
        new = await revision(sessions, seed["context_id"], messages(13), parent=first)

        async def snapshot():
            async with sessions() as session:
                return {
                    t: (
                        await session.scalars(
                            text(
                                f"SELECT to_jsonb(x) FROM {t} x ORDER BY to_jsonb(x)::text"
                            )
                        )
                    ).all()
                    for t in (
                        "desktop_context_revisions",
                        "desktop_context_publications",
                        "desktop_context_revision_sources",
                        "loop_task_progress",
                        "loop_progress_heads",
                        "loop_context_memberships",
                    )
                }

        before = await snapshot()
        probe = ModelProbe()
        result = await probe.service(sessions).build(captured)
        assert result.blocker_code is None, result.blocker_summary
        assert result.indexes[0].source == first.ref
        async with sessions() as session:
            assert (
                await ContextRevisionRepository().current(session, seed["context_id"])
            ).ref == new.ref
        assert await snapshot() == before

    exercise(tmp_path, run)


def test_model_calls_hold_no_database_transaction(tmp_path):
    async def run(sessions, seed):
        observed = []

        async def gate(role, payload):
            checked_out = sessions.kw["bind"].pool.checkedout()
            observed.append(checked_out)
            assert checked_out == 0

        probe = ModelProbe(gate=gate)
        first = await revision(sessions, seed["context_id"], messages(2))
        result = await probe.service(sessions).build(observation(seed, first))
        assert result.blocker_code is None, result.blocker_summary
        assert observed == [0] * len(probe.calls)

    exercise(tmp_path, run)


def test_retry_stage_does_not_replay_a_previous_failure(tmp_path):
    async def run(sessions, seed):
        probe = ModelProbe(fail=True)
        service = probe.service(sessions)
        first = await revision(sessions, seed["context_id"], messages(2))
        failed = await service.build(observation(seed, first))
        assert failed.blocker_code and failed.stage_record.failure_code
        probe.fail = False
        result = await service.build(observation(seed, first))
        assert result.blocker_code is None, result.blocker_summary
        assert result.stage_record.failure_code is None
        replay = await service.build(observation(seed, first))
        assert replay.stage_record == result.stage_record

    exercise(tmp_path, run)


def test_configuration_change_during_model_work_is_blocked(tmp_path):
    async def run(sessions, seed):
        probe = ModelProbe()

        async def gate(role, payload):
            if role == "projector":
                probe.version = "changed-during-build"

        probe.gate = gate
        first = await revision(sessions, seed["context_id"], messages(2))
        result = await probe.service(sessions).build(observation(seed, first))
        assert result.blocker_code == "portfolio_index_failed"
        assert "stale" in result.blocker_summary
        assert len(probe.local_calls) == 1

    exercise(tmp_path, run)


def test_cross_context_source_and_multi_portfolio_concurrency(tmp_path):
    async def run(sessions, seed):
        import uuid

        from backend.app.desktop.context_evolution import ContextRevisionRepository
        from backend.app.desktop.models import DesktopThread

        first = await revision(sessions, seed["context_id"], messages(12))
        async with sessions.begin() as session:
            parent = await session.get(DesktopThread, seed["context_id"])
            context_id = uuid.uuid4().hex
            session.add(
                DesktopThread(
                    task_id=context_id,
                    workspace_id=parent.workspace_id,
                    thread_id=uuid.uuid4().hex,
                    title="other",
                )
            )
            await session.flush()
            from backend.app.desktop.context_evolution import (
                ContextRevisionContract,
                ContextRevisionRef,
            )

            ref = ContextRevisionRef(
                context_id=context_id,
                revision_id=uuid.uuid4().hex,
                generation=1,
                execution_thread_id=uuid.uuid4().hex,
                checkpoint_ns="",
                checkpoint_id=uuid.uuid4().hex,
                payload_mode="checkpoint",
            )
            other = ContextRevisionContract(
                ref=ref,
                content_hash="b" * 64,
                projection_status="valid",
                origin_kind="root",
                created_at=datetime.now(UTC),
                execution_messages=tuple(messages(12)),
            )
            await ContextRevisionRepository().insert(session, other)
            await ContextRevisionRepository().switch_current(session, ref, None)
        probe = ModelProbe()
        service = probe.service(sessions)
        await service.build(observation(seed, first))
        target = await revision(
            sessions, seed["context_id"], messages(13), sources=[other.ref]
        )
        result = await service.build(observation(seed, target))
        assert result.indexes[0].inheritance.full_build_reason == "cross_context_source"
        arrivals = 0
        ready = asyncio.Event()

        async def gate(role, payload):
            nonlocal arrivals
            if role == "projector":
                arrivals += 1
                if arrivals == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 5)

        parallel = ModelProbe(gate=gate, version="parallel")
        obs = observation(
            seed, target, scope=[first.ref.context_id, other.ref.context_id]
        )
        obs.portfolio_frontier += (observation(seed, other).portfolio_frontier[0],)
        result = await parallel.service(sessions, concurrency=2).build(obs)
        assert result.blocker_code is None, result.blocker_summary
        assert parallel.max_active == 2
        assert len(result.indexes) == 2

    exercise(tmp_path, run)
