"""本文件对外提供冻结来源候选准入与安全反馈的纯回归测试。

输入为合成的 78 来源冻结合同和合法/非法模型候选；输出为引用、schema、诊断及反馈边界断言。
具体工作流为构造真实来源形状，注入重复、越界、历史引用、完成证据或遗漏并比较合法对照；核对多错误安全投影，不读取事故正文或调用模型。
示例：python -m pytest backend/tests/test_task_progress_candidate_contract.py -q。
工作区 received/deferred/discussion 与已提交 decision 的状态效力分别验证。
"""

import json
import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.app.desktop.agent_loop.task_progress.candidate_contract import (
    CandidateValidationError,
    candidate_schema,
    validate_candidate,
)
from backend.app.desktop.agent_loop.task_progress.consolidation import TaskProgressConsolidator
from backend.app.desktop.agent_loop.task_progress.contracts import (
    ProgressCandidate, RoundDecisionInputs, SourceAssessment, TaskDeltaManifest,
    TaskItem, TaskProgressDocument, TaskSource, canonical_hash,
)


def frozen_inputs():
    kinds = ["test"] * 40 + ["artifact"] * 36 + ["run_outcome", "workspace"]
    sources = []
    for index, kind in enumerate(kinds):
        payload = {"status": "verified", "fixture": index}
        version = canonical_hash(payload)
        source_id = f"fixture-{index}"
        sources.append(TaskSource(source_key=canonical_hash([kind, source_id, version]),
                                  kind=kind, source_id=source_id, version=version, payload=payload))
    previous = TaskProgressDocument(mission_revision=1, items=(TaskItem(item_id="check:test", description="测试"),))
    delta = TaskDeltaManifest(boundary="fixture-observation", sources=tuple(sources))
    topology = canonical_hash([])
    return RoundDecisionInputs(round_id="fixture-round", observation_id=delta.boundary,
                               observation_hash="a" * 64, previous_progress_id="fixture-previous",
                               previous_progress_hash=canonical_hash(previous), previous_progress=previous,
                               task_delta=delta, manifest_hash=canonical_hash(delta),
                               lineage={"roots": {}, "nodes": [], "edges": [], "topology_hash": topology},
                               topology_hash=topology)


def covered_candidate(inputs):
    return ProgressCandidate(source_assessments=tuple(
        SourceAssessment(source_key=source.source_key, disposition="unknown", explanation="待验证")
        for source in inputs.task_delta.sources
    ))


@pytest.mark.parametrize("disposition", [None, "deferred", "discussion", "decision"])
def test_workspace_input_effect_is_required_before_changing_work(disposition):
    inputs = frozen_inputs()
    payload = {"intent_kind": "workspace_input", "instruction": "做本地保存"}
    if disposition:
        payload["disposition"] = disposition
    version = canonical_hash(payload)
    source = TaskSource(source_key=canonical_hash(["user_revision", "input", version]), kind="user_revision",
        source_id="input", version=version, payload=payload)
    manifest = inputs.task_delta.model_copy(update={"sources": (source,)})
    inputs = inputs.model_copy(update={"task_delta": manifest, "manifest_hash": canonical_hash(manifest)})
    candidate = ProgressCandidate(changes=(TaskItem(item_id="new-work", description="本地保存", state="not_started", evidence_keys=(source.source_key,)),))
    if disposition == "decision":
        assert validate_candidate(inputs, candidate) == candidate
    else:
        with pytest.raises(CandidateValidationError, match="workspace_input_not_effective"):
            validate_candidate(inputs, candidate)
        pending = candidate.model_copy(update={"changes": (candidate.changes[0].model_copy(update={"state": "unknown"}),)})
        assert validate_candidate(inputs, pending) == pending


def test_complete_real_shaped_source_fixture_is_accepted_without_mutation():
    inputs = frozen_inputs()
    candidate = covered_candidate(inputs)
    before = canonical_hash(inputs)
    assert validate_candidate(inputs, candidate) == candidate
    document, contribution = TaskProgressConsolidator().apply(inputs, candidate, {})
    assert len(contribution.direct_source_keys) == 78
    assert len(document.items) == 79
    assert canonical_hash(inputs) == before


