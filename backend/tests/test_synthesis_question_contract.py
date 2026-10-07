"""本文件对外提供冻结问题作者合同、纠错反馈和有界尝试的生产链路回归。

输入为真实脱敏 WorkSpec/Mission fixture、受控作者响应和平台测试能力；输出为合法身份、逐项处置、失败隐私和消费断言。
具体工作流为捕获真实 RoleBound/StructuredWorker 请求，复现缺计划和缺覆盖，再校验纠正、耗尽与独立核验；
受控响应不是未保存的原始模型输出，也不替代真实 Provider 或插件交付验收。
示例：python -m pytest backend/tests/test_synthesis_question_contract.py -q。
"""

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.context_expansion.contracts import ExpansionOpportunity, ResolvedEvidenceBundle, WorkContextSpec, stable_expansion_hash
from backend.app.desktop.agent_loop.context_expansion.start_readiness import ExecutionReadiness, load_execution_readiness, question_catalog, work_question_id
from backend.app.desktop.agent_loop.context_expansion.artifact_repository import SemanticDerivationArtifactRepository
from backend.app.desktop.agent_loop.context_expansion.compiler import ContextExpansionPlanCompiler
from backend.app.desktop.agent_loop.models import LoopBudgetUsage
from backend.app.desktop.agent_loop.context_expansion.synthesis import ContextSynthesisWorkerDraft
from backend.app.desktop.agent_loop.context_expansion.synthesis_service import StructuredContextSynthesisService
from backend.app.desktop.agent_loop.derivation_worker import RoleBoundStructuredModel, safe_validation_message
from backend.app.desktop.agent_loop.derivation_worker import StructuredResultValidationError
from backend.tests.config_helpers import app_config_for
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.test_synthesis_claim_feedback import _compiler_observation


def _fixture():
    data = json.loads((Path(__file__).parent / "fixtures/synthesis_questions/bootstrap_contract_failure.json").read_text(encoding="utf-8"))
    return WorkContextSpec.model_validate(data["work_spec"]), ResolvedEvidenceBundle.model_validate(data["bundle"])


def _scope(capabilities=("read", "write", "host_command")):
    return ExecutionReadiness(workspace_id="controlled-workspace", goal_revision=1, authority_revision=1,
        capabilities=capabilities, workspace_mode="isolated_write")


def _response(spec, bundle, error=None):
    plans = [{"question_id": work_question_id(q), "disposition": "execution_research",
        "investigation": "在测试仓库核对构建入口与模块接口", "required_capabilities": ["read", "host_command"]}
        for q in spec.questions]
    payload = {"sections": [{"title": "任务要求", "claims": [{"claim_key": "delivery",
        "statement": "交付可构建的 Obsidian 插件仓库。", "authority": "confirmed",
        "citations": [next(iter(ContextSynthesisWorkerDraft.citation_catalog(bundle)))],
        "requirement_ids": [r.requirement_id for r in spec.evidence_requirements], "question_ids": []}]}],
        "unresolved_questions": list(spec.questions), "question_dispositions": plans}
    if error == "missing_plan":
        payload["question_dispositions"] = plans[:-1]
    elif error == "missing_question":
        payload["unresolved_questions"] = list(spec.questions[:-1])
        payload["question_dispositions"] = plans[:-1]
    elif error == "both":
        payload["unresolved_questions"] = list(spec.questions[:-1])
        payload["question_dispositions"] = plans[:1]
    elif error == "unknown_question":
        payload["sections"][0]["claims"][0]["question_ids"] = ["sk-private-marker-do-not-log"]
    return payload


def _service(monkeypatch, errors, *, verdict="supported"):
    spec, bundle = _fixture()
    config = app_config_for("question-contract-test", None)
    config.models[0].curation_output_method = "prompt_json"
    calls = []

    class Provider:
        async def ainvoke(self, messages, config):
            body = messages[-1].content
            payload = json.loads(body.split("<worker_input>", 1)[1].split("</worker_input>", 1)[0])
            author = "work_spec" in payload
            role = "author" if author else "verifier"
            config["callbacks"][0].usage_metadata["question-contract-test"] = {
                "input_tokens": 40, "output_tokens": 10, "total_tokens": 50}
            author_count = sum(c[0] == "author" for c in calls)
            calls.append((role, payload, json.loads(body.split("JSON Schema: ", 1)[1])))
            if author:
                response = _response(spec, bundle, errors[author_count])
            else:
                response = {"assessments": [{"claim_id": c["claim_id"], "verdict": verdict,
                    "reason": "Controlled independent result"} for c in payload["claims"]]}
            return AIMessage(content=json.dumps(response, ensure_ascii=False))

    monkeypatch.setattr("backend.app.desktop.agent_loop.structured_worker.create_chat_model", lambda **kwargs: Provider())
    author = RoleBoundStructuredModel(config, "dossier_synthesizer")
    verifier = RoleBoundStructuredModel(config, "claim_verifier")
    return StructuredContextSynthesisService(author, verifier), spec, bundle, author, verifier, calls


