"""Freeze real-corpus identities and exercise the isolated benchmark without a provider."""

import json

import pytest

from backend.scripts.semantic_index_corpus import BenchmarkCorpus, CorpusCase, Snapshot, load_corpus, require_jsonb_text, select_cases, summarize_reports
from backend.app.desktop.agent_loop.context_expansion.contracts import stable_expansion_hash
from backend.app.desktop.context_evolution import ContextRevisionRef


def snapshot(messages, generation=1, context="context"):
    require_jsonb_text(messages)
    return Snapshot(
        source=ContextRevisionRef(context_id=context, revision_id=f"revision-{generation}",
                                  generation=generation, execution_thread_id="execution",
                                  checkpoint_id=f"cp-{generation}", payload_mode="checkpoint"),
        source_content_hash="a" * 64, messages=tuple(messages),
        semantic_hash=stable_expansion_hash("benchmark-semantic-snapshot-v1", tuple(messages)),
    )


def test_export_roundtrip_and_hash_rejects_changed_content(tmp_path):
    sample = snapshot([{"id": "m", "role": "human", "content": "frozen"}])
    corpus = BenchmarkCorpus(cases=(CorpusCase(name="sample", category="selected", snapshots=(sample,)),))
    path = tmp_path / "corpus.json"
    path.write_text(corpus.model_dump_json(), encoding="utf-8")
    assert load_corpus(path) == corpus
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["cases"][0]["snapshots"][0]["messages"][0]["content"] = "changed"
    with pytest.raises(ValueError, match="hash mismatch"):
        BenchmarkCorpus.model_validate(payload)


def test_non_append_and_cross_context_revisions_are_rejected():
    first = snapshot([{"id": "m", "role": "human", "content": "first"}])
    for newer in (
        snapshot([{"id": "m", "role": "human", "content": "changed"}], 2),
        snapshot([*first.messages, {"id": "new", "role": "human", "content": "new"}], 2, "foreign"),
        snapshot(first.messages, 2),
    ):
        with pytest.raises(ValueError, match="append-only"):
            CorpusCase(name="sample", category="selected", snapshots=(first, newer))
    assert len(select_cases([first], automatic=False)) == 1
    with pytest.raises(ValueError, match="long tool"):
        select_cases([first], automatic=True)


@pytest.mark.parametrize("text", ["binary\x00output", "bad\ud800surrogate"])
def test_corpus_rejects_text_that_index_jsonb_cannot_store(text):
    with pytest.raises(ValueError, match="postgres_jsonb_incompatible_text"):
        snapshot([{"id": "binary", "role": "human", "content": text}])


def test_automatic_categories_distinguish_size_from_message_count():
    huge = snapshot([{"id": "huge", "role": "human", "content": "x" * 20000}])
    short = snapshot([{"id": "s1", "role": "human", "content": "question"},
                      {"id": "s2", "role": "ai", "content": "answer"}], context="short")
    tool = snapshot([{"id": "call", "role": "ai", "tool_calls": [{"id": "c", "name": "read", "args": {}}]},
                     {"id": "result", "role": "tool", "tool_call_id": "c", "content": "x" * 5000}], context="tool")
    appends = [snapshot([{"id": f"a-{i}", "role": "human", "content": "append"}
                         for i in range(count)], count, "appends") for count in (1, 2, 3)]
    cases = select_cases([huge, short, tool, *appends], automatic=True)
    assert cases[0].snapshots[0].source.context_id != "tool"
    assert cases[0].snapshots[0].source.context_id != "context"
    assert cases[1].snapshots[0] == tool
    assert cases[2].snapshots == tuple(appends)
    with pytest.raises(ValueError, match="short dialogue"):
        select_cases([huge], automatic=True)


@pytest.mark.parametrize("different_control", [{"concurrency": 8},
    {"role_output_token_limits": {"semantic_index_interpreter": 2048}}])
def test_summary_keeps_blockers_and_rejects_different_control_settings(tmp_path, different_control):
    report = {
        "case": "sample", "corpus_case_hash": "frozen", "repeat": 0,
        "configured_model": "model", "provider_model": "provider", "concurrency": 4,
        "budget_limits": {"max_model_calls": 100}, "segment_size": 12, "segment_token_limit": None,
        "frozen_sources": [], "evaluation_complete": True,
        "phases": {"cold": {"blocker_code": "window_exceeded", "model_calls": 0},
                   "exact-0": {"status": "not_run", "reason": "no index"}},
    }
    (tmp_path / "a-segment-12.json").write_text(json.dumps(report), encoding="utf-8")
    second = {**report, "segment_size": 20}
    other = tmp_path / "a-segment-20.json"
    other.write_text(json.dumps(second), encoding="utf-8")
    summary = summarize_reports(tmp_path)
    assert len(summary["rows"]) == 4
    assert summary["rows"][0]["blocker_code"] == "window_exceeded"
    assert summary["rows"][1]["status"] == "not_run"
    other.write_text(json.dumps({**second, **different_control}), encoding="utf-8")
    with pytest.raises(ValueError, match="incomparable"):
        summarize_reports(tmp_path)