@pytest.mark.parametrize("failure,expected", [
    ("duplicate", "duplicate_source_assessment"),
    ("outside", "unknown_assessment_source"),
    ("evidence", "unknown_change_evidence"),
    ("missing", "uncovered_source"),
])
def test_reference_errors_have_distinct_codes_and_field_paths(failure, expected):
    inputs = frozen_inputs()
    candidate = covered_candidate(inputs)
    if failure == "duplicate":
        candidate = candidate.model_copy(update={"source_assessments": candidate.source_assessments + (candidate.source_assessments[0],)})
    elif failure == "outside":
        candidate = candidate.model_copy(update={"source_assessments": candidate.source_assessments + (
            SourceAssessment(source_key="outside", disposition="unknown", explanation="未知"),)})
    elif failure == "evidence":
        candidate = candidate.model_copy(update={"changes": (TaskItem(item_id="new", description="新增", evidence_keys=("outside",)),)})
    else:
        candidate = candidate.model_copy(update={"source_assessments": candidate.source_assessments[:-1]})
    with pytest.raises(CandidateValidationError) as error:
        TaskProgressConsolidator().apply(inputs, candidate, {})
    assert expected in {issue.code for issue in error.value.issues}
    assert all(issue.path for issue in error.value.issues)


def test_output_schema_binds_both_reference_fields_without_changing_global_model():
    inputs = frozen_inputs()
    before = ProgressCandidate.model_json_schema()
    schema = candidate_schema(inputs)
    document = json.dumps(schema.model_json_schema())
    assert all(source.source_key in document for source in inputs.task_delta.sources)
    assert before == ProgressCandidate.model_json_schema()
    schema.model_validate(covered_candidate(inputs).model_dump())
    schema(source_assessments=covered_candidate(inputs).source_assessments)
    for candidate in (
        {"source_assessments": [{"source_key": "outside", "disposition": "unknown", "explanation": "未知"}]},
        {"changes": [{"item_id": "new", "description": "新增", "evidence_keys": ["outside"]}]},
    ):
        with pytest.raises(ValidationError):
            schema.model_validate(candidate)


def test_model_construct_cannot_bypass_canonical_shape_validation():
    inputs = frozen_inputs()
    candidate = ProgressCandidate.model_construct(changes="invalid", source_assessments=())
    with pytest.raises(CandidateValidationError) as error:
        validate_candidate(inputs, candidate)
    assert all(issue.code.startswith("schema_") for issue in error.value.issues)


def test_safe_diagnostics_and_feedback_preserve_identity_and_exclude_arbitrary_text():
    from backend.app.desktop.agent_loop.task_progress.candidate_interpretation import (
        interpretation_payload, validation_diagnostic,
    )
    inputs = frozen_inputs()
    secret = "sk-unconfigured-private-value"
    candidate = covered_candidate(inputs).model_copy(update={"changes": (
        TaskItem(item_id=secret, description=secret, evidence_keys=(secret,)),)})
    with pytest.raises(CandidateValidationError) as error:
        validate_candidate(inputs, candidate)
    diagnostic = validation_diagnostic(inputs, 1, error.value.candidate, error.value.issues)
    assert secret not in json.dumps(diagnostic)
    assert not diagnostic["valid"] and diagnostic["error_count"] == 1
    payload = interpretation_payload(inputs, {}, [diagnostic])
    assert secret not in json.dumps(payload)
    assert payload["validation_feedback"]["errors"][0]["code"] == "unknown_change_evidence"
    assert payload["task_delta"] == inputs.task_delta.model_dump(mode="json")
    with pytest.raises(ValueError, match="feedback_identity_mismatch"):
        interpretation_payload(inputs, {}, [{**diagnostic, "inputs_hash": "wrong"}])