def test_real_failure_pattern_retains_both_attempts_and_exact_gaps(monkeypatch):
    async def run():
        service, spec, bundle, author, verifier, calls = _service(monkeypatch, ["missing_plan", "missing_question"])
        result = await service.synthesize(None, spec, bundle, execution_readiness=_scope())
        assert result.blocker_code == "synthesis_invalid" and result.dossier is result.review is None
        assert [c[0] for c in calls] == ["author", "author"]
        first, second = result.attempt_records
        assert first["validation_feedback"]["details"]["missing_plan_ids"] == [work_question_id(spec.questions[-1])]
        assert second["validation_feedback"]["details"]["missing_question_ids"] == [work_question_id(spec.questions[-1])]
        assert calls[1][1]["previous_attempt_failure"] == first["validation_feedback"]
        assert author.last_usage.model_calls == 2 and verifier.last_usage.model_calls == 0
        assert sum(r["input_tokens"] for r in result.attempt_records) == 80
    asyncio.run(run())


def test_one_feedback_corrects_multiple_gaps_before_independent_verification(monkeypatch):
    async def run():
        service, spec, bundle, author, verifier, calls = _service(monkeypatch, ["both", None])
        guarded = []
        async def guard(worker, schema, system, payload):
            guarded.append(payload)
        author.bind_request_guard(guard)
        result = await service.synthesize(None, spec, bundle, execution_readiness=_scope())
        assert result.dossier is not None, result.blocker_summary
        details = calls[1][1]["previous_attempt_failure"]["details"]
        assert details["missing_question_ids"] == [work_question_id(spec.questions[-1])]
        assert details["missing_plan_ids"] == [work_question_id(spec.questions[1])]
        assert [c[0] for c in calls] == ["author", "author", "verifier"]
        assert [r["outcome"] for r in result.attempt_records] == ["error", "success", "success"]
        assert len(guarded) == 2 and guarded[1] == calls[1][1]
        assert author.last_usage.model_calls == 2 and verifier.last_usage.model_calls == 1
        assert sum(r["output_tokens"] for r in result.attempt_records) == 30
    asyncio.run(run())


def _definition(schema, field):
    return schema["$defs"][field["$ref"].rsplit("/", 1)[-1]]


def test_actual_schema_and_request_share_question_choices_and_research_shape(monkeypatch):
    async def run():
        service, spec, bundle, _, _, calls = _service(monkeypatch, [None])
        result = await service.synthesize(None, spec, bundle, execution_readiness=_scope())
        assert result.dossier is not None, result.blocker_summary
        _, payload, schema = calls[0]
        catalog = payload["question_identities"]
        assert catalog == {work_question_id(q): q for q in spec.questions}
        section = _definition(schema, schema["properties"]["sections"]["items"])
        question_refs = {_definition(schema, branch)["properties"]["question_ids"]["items"]["$ref"]
            for branch in section["properties"]["claims"]["items"]["anyOf"]}
        assert len(question_refs) == 1
        choices = _definition(schema, {"$ref": next(iter(question_refs))})
        assert set(choices["enum"]) == set(catalog)
        assert set(_definition(schema, schema["properties"]["unresolved_questions"]["items"])["enum"]) == set(spec.questions)
        plans = [_definition(schema, branch) for branch in schema["properties"]["question_dispositions"]["items"]["anyOf"]]
        research = next(p for p in plans if p["properties"]["disposition"].get("const") == "execution_research")
        assert research["properties"]["question_id"]["$ref"] in question_refs
        assert research["properties"]["required_capabilities"]["minItems"] == 1
    asyncio.run(run())


def test_catalog_reuses_existing_normalized_identity_deterministically():
    questions = (" What  IS verified? ", "what is verified?")
    assert work_question_id(questions[0]) == work_question_id(questions[1])
    assert question_catalog(questions) == question_catalog(tuple(reversed(questions)))
    assert set(question_catalog(questions)) == {work_question_id(questions[0])}


def test_answered_claims_and_explicit_research_share_complete_coverage():
    spec, bundle = _fixture()
    payload = _response(spec, bundle)
    payload["sections"][0]["claims"][0]["question_ids"] = [work_question_id(spec.questions[-1])]
    payload["unresolved_questions"] = list(spec.questions[:-1])
    payload["question_dispositions"] = payload["question_dispositions"][:-1]
    selected = ContextSynthesisWorkerDraft.schema_for(bundle, work_spec=spec).model_validate(payload)
    StructuredContextSynthesisService(None)._validate_worker_draft(selected,
        work_spec=spec, bundle=bundle, execution_readiness=_scope())
    assert len(selected.materialize().unresolved_questions) == 2


