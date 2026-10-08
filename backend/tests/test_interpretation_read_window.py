"""本文件对外提供 D7 原文窗口与完整阅读依赖的生产 builder 回归。

输入为协议闭合的冻结消息、真实请求预算及脚本化模型；输出为窗口有界、累积依赖完整和失败请求无消费的断言。
具体工作流为先验证单窗口可容纳而累计输入超窗，再调用真实 builder/read inputs/budget，核对最终 proof 与每次实际 payload。
隔离数据库案例验证窗口合同变化只失效综合缓存，局部缓存身份及旧 proof 的序列化保持。
示例：pytest backend/tests/test_interpretation_read_window.py -q；脚本化模型不替代原生 Provider 验收。
"""

import asyncio
from types import SimpleNamespace

import pytest

from backend.app.desktop.agent_loop.context_expansion.index_model_budget import BudgetedIndexModel, IndexModelBudget, IndexBudgetExceeded
from backend.app.desktop.agent_loop.context_expansion.interpretation_inputs import FrozenInterpretationInputs
from backend.app.desktop.agent_loop.context_expansion.interpretation_record import RevisionInterpretationProposal
from backend.app.desktop.agent_loop.context_expansion.revision_interpretation import INTERPRETATION_PROMPT, RevisionInterpretationBuilder
from backend.app.desktop.agent_loop.context_expansion.semantic_indexer import ProtocolSafeRevisionSegmenter, RevisionSemanticIndexer
from backend.app.desktop.context_evolution import ContextRevisionRef
from backend.app.desktop.agent_loop import derivation_worker
from backend.app.desktop.agent_loop.derivation_worker import RoleBoundStructuredModel
from backend.app.desktop.agent_loop.structured_worker import StructuredWorkerModel
from backend.tests.config_helpers import app_config_for
from focus.runtime.runs.usage import ModelUsage


def _source(contents=("甲" * 1700, "乙" * 1700)):
    ref = ContextRevisionRef(context_id="context", revision_id="revision", generation=1,
        execution_thread_id="thread", checkpoint_ns="", checkpoint_id="checkpoint", payload_mode="checkpoint")
    index = RevisionSemanticIndexer(ProtocolSafeRevisionSegmenter(max_messages=1)).index(
        source=ref, source_content_hash="a" * 64, context_role="primary", active_objective="window",
        raw_messages=tuple({"id": f"m-{i}", "role": "human", "content": value} for i, value in enumerate(contents)))
    records = tuple(SimpleNamespace(accepted_claim_keys=(), drafts=(), rejections=(), fallback=True,
        record_id=str(i) * 64) for i in range(len(index.segments)))
    return index, records


def _resources(max_reads=None):
    return SimpleNamespace(policy=SimpleNamespace(max_exact_reads=max_reads, max_planner_model_calls=8,
        max_request_input_tokens=None, output_token_reserve=512), global_model_calls_remaining=None,
        global_input_tokens_remaining=None, global_output_tokens_remaining=None)


class _ScriptedModel:
    context_window_tokens = 14000
    max_output_tokens = 512
    cache_identity = "scripted-window"
    last_attempt_records = ()

    def __init__(self, replies):
        self._replies = iter(replies)
        self.calls = []

    async def invoke(self, schema, system, payload):
        self.calls.append(payload)
        return schema.model_validate(next(self._replies))


def test_builder_replaces_window_and_keeps_every_read_dependency():
    index, records = _source()
    ids = tuple(s.segment_id for s in index.segments)
    resources = _resources()
    model = _ScriptedModel([{"action": "read", "read_segments": (ids[0],)},
        {"action": "read", "read_segments": (ids[1],)}, {"action": "complete"}])
    budget = IndexModelBudget(resources)
    inputs = FrozenInterpretationInputs(index, records, max_reads=None)
    assert not budget.fits_request(model, RevisionInterpretationProposal, INTERPRETATION_PROMPT,
        inputs.payload(originals=inputs.all_originals()))
    wrapped = BudgetedIndexModel(model, budget)
    builder = RevisionInterpretationBuilder(wrapped, None, resources, "window-contract", "local-contract", lambda rows: None)
    proof = asyncio.run(builder.build(index, records, SimpleNamespace(mode="full", recomputed_segment_ids=())))
    assert [tuple(s["segment_id"] for s in call["segments"]) for call in model.calls] == [(), (ids[0],), (ids[1],)]
    assert tuple(p.segment_id for p in proof.provided) == ids
    assert proof.read_requests == ((ids[0],), (ids[1],))
    assert len(proof.inventory) == 2 and proof.completed