@pytest.mark.usefixtures("isolated_postgres_database")
@pytest.mark.parametrize("prewarm", ["off", "on"])
def test_corpus_replay_reports_cold_appends_exact_and_citation_quality(tmp_path, caplog, monkeypatch, prewarm):
    from backend.tests import test_index_real_model_performance as benchmark
    from backend.tests.incremental_index_support import ModelProbe

    probe = ModelProbe()

    class FakeModel:
        last_attempt_records = ()
        max_output_tokens = 8192

        def __init__(self, config, role):
            self.delegate = probe.factory("projector" if role == "semantic_index_projector" else "verifier")()
            self.cache_identity = self.delegate.cache_identity

        async def invoke(self, schema, system, payload):
            return await self._invoke(schema, system, payload, None)

        async def invoke_validated(self, schema, system, payload, validator):
            return await self._invoke(schema, system, payload, validator)

        async def _invoke(self, schema, system, payload, validator):
            result = await self.delegate.invoke(schema, system, payload)
            self.last_attempt_records = self.delegate.last_attempt_records
            if validator:
                validator(result)
            return result

    monkeypatch.setattr(benchmark, "RoleBoundStructuredModel", FakeModel)
    monkeypatch.setenv("FOCUS_INDEX_BENCHMARK_OUTPUT", str(tmp_path / "reports"))
    history = [{"id": f"m-{i}", "role": "human", "content": f"Evidence number {i}",
                "semantic_policy": "index", "source_ordinal": i} for i in range(5)]
    case = CorpusCase(name="sample", category="multiple_append", snapshots=(
        snapshot(history[:2]), snapshot(history[:3], 2), snapshot(history, 3),
    ))
    benchmark.test_real_provider_latency_matrix(tmp_path, caplog, 12, 200, 0, case, prewarm)
    report = json.loads(next((tmp_path / "reports").glob("*.json")).read_text(encoding="utf-8"))
    assert report["evaluation_complete"] and len(report["phases"]) == (9 if prewarm == "on" else 6)
    assert [s["semantic_hash"] for s in report["frozen_sources"]] == [s.semantic_hash for s in case.snapshots]
    assert report["segment_token_limit"] == 200 and report["concurrency"] == 4
    for name, phase in report["phases"].items():
        if name.startswith("prewarm"):
            assert phase["model_calls"] == phase["billed_model_calls"]
            assert all(t["phase"] in {"local_projection", "local_verifier"} for t in phase["timings"])
            continue
        assert phase["quality"]["citation_validity"] == 1.0
        assert phase["quality"]["accepted_citation_message_coverage"] == 1.0
        assert phase["quality"]["fallback_ratio"] == 0
        if name.startswith("exact"):
            assert phase["model_calls"] == phase["billed_model_calls"] == 0


@pytest.mark.usefixtures("isolated_postgres_database")
def test_benchmark_records_unexpected_failure_and_does_not_claim_cache_hit(tmp_path, caplog, monkeypatch):
    from backend.tests import test_index_real_model_performance as benchmark

    class IdentityOnlyModel:
        def __init__(self, config, role):
            self.cache_identity = f"test-{role}"

    async def fail_build(self, observation):
        raise RuntimeError("unexpected provider failure")

    monkeypatch.setattr(benchmark, "RoleBoundStructuredModel", IdentityOnlyModel)
    monkeypatch.setattr(benchmark.PortfolioSemanticIndexService, "build", fail_build)
    monkeypatch.setenv("FOCUS_INDEX_BENCHMARK_OUTPUT", str(tmp_path / "reports"))
    case = CorpusCase(name="failure", category="selected", snapshots=(
        snapshot([{"id": "m", "role": "human", "content": "frozen"}]),
    ))
    benchmark.test_real_provider_latency_matrix(tmp_path, caplog, 12, None, 0, case, "off")
    report = json.loads(next((tmp_path / "reports").glob("*.json")).read_text(encoding="utf-8"))
    assert report["phases"]["cold"]["blocker_code"] == "benchmark_build_exception"
    assert report["phases"]["cold"]["blocker_summary"] == "RuntimeError"
    assert report["phases"]["cold"]["model_calls"] == 0
    assert report["phases"]["exact-0"]["status"] == "not_run"
