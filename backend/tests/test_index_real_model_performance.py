"""Opt-in real-provider comparison; each case uses an isolated Context and cold cache.

Run with FOCUS_INDEX_REAL_MODEL=1. FOCUS_INDEX_BENCHMARK_OUTPUT selects the report
directory; FOCUS_INDEX_BENCHMARK_MESSAGES and FOCUS_INDEX_BENCHMARK_REPEATS control
the synthetic workload and sample count. FOCUS_INDEX_BENCHMARK_CORPUS selects
an explicitly exported frozen corpus instead. Each case/configuration/repeat
gets a new Context in an isolated test database; source caches are never used.
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

from backend.app.desktop.patrol.agent_loop.context_expansion.portfolio_index import PortfolioIndexBuildResult, PortfolioSemanticIndexService
from backend.app.desktop.patrol.agent_loop.context_expansion.semantic_indexer import (
    ProtocolSafeRevisionSegmenter,
    RevisionSemanticIndexer,
)
from backend.app.desktop.patrol.agent_loop.context_expansion.projection_record_repository import ProjectionRecordRepository
from backend.app.desktop.patrol.agent_loop.context_expansion.contracts import stable_expansion_hash
from backend.app.desktop.contexts.evolution.schemas import ContextRevisionMessageView
from backend.app.desktop.patrol.agent_loop.workers.derivation_worker import RoleBoundStructuredModel
from backend.app.desktop.patrol.agent_loop.models import LoopBudgetUsage
from backend.tests.incremental_index_support import Checkpoints, observation, revision
from backend.tests.test_incremental_revision_index import exercise
from backend.scripts.semantic_index_corpus import load_corpus


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


def _quality(index, records=()):
    represented = {
        ref.message_id for unit in index.semantic_units
        if unit.unit_id in index.projected_unit_ids
        for ref in unit.evidence_refs
    }
    eligible = {m.message_id for m in index.messages if m.semantic_policy == "index"}
    contents = RevisionSemanticIndexer.message_contents(index.messages)
    proofs = (*records, *((index.interpretation,) if index.interpretation else ()))
    supports = [support for proof in proofs for draft in proof.drafts
                if draft.claim_key in proof.accepted_claim_keys for support in draft.supports]
    valid_supports = sum(s.message_id in contents and s.quote in contents[s.message_id] for s in supports)
    return {
        "state": index.quality_state,
        "message_count": len(index.messages),
        "represented_messages": len(represented),
        "missing_message_ids": sorted({m.message_id for m in index.messages} - represented),
        "eligible_messages": len(eligible),
        "accepted_citation_message_coverage": len(eligible & represented) / len(eligible) if eligible else 1.0,
        "semantic_coverage_method": "accepted citation message coverage proxy; semantic completeness requires review",
        "fallback_segments": len(index.fallback_segment_ids),
        "fallback_ratio": len(index.fallback_segment_ids) / len(index.segments) if index.segments else 0.0,
        "accepted_supports": len(supports), "valid_supports": valid_supports,
        "citation_validity": valid_supports / len(supports) if supports else None,
        "rejection_codes": dict(Counter(r.code for r in index.rejected_units)),
        "all_sources_bound": all(ref.source == index.source for u in index.semantic_units for ref in u.evidence_refs),
        "statements": [{"text": u.statement, "authority": u.authority,
                        "message_ids": [r.message_id for r in u.evidence_refs]}
                       for u in index.semantic_units if u.unit_id in index.projected_unit_ids],
    }


def _corpus_cases():
    path = os.environ.get("FOCUS_INDEX_BENCHMARK_CORPUS")
    return load_corpus(path).cases if path else (None,)


@pytest.mark.parametrize("case", _corpus_cases(), ids=lambda case: case.name if case else "synthetic")
@pytest.mark.parametrize("repeat", range(int(os.environ.get("FOCUS_INDEX_BENCHMARK_REPEATS", "1"))))
@pytest.mark.parametrize("token_limit", [None if value == "none" else int(value) for value in
                                      os.environ.get("FOCUS_INDEX_BENCHMARK_TOKEN_LIMITS", "none,4096").split(",")])
@pytest.mark.parametrize("segment_size", [12, 20, 30])
@pytest.mark.parametrize("prewarm", os.environ.get("FOCUS_INDEX_BENCHMARK_PREWARM", "off").split(","))
def test_real_provider_latency_matrix(tmp_path, caplog, segment_size, token_limit, repeat, case, prewarm):
    async def run(sessions, seed):
        assert prewarm in {"off", "on"}
        config = get_app_config("config.yaml").model_copy(deep=True)
        selected = config.get_model(config.resolve_default_model_name())
        role_limits = os.environ.get("FOCUS_INDEX_BENCHMARK_ROLE_LIMITS")
        if role_limits:
            validated = type(selected).model_validate({**selected.model_dump(),
                "derivation_max_output_tokens": json.loads(role_limits)})
            selected.derivation_max_output_tokens = validated.derivation_max_output_tokens
        concurrency = int(os.environ.get("FOCUS_INDEX_BENCHMARK_CONCURRENCY", "4"))
        count = int(os.environ.get("FOCUS_INDEX_BENCHMARK_MESSAGES", "55"))
        output = Path(os.environ.get("FOCUS_INDEX_BENCHMARK_OUTPUT", ".tmp/index-latency"))
        output.mkdir(parents=True, exist_ok=True)
        case_key = stable_expansion_hash("benchmark-case", case.model_dump(mode="json"))[:16] if case else "synthetic"
        preparation_suffix = "-prewarm" if prewarm == "on" else ""
        path = output / f"{case_key}-segment-{segment_size}-tokens-{token_limit}-concurrency-{concurrency}-repeat-{repeat}{preparation_suffix}.json"
        report = {
            "configured_model": selected.name, "provider_model": selected.model,
            "output_token_limit": selected.curation_max_output_tokens,
            "role_output_token_limits": selected.derivation_max_output_tokens,
            "local_prewarm": prewarm,
            "segment_size": segment_size, "concurrency": concurrency, "repeat": repeat,
            "segment_token_limit": token_limit,
            "fixture": case.category if case else "unique synthetic batch verification facts; no customer data",
            "case": case.name if case else "synthetic", "corpus_case_hash": case_key,
            "incremental_scenario_available": len(case.snapshots) > 1 if case else True,
            "frozen_sources": [{"source": s.source.model_dump(mode="json"),
                                "source_content_hash": s.source_content_hash, "semantic_hash": s.semantic_hash,
                                "messages": len(s.messages)} for s in case.snapshots] if case else [],
            "phases": {},
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
                            "role": role, "output_token_limit": self.max_output_tokens,
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
            segment_max_messages=segment_size, segment_max_estimated_tokens=token_limit,
            semantic_projector_factory=factory("semantic_index_projector"),
            semantic_claim_verifier_factory=factory("claim_verifier"),
            semantic_interpreter_factory=factory("semantic_index_interpreter"),
        )
        report["segmenter_fingerprint"] = service._indexer.segmenter_version
        report["model_identities"] = {
            "projector": service._model_identity(service._semantic_projector_factory),
            "verifier": service._model_identity(service._semantic_claim_verifier_factory),
            "interpreter": service._model_identity(service._semantic_interpreter_factory),
        }
        # Corpus exports already contain the versioned semantic view. Re-reading
        # them through the legacy adapter could reclassify evidence. Only source
        # reading is rehydrated; production indexing and model payloads are intact.
        frozen_messages = {}
        reader = service._reader

        class FrozenReader:
            async def read(self, session, ref, view):
                if view == "semantic" and ref.revision_id in frozen_messages:
                    return ContextRevisionMessageView(ref=ref, view=view, messages=frozen_messages[ref.revision_id])
                return await reader.read(session, ref, view)

        if case:
            service._reader = FrozenReader()

        async def measure(name, target, *, prepare_only=False):
            invocations.clear()
            log_start = len(caplog.records)
            async with sessions() as session:
                before = await session.get(LoopBudgetUsage, seed["loop_id"])
                billed_before = before.model_calls
            started = perf_counter()
            try:
                if prepare_only:
                    await service.prewarm_revision(seed["loop_id"], 1, target)
                    result = PortfolioIndexBuildResult()
                else:
                    result = await service.build(observation(seed, target))
            except Exception as exc:
                # An uncaught provider/storage error is a measured failure, not
                # a missing sample. Preserve attempts and billing below without
                # writing raw exception payloads (which may contain credentials).
                result = PortfolioIndexBuildResult(
                    blocker_code="benchmark_build_exception", blocker_summary=type(exc).__name__,
                )
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
                "retry_rate": sum(int(a.get("attempt", 1)) > 1 for a in attempts) / len(attempts) if attempts else 0.0,
                "error_categories": dict(Counter(a.get("failure_category") or a.get("outcome") for a in attempts if a.get("outcome") != "success")),
                "invocations": [{k: v for k, v in call.items() if k != "attempts"} for call in invocations],
                "locally_projected_messages": sum(call["projected_messages"] for call in invocations),
                "timings": [{"phase": r.index_phase, "queue_wait_ms": r.queue_wait_ms,
                             "execution_ms": r.execution_ms, "admission_ms": r.admission_ms,
                             "output_token_limit": r.output_token_limit, "output_tokens": r.output_tokens,
                             "input_tokens": r.input_tokens, "model_calls": r.model_calls} for r in metrics],
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
                async with sessions() as session:
                    records = await ProjectionRecordRepository().by_ids(session, index.source.context_id, index.inheritance.record_ids)
                assert records is not None
                phase.update(
                    quality=_quality(index, records), segments=len(index.segments),
                    reused_segments=len(index.inheritance.reused_segment_ids),
                    recomputed_segments=len(index.inheritance.recomputed_segment_ids),
                    interpretation_read_rounds=len(index.interpretation.read_requests),
                )
            report["phases"][name] = phase
            report["budget_limits"] = observation(seed, target).budget["limits"]
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[index-bench] size={segment_size} concurrency={concurrency} repeat={repeat} "
                  f"phase={name} seconds={elapsed / 1000:.2f} calls={phase['model_calls']} "
                  f"quality={phase.get('quality', {}).get('state', result.blocker_code)}", flush=True)
            assert phase["model_calls"] == phase["billed_model_calls"], phase
            if result.blocker_code is not None:
                if case is None:
                    pytest.fail(result.blocker_summary)
            elif result.indexes:
                assert phase["quality"]["all_sources_bound"]
                assert phase["quality"]["valid_supports"] == phase["quality"]["accepted_supports"]
            return result

        histories = [s.messages for s in case.snapshots] if case else [_history(count), _history(count + 2)]
        parent = None
        for i, history in enumerate(histories):
            target = await revision(sessions, seed["context_id"], history, parent=parent)
            if case:
                frozen_messages[target.ref.revision_id] = tuple(history)
            phase_name = "cold" if i == 0 else f"append-{i}"
            if prewarm == "on":
                await measure(f"prewarm-{i}", target, prepare_only=True)
            built = await measure(phase_name, target)
            if built.blocker_code is None:
                cached = await measure(f"exact-{i}", target)
                assert cached.indexes == built.indexes
                assert report["phases"][f"exact-{i}"]["model_calls"] == 0
            else:
                report["phases"][f"exact-{i}"] = {"status": "not_run", "reason": "no complete Index was published"}
            parent = target
        report["evaluation_complete"] = True
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if case is None:
            assert all(phase["quality"]["state"] == "complete" for phase in report["phases"].values()
                       if "quality" in phase), path

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