def test_read_window_replays_without_dropping_audit_or_source():
    index, records = _source(("first", "middle", "last"))
    ids = tuple(s.segment_id for s in index.segments)
    inputs = FrozenInterpretationInputs(index, records, max_reads=3)
    inputs.read((ids[0], ids[2]))
    inputs.read((ids[1],))
    inputs.read((ids[2], ids[0]))
    assert {p.segment_id for p in inputs.provided} == set(ids)
    assert {s["segment_id"] for s in inputs.payload()["segments"]} == {ids[0], ids[2]}
    assert inputs.requests == ((ids[0], ids[2]), (ids[1],), (ids[2], ids[0]))
    assert inputs.payload()["read_requests"] == ((ids[2], ids[0]),)


def test_invalid_read_does_not_mutate_dependencies():
    index, records = _source()
    ids = tuple(s.segment_id for s in index.segments)
    inputs = FrozenInterpretationInputs(index, records, max_reads=1)
    inputs.read((ids[0],))
    before = inputs.provided, inputs.requests, inputs.payload()
    for request in [("outside",), (ids[0], ids[0]), (ids[1],)]:
        with pytest.raises(ValueError):
            inputs.read(request)
        assert (inputs.provided, inputs.requests, inputs.payload()) == before


@pytest.mark.parametrize("model_name", [None, "window-test"])
def test_real_wrapper_preflight_uses_provider_limits_without_policy_cap(model_name):
    config = app_config_for("window-test", None)
    config.models[0].context_window = 1_000_000
    config.models[0].curation_max_output_tokens = 65_536
    resources = _resources()
    resources.policy.output_token_reserve = None
    budget = IndexModelBudget(resources)
    model = RoleBoundStructuredModel(config, "semantic_index_projector", model_name)
    worker = StructuredWorkerModel(config, model_name)
    wrapped = BudgetedIndexModel(model, budget)

    for size, fits in [(800_000, True), (950_000, False), (1_219_235, False)]:
        payload = {"segments": "x" * size}
        assert wrapped.fits_request(RevisionInterpretationProposal, INTERPRETATION_PROMPT, payload) is fits
        assert budget.fits_request(worker, RevisionInterpretationProposal, INTERPRETATION_PROMPT, payload) is fits
        if fits:
            assert wrapped.require_fit(RevisionInterpretationProposal, INTERPRETATION_PROMPT, payload) == budget.require_fit(
                worker, RevisionInterpretationProposal, INTERPRETATION_PROMPT, payload)
        else:
            with pytest.raises(IndexBudgetExceeded) as failure:
                wrapped.require_fit(RevisionInterpretationProposal, INTERPRETATION_PROMPT, payload)
            assert failure.value.request_diagnostic["input_limit"] == 934_464
            assert failure.value.request_diagnostic["output_reserve"] == 65_536
    assert model.usage.model_calls == 0
    assert wrapped.last_attempt_records == ()


@pytest.mark.parametrize("input_limit", [None, 13488])
@pytest.mark.parametrize("corrected", [True, False])
def test_actual_bounded_model_feedback_rejects_window_before_mutation(monkeypatch, corrected, input_limit):
    index, records = _source()
    ids = tuple(s.segment_id for s in index.segments)
    replies = iter([{"action": "read", "read_segments": ids},
        {"action": "read", "read_segments": (ids[0],) if corrected else ids},
        {"action": "read", "read_segments": (ids[1],)}, {"action": "complete"}])
    sent = []

    class Worker:
        context_window_tokens = 14000
        max_output_tokens = 512

        def __init__(self, *args):
            self.usage = ModelUsage()
            self.last_model_metadata = {}

        async def invoke(self, schema, system, payload):
            sent.append(payload)
            self.usage = ModelUsage(model_calls=1, input_tokens=100, output_tokens=10)
            return schema.model_validate(next(replies))

    monkeypatch.setattr(derivation_worker, "StructuredWorkerModel", Worker)
    resources = _resources()
    resources.policy.max_request_input_tokens = input_limit
    config = app_config_for("window-test", None)
    raw = RoleBoundStructuredModel(config, "semantic_index_projector", max_attempts=2)
    budget = IndexModelBudget(resources)
    wrapped = BudgetedIndexModel(raw, budget)
    builder = RevisionInterpretationBuilder(wrapped, None, resources, "window-contract", "local-contract", lambda rows: None)
    operation = builder.build(index, records, SimpleNamespace(mode="full", recomputed_segment_ids=()))
    if corrected:
        proof = asyncio.run(operation)
        assert tuple(p.segment_id for p in proof.provided) == ids
        assert proof.read_requests == ((ids[0],), (ids[1],))
        assert len(sent) == 4 and raw.usage.model_calls == 4
    else:
        with pytest.raises(IndexBudgetExceeded) as failure:
            asyncio.run(operation)
        diagnostic = failure.value.request_diagnostic
        assert diagnostic["schema"] == "RevisionInterpretationProposal"
        assert diagnostic["input_bound"] > diagnostic["input_limit"]
        assert len(sent) == 2 and raw.usage.model_calls == 2
    assert sent[0]["segments"] == sent[1]["segments"] == ()
    feedback = sent[1]["previous_attempt_failure"]
    assert "input_bound=" in feedback["message"]
    assert all(budget.fits_request(Worker(), RevisionInterpretationProposal, INTERPRETATION_PROMPT, p) for p in sent)


