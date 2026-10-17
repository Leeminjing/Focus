"""Opt-in real-provider comparison; each case uses an isolated Context and cold cache.

Run with FOCUS_INDEX_REAL_MODEL=1. FOCUS_INDEX_BENCHMARK_OUTPUT selects the report
directory; FOCUS_INDEX_BENCHMARK_MESSAGES and FOCUS_INDEX_BENCHMARK_REPEATS control
the synthetic workload and sample count. No customer conversation is sent.
The production builder selects its initial window; this benchmark does not
patch model input or interpretation behavior.
"""

import json
import logging
import os
from collections import Counter
from pathlib import Path
from time import perf_counter

import pytest
from focus.config.app_config import get_app_config
from sqlalchemy import select

from backend.app.desktop.agent_loop.context_expansion.portfolio_index import PortfolioSemanticIndexService
from backend.app.desktop.agent_loop.context_expansion.semantic_indexer import (
    ProtocolSafeRevisionSegmenter,
    RevisionSemanticIndexer,
)
from backend.app.desktop.agent_loop.derivation_worker import RoleBoundStructuredModel
from backend.app.desktop.agent_loop.models import LoopBudgetUsage
from backend.tests.incremental_index_support import Checkpoints, observation, revision
from backend.tests.test_incremental_revision_index import exercise


pytestmark = [
    pytest.mark.usefixtures("isolated_postgres_database"),
    pytest.mark.skipif(os.environ.get("FOCUS_INDEX_REAL_MODEL") != "1", reason="real-provider benchmark is opt-in"),
]


def _history(count):
    return [
        {"id": f"bench-{i}", "role": "human", "content": (
            f"Verification batch B{i:03d}: {40 + i} tests passed and {i % 3} tests failed. "
            f"The result belongs only to batch B{i:03d}."
        )}
        for i in range(count)
    ]


def _quality(index):
    represented = {
        ref.message_id for unit in index.semantic_units
        if unit.unit_id in index.projected_unit_ids
        for ref in unit.evidence_refs
    }
    return {
        "state": index.quality_state,
        "message_count": len(index.messages),
        "represented_messages": len(represented),
        "missing_message_ids": sorted({m.message_id for m in index.messages} - represented),
        "fallback_segments": len(index.fallback_segment_ids),
        "rejection_codes": dict(Counter(r.code for r in index.rejected_units)),
        "all_sources_bound": all(ref.source == index.source for u in index.semantic_units for ref in u.evidence_refs),
        "statements": [{"text": u.statement, "authority": u.authority,
                        "message_ids": [r.message_id for r in u.evidence_refs]}
                       for u in index.semantic_units if u.unit_id in index.projected_unit_ids],
    }