def test_diagnostic_limit_is_explicit_without_reducing_validation():
    from backend.app.desktop.agent_loop.task_progress.candidate_interpretation import DIAGNOSTIC_BYTES, validation_diagnostic
    inputs = frozen_inputs()
    candidate = covered_candidate(inputs).model_copy(update={"source_assessments": covered_candidate(inputs).source_assessments * 80})
    with pytest.raises(CandidateValidationError) as error:
        validate_candidate(inputs, candidate)
    diagnostic = validation_diagnostic(inputs, 1, candidate, error.value.issues)
    assert len(json.dumps(diagnostic, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= DIAGNOSTIC_BYTES
    assert diagnostic["truncated"]
    assert diagnostic["error_count"] == 78 * 79
    assert diagnostic["retained_error_count"] < diagnostic["error_count"]
    assert diagnostic["retained_projection_count"] < diagnostic["projection_count"]


def test_provider_schema_errors_have_safe_paths_and_no_raw_input():
    from backend.app.desktop.agent_loop.task_progress.candidate_contract import structural_error
    inputs = frozen_inputs()
    secret = "Bearer unknown-credential"
    with pytest.raises(ValidationError) as error:
        candidate_schema(inputs).model_validate({"source_assessments": [{"source_key": secret, "disposition": "unknown", "explanation": secret}], secret: secret})
    safe = structural_error(error.value)
    assert "unknown_assessment_source" in {issue.code for issue in safe.issues}
    assert secret not in str(safe)
    assert secret not in json.dumps([issue.model_dump(mode="json") for issue in safe.issues])


def test_legacy_feedback_is_explicit_and_generic_failures_do_not_become_corrections():
    from backend.app.desktop.agent_loop.task_progress.candidate_interpretation import interpretation_payload
    inputs = frozen_inputs()
    assert "validation_feedback" not in interpretation_payload(inputs, {}, error="ValueError: provider failure")
    feedback = interpretation_payload(inputs, {}, [{"event": "explicit_retry", "legacy_candidate_failure": True}])["validation_feedback"]
    assert feedback["evidence_missing"]
    assert "errors" not in feedback


@pytest.mark.parametrize("method", ["prompt_json", "json_schema"])
def test_existing_structured_worker_routes_receive_bound_schema(monkeypatch, method):
    import backend.app.desktop.agent_loop.structured_worker as worker
    from backend.app.desktop.agent_loop.task_progress.candidate_interpretation import interpretation_payload
    inputs = frozen_inputs()
    schema = candidate_schema(inputs)
    captured = {}

    class Model:
        def with_structured_output(self, received, **kwargs):
            captured["schema"] = received
            return self

        async def ainvoke(self, messages, config):
            captured["messages"] = messages
            candidate = covered_candidate(inputs).model_dump(mode="json")
            if method == "prompt_json":
                return SimpleNamespace(content=json.dumps(candidate), response_metadata={})
            return {"raw": SimpleNamespace(response_metadata={}), "parsed": schema.model_validate(candidate)}

    config = SimpleNamespace(model="fixture", name="fixture", curation_output_method=method,
                             curation_max_output_tokens=1000, context_window=100000)
    app_config = SimpleNamespace(get_model=lambda name: config, resolve_default_model_name=lambda: "fixture")
    monkeypatch.setattr(worker, "create_chat_model", lambda **kwargs: Model())
    result = asyncio.run(worker.StructuredWorkerModel(app_config).invoke(schema, "fixture system", interpretation_payload(inputs, {})))
    assert validate_candidate(inputs, result) == covered_candidate(inputs)
    body = str(captured["messages"][1].content)
    assert all(source.source_key in body for source in inputs.task_delta.sources)
    if method == "json_schema":
        assert captured["schema"] is schema


def test_feedback_and_bound_schema_are_included_in_capacity_admission(monkeypatch):
    from focus.messages.usage import estimate_raw_tokens
    from backend.app.desktop.agent_loop.structured_worker import StructuredWorkerModel
    from backend.app.desktop.agent_loop.task_progress.candidate_interpretation import interpretation_payload, validation_diagnostic
    from backend.app.desktop.agent_loop.task_progress.runtime import TaskProgressRuntime, _SYSTEM
    inputs = frozen_inputs()
    plain = interpretation_payload(inputs, {})
    schema = candidate_schema(inputs)
    messages = StructuredWorkerModel.request_messages(schema, _SYSTEM, plain)
    estimated = estimate_raw_tokens("".join(str(message.content) for message in messages), len(messages))
    candidate = covered_candidate(inputs)
    bad = candidate.model_copy(update={"source_assessments": candidate.source_assessments + (candidate.source_assessments[0],)})
    with pytest.raises(CandidateValidationError) as error:
        validate_candidate(inputs, bad)
    diagnostic = validation_diagnostic(inputs, 1, bad, error.value.issues)
    corrected = interpretation_payload(inputs, {}, [diagnostic])
    model = SimpleNamespace(context_window_tokens=estimated + 1000, max_output_tokens=1000)
    runtime = TaskProgressRuntime(None, None, model_factory=lambda name: model)

    async def no_reservation(*args):
        pytest.fail("超窗请求不得预留或调用模型")

    monkeypatch.setattr(runtime, "_reserve", no_reservation)
    with pytest.raises(ValueError, match="context_budget"):
        asyncio.run(runtime._interpret(inputs, 2, {}, None, corrected))


@pytest.mark.parametrize("field", ["assessment", "evidence"])
def test_historical_source_does_not_expand_current_manifest(field):
    inputs = frozen_inputs()
    old_key = canonical_hash("historical-source")
    previous = inputs.previous_progress.model_copy(update={"items": (
        inputs.previous_progress.items[0].model_copy(update={"evidence_keys": (old_key,)}),)})
    inputs = inputs.model_copy(update={"previous_progress": previous, "previous_progress_hash": canonical_hash(previous)})
    candidate = covered_candidate(inputs)
    if field == "assessment":
        candidate = candidate.model_copy(update={"source_assessments": candidate.source_assessments + (
            SourceAssessment(source_key=old_key, disposition="already_known", explanation="历史存在"),)})
        code = "unknown_assessment_source"
    else:
        candidate = candidate.model_copy(update={"changes": (TaskItem(item_id="new", description="新增", evidence_keys=(old_key,)),)})
        code = "unknown_change_evidence"
    with pytest.raises(CandidateValidationError) as error:
        validate_candidate(inputs, candidate)
    assert code in {issue.code for issue in error.value.issues}
    assert old_key not in {source.source_key for source in inputs.task_delta.sources}


def test_multiple_reference_errors_preserve_paths_totals_and_original_sequence():
    from backend.app.desktop.agent_loop.task_progress.candidate_interpretation import validation_diagnostic
    inputs = frozen_inputs()
    base = covered_candidate(inputs)
    candidate = base.model_copy(update={"source_assessments": base.source_assessments + (base.source_assessments[0],),
                                       "changes": (TaskItem(item_id="new", description="新增", evidence_keys=("outside",)),)})
    before = canonical_hash(candidate)
    with pytest.raises(CandidateValidationError) as error:
        validate_candidate(inputs, candidate)
    diagnostic = validation_diagnostic(inputs, 1, candidate, error.value.issues)
    assert {issue.code: issue.path for issue in error.value.issues} == {
        "duplicate_source_assessment": ("source_assessments", 78, "source_key"),
        "unknown_change_evidence": ("changes", 0, "evidence_keys", 0)}
    assert diagnostic["error_count"] == diagnostic["retained_error_count"] == 2
    assert diagnostic["projection_count"] == diagnostic["retained_projection_count"] == 80
    assert diagnostic["error_counts"] == {"duplicate_source_assessment": 1, "unknown_change_evidence": 1}
    assert not diagnostic["truncated"] and canonical_hash(candidate) == before


@pytest.mark.parametrize("status", ["failed", "unknown"])
def test_completed_rejects_failed_or_unknown_test_evidence(status):
    inputs = frozen_inputs()
    old = inputs.task_delta.sources[0]
    payload = {"status": status}
    version = canonical_hash(payload)
    source = TaskSource(**{**old.model_dump(), "payload": payload, "version": version,
                           "source_key": canonical_hash([old.kind, old.source_id, version])})
    delta = inputs.task_delta.model_copy(update={"sources": (source, *inputs.task_delta.sources[1:])})
    inputs = inputs.model_copy(update={"task_delta": delta, "manifest_hash": canonical_hash(delta)})
    candidate = covered_candidate(inputs).model_copy(update={"changes": (
        TaskItem(item_id="new", description="完成", state="completed", support="supported", evidence_keys=(source.source_key,)),)})
    with pytest.raises(CandidateValidationError) as error:
        validate_candidate(inputs, candidate)
    assert "unverified_completion" in {issue.code for issue in error.value.issues}


@pytest.mark.parametrize("relationship,code", [("unknown", "unknown_correction"), ("missing", "missing_correction"),
                                               ("supersedes", "invalid_supersession")])
def test_completed_does_not_bypass_correction_relationships(relationship, code):
    inputs = frozen_inputs()
    update = {"corrects": ("missing-item",)} if relationship == "unknown" else {}
    if relationship == "supersedes":
        update = {"supersedes": ("missing-item",)}
    change = TaskItem(item_id="check:test", description="修改旧义务", state="completed", support="supported",
                      evidence_keys=(inputs.task_delta.sources[0].source_key,), **update)
    candidate = covered_candidate(inputs).model_copy(update={"changes": (change,)})
    with pytest.raises(CandidateValidationError) as error:
        validate_candidate(inputs, candidate)
    assert code in {issue.code for issue in error.value.issues}