def test_schema_requires_nonempty_research_capabilities():
    spec, bundle = _fixture()
    payload = _response(spec, bundle)
    payload["question_dispositions"][0]["required_capabilities"] = []
    with pytest.raises(ValidationError):
        ContextSynthesisWorkerDraft.schema_for(bundle, work_spec=spec).model_validate(payload)


def test_machine_readable_missing_identities_survive_summary_truncation():
    original, evidence = _fixture()
    spec = WorkContextSpec.create(planner_version=original.planner_version, objective=original.objective,
        separation_reason=original.separation_reason, questions=tuple(f"Required technical question {i}?" for i in range(30)),
        completion_criteria=original.completion_criteria, workspace_requirement=original.workspace_requirement,
        evidence_requirements=original.evidence_requirements)
    bundle = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence.evidence, items=evidence.items)
    payload = _response(spec, bundle)
    payload["unresolved_questions"] = []
    payload["question_dispositions"] = []
    selected = ContextSynthesisWorkerDraft.schema_for(bundle, work_spec=spec).model_validate(payload)
    with pytest.raises(StructuredResultValidationError) as raised:
        StructuredContextSynthesisService(None)._validate_worker_draft(selected,
            work_spec=spec, bundle=bundle, execution_readiness=_scope())
    feedback = RoleBoundStructuredModel._failure_feedback(raised.value, "synthesis_graph_invalid")
    assert len(json.dumps(feedback["details"])) > 1000
    assert set(feedback["details"]["missing_question_ids"]) == set(question_catalog(spec.questions))


@pytest.mark.parametrize("field", ["claim", "unresolved", "plan"])
def test_unknown_question_input_is_rejected_without_sensitive_feedback(field):
    spec, bundle = _fixture()
    payload = _response(spec, bundle)
    marker = "sk-private-marker-do-not-log"
    if field == "claim":
        payload["sections"][0]["claims"][0]["question_ids"] = [marker]
    elif field == "unresolved":
        payload["unresolved_questions"] = [marker]
    else:
        payload["question_dispositions"][0]["question_id"] = marker
    schema = ContextSynthesisWorkerDraft.schema_for(bundle, work_spec=spec)
    with pytest.raises(ValidationError) as raised:
        schema.model_validate(payload)
    message = safe_validation_message(raised.value)
    assert marker not in message and payload["sections"][0]["claims"][0]["statement"] not in message


def test_author_selection_preserves_static_dto_and_stable_claim_identity():
    spec, bundle = _fixture()
    selected = ContextSynthesisWorkerDraft.schema_for(bundle, work_spec=spec).model_validate(_response(spec, bundle))
    restored = ContextSynthesisWorkerDraft.model_validate_json(selected.model_dump_json())
    assert selected.materialize() == restored.materialize()


@pytest.mark.parametrize("problem", ["duplicate", "extra", "unknown"])
def test_parsed_question_relationship_diagnostics_do_not_modify_candidate(problem):
    spec, bundle = _fixture()
    payload = _response(spec, bundle)
    if problem == "duplicate":
        payload["question_dispositions"].append(payload["question_dispositions"][0])
        expected = "duplicate_plan_ids"
    elif problem == "extra":
        payload["unresolved_questions"] = list(spec.questions[:-1])
        payload["sections"][0]["claims"][0]["question_ids"] = [work_question_id(spec.questions[-1])]
        expected = "extra_plan_ids"
    else:
        payload["sections"][0]["claims"][0]["question_ids"] = ["sk-private-marker-do-not-log"]
        expected = "unknown_reference_hashes"
    selected = ContextSynthesisWorkerDraft.schema_for(bundle, work_spec=spec).model_validate(_response(spec, bundle))
    payload["sections"][0]["claims"][0]["citations"] = selected.model_dump(mode="json")["sections"][0]["claims"][0]["citations"]
    draft = ContextSynthesisWorkerDraft.model_validate(payload)
    before = draft.model_dump_json()
    service = StructuredContextSynthesisService(None)
    with pytest.raises(StructuredResultValidationError) as raised:
        service._validate_worker_draft(draft, work_spec=spec, bundle=bundle, execution_readiness=_scope())
    assert raised.value.details[expected]
    assert "sk-private-marker-do-not-log" not in json.dumps(raised.value.details)
    assert raised.value.violated_rule == "question_coverage" and before == draft.model_dump_json()