@pytest.mark.parametrize("repeat", range(int(os.environ.get("FOCUS_INDEX_BENCHMARK_REPEATS", "1"))))
@pytest.mark.parametrize("segment_size,concurrency", [(12, 1), (12, 4), (30, 1), (30, 4)])
def test_real_provider_latency_matrix(tmp_path, caplog, segment_size, concurrency, repeat):
    async def run(sessions, seed):
        config = get_app_config("config.yaml")
        selected = config.get_model(config.resolve_default_model_name())
        count = int(os.environ.get("FOCUS_INDEX_BENCHMARK_MESSAGES", "55"))
        output = Path(os.environ.get("FOCUS_INDEX_BENCHMARK_OUTPUT", ".tmp/index-latency"))
        output.mkdir(parents=True, exist_ok=True)
        path = output / f"segment-{segment_size}-concurrency-{concurrency}-repeat-{repeat}.json"
        report = {
            "configured_model": selected.name, "provider_model": selected.model,
            "output_token_limit": selected.curation_max_output_tokens,
            "segment_size": segment_size, "concurrency": concurrency, "repeat": repeat,
            "fixture": "unique synthetic batch verification facts; no customer data",
            "history_messages": count, "append_messages": 2, "phases": {},
            "initial_originals_policy": "capacity-and-budget-checked-full-originals-production",
        }
        invocations = []

        def factory(role):
            class MeasuredModel(RoleBoundStructuredModel):
                async def _invoke(self, schema, system, payload, validator):
                    started = perf_counter()
                    result = None
                    try:
                        result = await super()._invoke(schema, system, payload, validator)
                        return result
                    finally:
                        invocations.append({
                            "schema": schema.__name__, "duration_ms": (perf_counter() - started) * 1000,
                            "response_json_chars": len(result.model_dump_json()) if result is not None else None,
                            "input_tokens": sum(a.get("input_tokens", 0) for a in self.last_attempt_records),
                            "output_tokens": sum(a.get("output_tokens", 0) for a in self.last_attempt_records),
                            "projected_messages": sum(len(s["messages"]) for s in payload.get("segments", ()))
                            if schema.__name__ == "SemanticProjectionProposal" else 0,
                            "attempts": [dict(a) for a in self.last_attempt_records],
                        })

            return lambda: MeasuredModel(config, role)

        service = PortfolioSemanticIndexService(
            sessions, Checkpoints(), concurrency=concurrency,
            semantic_projector_factory=factory("semantic_index_projector"),
            semantic_claim_verifier_factory=factory("claim_verifier"),
        )
        service._indexer = RevisionSemanticIndexer(ProtocolSafeRevisionSegmenter(max_messages=segment_size))

        async def measure(name, target):
            invocations.clear()
            log_start = len(caplog.records)
            async with sessions() as session:
                before = await session.get(LoopBudgetUsage, seed["loop_id"])
                billed_before = before.model_calls
            started = perf_counter()
            result = await service.build(observation(seed, target))
            elapsed = (perf_counter() - started) * 1000
            attempts = [a for call in invocations for a in call["attempts"]]
            metrics = [r for r in caplog.records[log_start:] if r.msg.startswith("semantic_index_model_invocation")]
            phase = {
                "duration_ms": elapsed, "blocker_code": result.blocker_code,
                "blocker_summary": result.blocker_summary,
                "model_calls": sum(a.get("model_calls", 0) for a in attempts),
                "input_tokens": sum(a.get("input_tokens", 0) for a in attempts),
                "output_tokens": sum(a.get("output_tokens", 0) for a in attempts),
                "retries": sum(int(a.get("attempt", 1)) > 1 for a in attempts),
                "error_categories": dict(Counter(a.get("failure_category") or a.get("outcome") for a in attempts if a.get("outcome") != "success")),
                "invocations": [{k: v for k, v in call.items() if k != "attempts"} for call in invocations],
                "locally_projected_messages": sum(call["projected_messages"] for call in invocations),
                "timings": [{"phase": r.index_phase, "queue_wait_ms": r.queue_wait_ms,
                             "execution_ms": r.execution_ms, "admission_ms": r.admission_ms} for r in metrics],
            }
            phase["revision_timings"] = [
                {name: getattr(record, name) for name in (
                    "local_projection_ms", "interpretation_ms", "local_cache_hits", "projected_segments"
                )}
                for record in caplog.records[log_start:] if record.msg.startswith("semantic_index_revision")
            ]
            async with sessions() as session:
                after = await session.get(LoopBudgetUsage, seed["loop_id"])
                phase["billed_model_calls"] = after.model_calls - billed_before
            if result.indexes:
                index = result.indexes[0]
                phase.update(
                    quality=_quality(index), segments=len(index.segments),
                    reused_segments=len(index.inheritance.reused_segment_ids),
                    recomputed_segments=len(index.inheritance.recomputed_segment_ids),
                    interpretation_read_rounds=len(index.interpretation.read_requests),
                )
            report["phases"][name] = phase
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[index-bench] size={segment_size} concurrency={concurrency} repeat={repeat} "
                  f"phase={name} seconds={elapsed / 1000:.2f} calls={phase['model_calls']} "
                  f"quality={phase.get('quality', {}).get('state', result.blocker_code)}", flush=True)
            assert result.blocker_code is None, result.blocker_summary
            assert phase["model_calls"] == phase["billed_model_calls"], phase
            assert phase["quality"]["all_sources_bound"]
            return result

        history = _history(count)
        first = await revision(sessions, seed["context_id"], history)
        await measure("cold", first)
        newer = await revision(sessions, seed["context_id"], _history(count + 2), parent=first)
        appended = await measure("append", newer)
        cached = await measure("exact", newer)
        assert cached.indexes == appended.indexes and report["phases"]["exact"]["model_calls"] == 0
        assert all(phase["quality"]["state"] == "complete" for phase in report["phases"].values()), path

    with caplog.at_level(logging.INFO):
        exercise(tmp_path, run)


def test_production_prefill_preserves_cross_segment_correction(tmp_path):
    from backend.tests.test_incremental_index_real_model import observed_model_factory
    from backend.tests.test_revision_interpretation import NARRATIVE

    async def run(sessions, seed):
        config = get_app_config("config.yaml")
        calls, usage = [], []
        service = PortfolioSemanticIndexService(
            sessions, Checkpoints(),
            semantic_projector_factory=observed_model_factory(config, "semantic_index_projector", calls, usage),
            semantic_claim_verifier_factory=observed_model_factory(config, "claim_verifier", calls, usage),
        )
        service._indexer = RevisionSemanticIndexer(ProtocolSafeRevisionSegmenter(max_messages=1))
        first = await revision(sessions, seed["context_id"], NARRATIVE[:2])
        assert (await service.build(observation(seed, first))).blocker_code is None
        target = await revision(sessions, seed["context_id"], NARRATIVE, parent=first)
        started = perf_counter()
        result = await service.build(observation(seed, target))
        assert result.blocker_code is None, result.blocker_summary
        index = result.indexes[0]
        required = {"suspect", "reject", "diagnosis"}
        joint = [u for u in index.semantic_units if u.authority == "confirmed"
                 and required.issubset({ref.message_id for ref in u.evidence_refs})]
        text = " ".join(unit.statement.lower() for unit in joint)
        assert joint and any(word in text for word in ("thread", "线程"))
        assert any(word in text for word in ("ruled", "reject", "排除", "否定", "excluded"))
        assert index.quality_state == "complete" and not index.interpretation.read_requests
        output = Path(os.environ.get("FOCUS_INDEX_BENCHMARK_OUTPUT", ".tmp/index-latency"))
        output.mkdir(parents=True, exist_ok=True)
        (output / "production-cross-segment-quality.json").write_text(json.dumps({
            "configured_model": config.resolve_default_model_name(), "quality": _quality(index),
            "append_duration_ms": (perf_counter() - started) * 1000,
            "interpretation_read_rounds": len(index.interpretation.read_requests),
            "joint_statements": [unit.statement for unit in joint],
            "reused_segments": len(index.inheritance.reused_segment_ids),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    exercise(tmp_path, run)
