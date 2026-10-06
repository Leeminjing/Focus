"""本文件对外提供质量评估冻结身份与原有界纠错的回归。
输入为实际WorkSpec、证据包、Dossier与仅替换远端响应的Provider；输出为身份拒绝、逐次反馈、原尝试上限和真实消费断言。
具体工作流使用生产RoleBoundStructuredModel提交越界引用或重复维度，再核对成功记录之前校验；合法fail/unknown不得被重试为pass。
示例：pytest backend/tests/test_quality_claim_feedback.py -q；不连接用户Vault或改写模型的语义判定。
"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

from backend.app.desktop.agent_loop.context_expansion.quality_verifier import StructuredContextQualityService
from backend.app.desktop.agent_loop.context_expansion.synthesis_service import DeterministicTestContextSynthesisService
from backend.app.desktop.agent_loop.derivation_worker import RoleBoundStructuredModel
from backend.app.desktop.context_curation import evidence_ref_key
from backend.tests.config_helpers import app_config_for
from backend.tests.test_synthesis_claim_feedback import _inputs


def _service(monkeypatch, *, invalid=None, persistent=False, verdict="pass"):
    config = app_config_for("quality-feedback", None)
    config.models[0].curation_output_method = "prompt_json"
    spec, bundle = _inputs()
    calls = []

    class Provider:
        async def ainvoke(self, messages, config):
            document = json.loads(messages[-1].content.split("<worker_input>", 1)[1].split("</worker_input>", 1)[0])
            calls.append(document)
            config["callbacks"][0].usage_metadata["quality-feedback"] = {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50}
            dimensions = [
                {"dimension": name, "verdict": verdict, "reasons": ["Controlled frozen review"],
                 "evidence_keys": [list(evidence_ref_key(bundle.evidence_frontier[0]))],
                 "requirement_ids": [spec.evidence_requirements[0].requirement_id],
                 "claim_ids": [document["dossier"]["claims"][0]["claim_id"]]}
                for name in ("minimality", "sufficiency", "coherence")
            ]
            if persistent or len(calls) == 1:
                if invalid == "evidence":
                    dimensions[0]["evidence_keys"][0][-1] = "foreign-evidence"
                elif invalid == "requirement":
                    dimensions[0]["requirement_ids"] = ["foreign-requirement"]
                elif invalid == "claim":
                    dimensions[0]["claim_ids"] = ["0" * 64]
                elif invalid == "dimension":
                    dimensions[1]["dimension"] = "minimality"
            return AIMessage(content=json.dumps({"dimensions": dimensions}))

    monkeypatch.setattr("backend.app.desktop.agent_loop.structured_worker.create_chat_model", lambda **kwargs: Provider())
    model = RoleBoundStructuredModel(config, "context_quality_verifier", max_attempts=2)
    return StructuredContextQualityService(model), spec, bundle, model, calls


async def _verify(service, spec, bundle):
    synthesis = await DeterministicTestContextSynthesisService().synthesize(None, spec, bundle)
    assert synthesis.dossier is not None
    return await service.verify(spec, bundle, synthesis.dossier)


@pytest.mark.parametrize("invalid", ["evidence", "requirement", "claim", "dimension"])
def test_invalid_quality_contract_is_corrected_before_success(monkeypatch, invalid):
    async def run():
        service, spec, bundle, model, calls = _service(monkeypatch, invalid=invalid)
        result = await _verify(service, spec, bundle)
        assert result.assessment is not None and result.assessment.passes, result.blocker_summary
        assert len(calls) == 2
        assert [item["outcome"] for item in result.attempt_records] == ["error", "success"]
        assert calls[1]["previous_attempt_failure"]["category"] == "quality_contract_invalid"
        assert model.last_usage.model_calls == 2
        assert sum(item["input_tokens"] for item in result.attempt_records) == 80
        assert sum(item["output_tokens"] for item in result.attempt_records) == 20
    asyncio.run(run())


@pytest.mark.parametrize("invalid", ["evidence", "requirement", "claim", "dimension"])
def test_persistent_invalid_quality_exhausts_original_bound(monkeypatch, invalid):
    async def run():
        service, spec, bundle, model, calls = _service(monkeypatch, invalid=invalid, persistent=True)
        result = await _verify(service, spec, bundle)
        assert result.blocker_code == "quality_contract_invalid" and result.assessment is None
        assert len(calls) == 2 and model.last_usage.model_calls == 2
        assert [item["outcome"] for item in result.attempt_records] == ["error", "error"]
    asyncio.run(run())


@pytest.mark.parametrize("verdict", ["pass", "fail", "unknown"])
def test_valid_quality_verdict_is_never_rewritten_or_retried(monkeypatch, verdict):
    async def run():
        service, spec, bundle, model, calls = _service(monkeypatch, verdict=verdict)
        result = await _verify(service, spec, bundle)
        assert result.assessment is not None
        assert {item.verdict for item in result.assessment.dimensions} == {verdict}
        assert len(calls) == 1 and model.last_usage.model_calls == 1
        assert result.blocker_code == (None if verdict == "pass" else "context_quality_failed")
        assert [item["outcome"] for item in result.attempt_records] == ["success"]
    asyncio.run(run())


def test_correction_uses_original_admission_and_actual_receipts(monkeypatch):
    async def run():
        service, spec, bundle, model, calls = _service(monkeypatch, invalid="evidence")
        admissions, settlements, reservations = [], [], []

        async def admit(worker, schema, system, payload):
            admissions.append(payload)

        class Receipts:
            async def reserve(self, input_size, output_limit):
                reservations.append((input_size, output_limit))
                return len(reservations)

            async def settle(self, receipt, usage, reported):
                settlements.append((receipt, usage.model_calls, usage.input_tokens, usage.output_tokens, reported))

        model.bind_request_guard(admit)
        service.bind_usage_receipts(Receipts())
        result = await _verify(service, spec, bundle)
        assert result.assessment is not None and result.assessment.passes
        assert len(admissions) == len(reservations) == len(calls) == 2
        assert admissions[1]["previous_attempt_failure"]["category"] == "quality_contract_invalid"
        assert settlements == [(1, 1, 40, 10, True), (2, 1, 40, 10, True)]
        assert all(item["usage_managed"] for item in result.attempt_records)
    asyncio.run(run())


def test_failed_preflight_makes_no_provider_call(monkeypatch):
    async def run():
        service, spec, bundle, model, calls = _service(monkeypatch)
        synthesis = await DeterministicTestContextSynthesisService().synthesize(None, spec, bundle)
        dossier = synthesis.dossier.model_copy(update={"work_spec_id": "0" * 64})
        result = await service.verify(spec, bundle, dossier)
        assert result.blocker_code == "quality_preflight_failed"
        assert calls == [] and model.last_usage.model_calls == 0
    asyncio.run(run())


@pytest.mark.parametrize("invalid", [False, True])
def test_invoke_only_adapter_is_checked_once(invalid):
    async def run():
        spec, bundle = _inputs()
        calls = []

        class Model:
            async def invoke(self, schema, system, payload):
                calls.append(payload)
                return schema.model_validate({"dimensions": [
                    {"dimension": name, "verdict": "fail", "reasons": ["Controlled adapter review"],
                     "requirement_ids": ["foreign-requirement"] if invalid else ["state"]}
                    for name in ("minimality", "sufficiency", "coherence")]})

        result = await _verify(StructuredContextQualityService(Model()), spec, bundle)
        assert len(calls) == 1
        assert result.blocker_code == ("quality_contract_invalid" if invalid else "context_quality_failed")
        assert (result.assessment is None) == invalid
    asyncio.run(run())