def test_unparsed_author_records_actual_schema_error_without_fake_coverage(monkeypatch):
    async def run():
        service, spec, bundle, _, _, calls = _service(monkeypatch, ["unknown_question", "unknown_question"])
        result = await service.synthesize(None, spec, bundle, execution_readiness=_scope())
        assert result.blocker_code == "synthesis_invalid" and result.review is None
        assert [c[0] for c in calls] == ["author", "author"]
        assert all(r["failure_category"] == "model_schema_error" for r in result.attempt_records)
        assert all("details" not in r["validation_feedback"] for r in result.attempt_records)
        assert "sk-private-marker-do-not-log" not in json.dumps(result.attempt_records)
    asyncio.run(run())


def test_bundle_identity_mismatch_blocks_without_provider_calls(monkeypatch):
    async def run():
        service, spec, bundle, _, _, calls = _service(monkeypatch, [None, None])
        result = await service.synthesize(None, spec, bundle.model_copy(update={"work_spec_id": "0" * 64}))
        assert result.blocker_code == "synthesis_invalid"
        assert calls == [] and result.attempt_records == ()
    asyncio.run(run())


def test_capability_failure_stays_blocked_before_support_verifier(monkeypatch):
    async def run():
        service, spec, bundle, _, _, calls = _service(monkeypatch, [None, None])
        result = await service.synthesize(None, spec, bundle, execution_readiness=_scope(("read",)))
        assert result.blocker_code == "synthesis_invalid" and result.dossier is None
        assert [c[0] for c in calls] == ["author", "author"]
    asyncio.run(run())


@pytest.mark.parametrize("verdict", ["unsupported", "unknown"])
def test_corrected_author_does_not_retry_or_override_support_verdict(monkeypatch, verdict):
    async def run():
        service, spec, bundle, _, _, calls = _service(monkeypatch, ["both", None], verdict=verdict)
        result = await service.synthesize(None, spec, bundle, execution_readiness=_scope())
        assert result.blocker_code == "synthesis_invalid" and result.review is not None
        assert [c[0] for c in calls] == ["author", "author", "verifier"]
        assert result.review.support_assessments[0].verdict == verdict
    asyncio.run(run())


def test_compiler_persists_safe_gaps_consumption_and_separate_old_failure(tmp_path, monkeypatch, runtime_postgres_database):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        seeded = await _seed_loop(sessions, tmp_path, label="qgap", started_at=datetime.now(UTC))
        try:
            service, spec, bundle, _, _, calls = _service(monkeypatch, ["missing_plan", "missing_question"])
            observation = _compiler_observation(seeded)
            readiness = await load_execution_readiness(sessions, observation, spec)
            inputs = (bundle.resolution_id, stable_expansion_hash("execution-readiness", readiness.model_dump(mode="json")))
            opportunity = ExpansionOpportunity.create(loop_id=seeded["loop_id"], round_id=seeded["round_id"],
                observation_hash="a" * 64, work_spec=spec, manifest_sources=bundle.source_frontier)
            compiler = ContextExpansionPlanCompiler(sessions, None, synthesizer=service)
            old_payload = {"blocker_code": "synthesis_invalid", "blocker_summary": "Retained old contract failure", "review": None}
            await compiler._save_artifact(opportunity, "dossier_synthesis", inputs, "structured-context-synthesizer-v10",
                old_payload, outcome="blocked")
            blocker = await compiler._synthesize_dossier_stage(observation, opportunity, bundle, [],
                artifact_expansion_id=None, persist_artifacts=True)
            assert blocker.code == "synthesis_invalid" and len(calls) == 2
            async with sessions() as session:
                repository = SemanticDerivationArtifactRepository()
                old = await repository.stage_artifact(session, loop_id=opportunity.loop_id, round_id=opportunity.round_id,
                    stage="dossier_synthesis", input_identities=inputs, version="structured-context-synthesizer-v10")
                current = await repository.stage_artifact(session, loop_id=opportunity.loop_id, round_id=opportunity.round_id,
                    stage="dossier_synthesis", input_identities=inputs, version=service.VERSION)
                assert old.payload == old_payload and old.outcome == current.outcome == "blocked"
                assert old.artifact_id != current.artifact_id and current.payload["review"] is None
                assert current.attempt_records[1]["validation_feedback"]["details"]["missing_question_ids"]
                assert "交付可构建的 Obsidian 插件仓库。" not in json.dumps(current.attempt_records, ensure_ascii=False)
                usage = await session.get(LoopBudgetUsage, opportunity.loop_id)
                assert usage.model_calls == 2 and usage.input_tokens == 80 and usage.output_tokens == 20
            assert await compiler._artifact_payload(opportunity, "dossier_synthesis", inputs, service.VERSION) is None
        finally:
            await _stop(seeded["service"], seeded["loop_id"])
            await engine.dispose()
    asyncio.run(run())