def test_window_payload_size_does_not_grow_with_read_log():
    index, records = _source(("one", "two"))
    inputs = FrozenInterpretationInputs(index, records, max_reads=2)
    ids = tuple(s.segment_id for s in index.segments)
    for _ in range(100):
        inputs.read((ids[0],))
        inputs.read((ids[1],))
    assert len(inputs.requests) == 200
    assert inputs.payload()["read_requests"] == ((ids[1],),)
    assert len(inputs.payload()["inventory"]) == len(inputs.provided) == 2


def test_uncontainable_complete_inventory_blocks_before_any_model_call():
    index, records = _source(tuple("evidence" for _ in range(30)))
    resources = _resources()
    raw = _ScriptedModel([])
    raw.context_window_tokens = 6000
    wrapped = BudgetedIndexModel(raw, IndexModelBudget(resources))
    builder = RevisionInterpretationBuilder(wrapped, None, resources, "window-contract", "local-contract", lambda rows: None)
    with pytest.raises(IndexBudgetExceeded) as failure:
        asyncio.run(builder.build(index, records, SimpleNamespace(mode="full", recomputed_segment_ids=())))
    diagnostic = failure.value.request_diagnostic
    assert diagnostic["schema"] == "RevisionInterpretationProposal"
    assert diagnostic["payload_field_bytes"]["inventory"] > diagnostic["input_limit"]
    assert raw.calls == [] and wrapped.last_attempt_records == ()


def test_tool_exchange_is_a_complete_window_atom():
    index, _ = _source(("unused",))
    index = RevisionSemanticIndexer(ProtocolSafeRevisionSegmenter(max_messages=1)).index(
        source=index.source, source_content_hash="b" * 64, context_role="primary", active_objective="tool",
        raw_messages=({"id": "call", "role": "ai", "content": "",
            "tool_calls": [{"id": "tool-call", "name": "read_file", "args": {"path": "fixture"}}]},
            {"id": "result", "role": "tool", "tool_call_id": "tool-call", "name": "read_file", "content": "exact result"},
            {"id": "other", "role": "human", "content": "other evidence"}))
    records = tuple(SimpleNamespace(accepted_claim_keys=(), drafts=(), rejections=(), fallback=True,
        record_id=str(i) * 64) for i in range(len(index.segments)))
    inputs = FrozenInterpretationInputs(index, records, max_reads=None)
    inputs.read((index.segments[0].segment_id,))
    messages = inputs.payload()["segments"][0]["messages"]
    assert tuple(m["message_id"] for m in messages) == ("call", "result")
    assert messages[0]["tool_calls"][0]["id"] == messages[1]["tool_call_id"]


@pytest.mark.usefixtures("isolated_postgres_database")
def test_window_contract_keeps_local_cache_and_old_proof(monkeypatch, tmp_path):
    import backend.app.desktop.agent_loop.context_expansion.portfolio_index as module
    from backend.tests.incremental_index_support import ModelProbe, observation, revision
    from backend.tests.test_incremental_revision_index import exercise

    async def run(sessions, seed):
        probe = ModelProbe()
        target = await revision(sessions, seed["context_id"], [{"id": "evidence", "role": "human", "content": "frozen evidence"}])
        frozen = observation(seed, target)
        with monkeypatch.context() as legacy:
            legacy.setattr(module, "INTERPRETATION_PROMPT", "legacy interpretation contract")
            old = (await probe.service(sessions).build(frozen)).indexes[0]
        original = old.model_dump(mode="json")
        probe.calls.clear()
        result = await probe.service(sessions).build(frozen)
        assert result.blocker_code is None, result.blocker_summary
        current = result.indexes[0]
        assert current.projector_version != old.projector_version
        assert current.interpretation.contract_fingerprint != old.interpretation.contract_fingerprint
        assert current.interpretation.local_contract_fingerprint == old.interpretation.local_contract_fingerprint
        assert current.inheritance.record_ids == old.inheritance.record_ids
        assert not probe.local_calls and len(probe.interpretation_calls) == 1
        assert old.model_dump(mode="json") == original
    exercise(tmp_path, run)
