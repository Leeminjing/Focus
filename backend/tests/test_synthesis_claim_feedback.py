"""本文件对外提供正式 Dossier 合同校验与有界模型纠错回归。
输入为冻结单来源证据、生产 RoleBoundStructuredModel 和仅替换远端响应的 Provider；输出为严格来源、纠错反馈与实际消费断言。
具体工作流为提交非法引用/claim graph，核对有界纠错；编译恢复 fixture 从真实 Loop snapshot 绑定不可变 observation。
实际 Provider 请求须包含冻结 claim identity 枚举和精确 assessment 数量，重复集合仍由同一确定性 validator 拒绝。
作者响应使用 section 内嵌的唯一声明清单及冻结问题选择；跨组重复本地 key 仍拒绝，不补造或忽略声明。
受控作者从实际冻结目录选择引用键；非法引用保持未知键，较早的 Schema 拒绝如实记录，原两次上限保持。
示例：pytest backend/tests/test_synthesis_claim_feedback.py -q；不连接用户 Vault、不伪造独立支持或修改证据。
"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from pydantic import TypeAdapter

from backend.app.desktop.agent_loop.context_expansion.contracts import EvidenceRequirement, ResolvedEvidenceBundle, ResolvedEvidenceItem, WorkContextSpec, stable_expansion_hash
from backend.app.desktop.agent_loop.context_expansion.synthesis import ClaimSupportProposal, ContextSynthesisValidator
from backend.app.desktop.agent_loop.context_expansion.synthesis_service import StructuredContextSynthesisService
from backend.app.desktop.agent_loop.derivation_worker import RoleBoundStructuredModel
from backend.app.desktop.agent_loop.observation import LoopObservationBuilder
from backend.app.desktop.context_curation import EvidenceRef, MissionEvidenceRef, MultiSourceEvidence, StructuredEvidence, evidence_ref_key
from backend.tests._semantic_context_fixtures import single_source_fixture
from backend.tests.config_helpers import app_config_for


def _inputs(*, mission=False):
    evidence = single_source_fixture()
    ref = evidence.evidence_frontier[0]
    if mission:
        ref = MissionEvidenceRef(identity_version="mission-section-v2", loop_id="loop-test", goal_revision=1,
            section_kind="outcome", item_id="outcome", content_hash="a" * 64)
        evidence = MultiSourceEvidence(sources=evidence.sources,
            structured=(StructuredEvidence(ref=ref, content="Continue from this verified state."),), evidence_frontier=(ref,))
    spec = WorkContextSpec.create(planner_version="test-v1", objective="Continue the verified state", separation_reason="Independent review",
        questions=("What is verified?",), completion_criteria=("The source is reviewed",), workspace_requirement="read_only",
        evidence_requirements=(EvidenceRequirement(requirement_id="state", role="conversation", question="What is verified?", coverage_criterion="Exact frozen source"),))
    bundle = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence,
        items=(ResolvedEvidenceItem(requirement_id="state", ref=ref, content_hash=evidence.sources[0].content_hash, relevance_reason="Exact source"),))
    return spec, bundle


def _compiler_observation(seeded):
    snapshot = seeded["snapshot"]
    return LoopObservationBuilder().build(loop_id=seeded["loop_id"], round_id=seeded["round_id"],
        loop_revision=snapshot["revision"], goal_revision=snapshot["goal_revision"],
        authority_revision=snapshot["authority_revision"], observed_frontier_hash="a" * 64,
        mission={key: snapshot["mission"][key] for key in ("outcome", "boundaries", "completion_checks")},
        goal=snapshot["goal"], grant=snapshot["grant"],
        portfolio_frontier=(), workspace={"workspace_id": snapshot["workspace_id"]}, budget={})


def _draft(spec, bundle, invalid=None):
    ref = bundle.evidence_frontier[0].model_dump(mode="json")
    if invalid == "citation":
        ref["message_id"] = "not-in-frozen-evidence"
    if invalid == "mission":
        ref.update(section_kind="boundary", item_id="in_scope", content_hash="b" * 64)
    claim = {"claim_key": "state", "statement": "Continue from this verified state.", "authority": "confirmed", "citations": [ref],
        "requirement_ids": ["state"], "question_ids": [ContextSynthesisValidator.question_id(spec.questions[0])]}
    if invalid == "requirement":
        claim["requirement_ids"] = ["invented-requirement"]
    if invalid == "question":
        claim["question_ids"] = ["0" * 64]
    if invalid == "atomic":
        claim["statement"] += "\nAnother claim."
    sections = [{"title": "State", "claims": [claim]}]
    if invalid == "section":
        sections.append({"title": "Repeated state", "claims": [dict(claim)]})
    return {"sections": sections}


def _service(monkeypatch, *, synthesis_error=None, verifier_error=None, persistent=False, inspect_contract=False):
    config = app_config_for("synthesis-feedback", None)
    config.models[0].curation_output_method = "prompt_json"
    spec, bundle = _inputs(mission=synthesis_error == "mission")
    calls = []
    counts = {"synthesis": 0, "verifier": 0}

    class Provider:
        async def ainvoke(self, messages, config):
            body = messages[-1].content
            document = json.loads(body.split("<worker_input>", 1)[1].split("</worker_input>", 1)[0])
            role = "synthesis" if "work_spec" in document else "verifier"
            counts[role] += 1
            calls.append((role, document))
            config["callbacks"][0].usage_metadata["synthesis-feedback"] = {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50}
            invalid = persistent or counts[role] == 1
            if role == "synthesis":
                if inspect_contract:
                    contract = json.loads(body.split("JSON Schema: ", 1)[1])
                    assert "claims" not in contract["properties"]
                    section = contract["$defs"][contract["properties"]["sections"]["items"]["$ref"].rsplit("/", 1)[-1]]
                    assert "claim_keys" not in section["properties"] and section["properties"]["claims"]["minItems"] == 1
                response = _draft(spec, bundle, synthesis_error if invalid else None)
                for section in response["sections"]:
                    for claim in section["claims"]:
                        claim["citations"] = [stable_expansion_hash("synthesis-citation-input-v1",
                            evidence_ref_key(TypeAdapter(EvidenceRef).validate_python(ref))) for ref in claim["citations"]]
            else:
                if inspect_contract:
                    contract = json.loads(body.split("JSON Schema: ", 1)[1])
                    collection = contract["properties"]["assessments"]
                    item = contract["$defs"][collection["items"]["$ref"].rsplit("/", 1)[-1]]
                    identity_contract = item["properties"]["claim_id"]
                    allowed = identity_contract.get("enum", [identity_contract.get("const")])
                    assert set(allowed) == {claim["claim_id"] for claim in document["claims"]}
                    assert collection["minItems"] == collection["maxItems"] == len(document["claims"])
                identity = document["claims"][0]["claim_id"]
                assessment = {"claim_id": identity, "verdict": "unsupported" if verifier_error == "unsupported" else "supported", "reason": "Frozen source supports the statement"}
                assessments = [assessment]
                if invalid and verifier_error == "missing":
                    assessments = []
                elif invalid and verifier_error == "duplicate":
                    assessments = [assessment, assessment]
                elif invalid and verifier_error == "extra":
                    assessments.append({**assessment, "claim_id": "0" * 64})
                response = {"assessments": assessments}
            return AIMessage(content=json.dumps(response))

    monkeypatch.setattr("backend.app.desktop.agent_loop.structured_worker.create_chat_model", lambda **kwargs: Provider())
    synthesis = RoleBoundStructuredModel(config, "dossier_synthesizer", max_attempts=2)
    verifier = RoleBoundStructuredModel(config, "claim_verifier", max_attempts=2)
    return StructuredContextSynthesisService(synthesis, verifier), spec, bundle, synthesis, verifier, calls


@pytest.mark.parametrize("invalid", ["citation", "mission", "section", "requirement", "question", "atomic"])
def test_invalid_graph_is_corrected_before_success_and_verification(monkeypatch, invalid):
    async def run():
        service, spec, bundle, synthesis, verifier, calls = _service(monkeypatch, synthesis_error=invalid)
        result = await service.synthesize(None, spec, bundle)
        assert result.dossier is not None, result.blocker_summary
        assert [role for role, _ in calls] == ["synthesis", "synthesis", "verifier"]
        assert [record["outcome"] for record in synthesis.last_attempt_records] == ["error", "success"]
        category = "model_schema_error" if invalid in {"citation", "mission", "question"} else "synthesis_graph_invalid"
        assert calls[1][1]["previous_attempt_failure"]["category"] == category
        assert synthesis.last_usage.model_calls == 2 and verifier.last_usage.model_calls == 1
        assert sum(record["input_tokens"] for record in result.attempt_records) == 120
        assert sum(record["output_tokens"] for record in result.attempt_records) == 30
    asyncio.run(run())


def test_persistent_invalid_citation_exhausts_original_bound_without_verifier(monkeypatch):
    async def run():
        service, spec, bundle, synthesis, verifier, calls = _service(monkeypatch, synthesis_error="citation", persistent=True)
        result = await service.synthesize(None, spec, bundle)
        assert result.dossier is None and result.blocker_code == "synthesis_invalid"
        assert len(calls) == 2 and all(role == "synthesis" for role, _ in calls)
        assert [record["outcome"] for record in result.attempt_records] == ["error", "error"]
        assert synthesis.last_usage.model_calls == 2 and verifier.last_usage.model_calls == 0
    asyncio.run(run())


@pytest.mark.parametrize("invalid", ["missing", "duplicate", "extra"])
def test_verifier_contract_covers_exactly_frozen_confirmed_claims(monkeypatch, invalid):
    async def run():
        service, spec, bundle, synthesis, verifier, calls = _service(monkeypatch, verifier_error=invalid)
        result = await service.synthesize(None, spec, bundle)
        assert result.dossier is not None, result.blocker_summary
        assert [role for role, _ in calls] == ["synthesis", "verifier", "verifier"]
        assert calls[-1][1]["previous_attempt_failure"]["category"] == "model_schema_error"
        assert [record["outcome"] for record in verifier.last_attempt_records] == ["error", "success"]
        assert len(result.attempt_records) == 3
    asyncio.run(run())


def test_unsupported_verdict_is_preserved_while_author_gets_bounded_repair(monkeypatch):
    async def run():
        service, spec, bundle, synthesis, verifier, calls = _service(monkeypatch, verifier_error="unsupported")
        result = await service.synthesize(None, spec, bundle)
        assert result.dossier is None and result.blocker_code == "synthesis_invalid"
        assert "direct-support" in result.blocker_summary
        assert [role for role, _ in calls] == ["synthesis", "verifier", "synthesis"]
        assert len(result.rejected_reviews) == 1
        assert [record["outcome"] for record in synthesis.last_attempt_records] == ["error", "error"]
        assert verifier.usage.model_calls == 1
    asyncio.run(run())


def test_actual_verifier_request_declares_exact_frozen_input_contract(monkeypatch):
    async def run():
        service, spec, bundle, synthesis, verifier, calls = _service(monkeypatch, inspect_contract=True)
        result = await service.synthesize(None, spec, bundle)
        assert result.dossier is not None, result.blocker_summary
        assert [role for role, _ in calls] == ["synthesis", "verifier"]
    asyncio.run(run())


def test_duplicate_known_id_cannot_replace_a_second_confirmed_claim():
    from types import SimpleNamespace
    schema = ClaimSupportProposal.schema_for(("a" * 64, "b" * 64))
    candidate = schema.model_validate({"assessments": [
        {"claim_id": "a" * 64, "verdict": "supported", "reason": "First source"},
        {"claim_id": "a" * 64, "verdict": "supported", "reason": "First source"},
    ]})
    with pytest.raises(ValueError, match="恰好逐项覆盖"):
        StructuredContextSynthesisService._validate_support_proposal(candidate,
            confirmed=(SimpleNamespace(claim_id="a" * 64), SimpleNamespace(claim_id="b" * 64)))


@pytest.mark.parametrize("claim_ids", [(), ("a" * 64,), ("a" * 64, "b" * 64)])
def test_frozen_verdict_contract_preserves_unknown_and_exact_coverage(claim_ids):
    from pydantic import ValidationError
    schema = ClaimSupportProposal.schema_for(claim_ids)
    entries = [{"claim_id": key, "verdict": "unknown", "reason": "No independent support"} for key in claim_ids]
    proposal = schema.model_validate({"assessments": entries})
    assert tuple(item.claim_id for item in proposal.assessments) == claim_ids
    assert all(item.verdict == "unknown" for item in proposal.assessments)
    with pytest.raises(ValidationError):
        schema.model_validate({"assessments": entries + [{"claim_id": "c" * 64, "verdict": "supported", "reason": "New identity"}]})
    if entries:
        with pytest.raises(ValidationError):
            schema.model_validate({"assessments": entries[:-1]})
        with pytest.raises(ValidationError):
            schema.model_validate({"assessments": [{**entry, "claim_id": "c" * 64} for entry in entries]})
