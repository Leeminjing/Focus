"""本文件对外提供生产声明支持修订与恢复回归。

输入为真实故障模式的冻结 Mission、生产 RoleBound 入口与受控 Provider；输出为正确引用、原判定、私有反馈和真实消费断言。
工作流为 outcome 术语改写被拒绝后，作者引用明确支持的 invariant，独立重验新声明，再通过独立 PostgreSQL 核对审计与缓存恢复。
示例：pytest backend/tests/test_synthesis_support_repair.py -q；不访问用户 Vault 或真实模型，不改写原 verdict。
"""
import asyncio
import json
import os
from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.context_expansion.contracts import ExpansionOpportunity, ResolvedEvidenceBundle, ResolvedEvidenceItem, stable_expansion_hash
from backend.app.desktop.agent_loop.context_expansion.artifact_repository import SemanticDerivationArtifactRepository
from backend.app.desktop.agent_loop.context_expansion.compiler import ContextExpansionPlanCompiler
from backend.app.desktop.agent_loop.context_expansion.start_readiness import load_execution_readiness
from backend.app.desktop.agent_loop.context_expansion.synthesis import ContextSynthesisValidator
from backend.app.desktop.agent_loop.context_expansion.synthesis_service import StructuredContextSynthesisService
from backend.app.desktop.agent_loop.derivation_worker import RoleBoundStructuredModel
from backend.app.desktop.context_curation import MultiSourceEvidence, StructuredEvidence, evidence_ref_key
from backend.tests.test_synthesis_claim_feedback import _inputs
from backend.tests.test_synthesis_claim_feedback import _compiler_observation
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.app.desktop.agent_loop.models import LoopBudgetUsage
from backend.tests.config_helpers import app_config_for


def _repair_service(monkeypatch, *, change=True, verdict="unsupported", verifier_fails=False):
    spec, original = _inputs(mission=True)
    outcome = original.evidence_frontier[0]
    invariant = outcome.model_copy(update={"section_kind": "boundary", "item_id": "required_invariants", "content_hash": "b" * 64})
    evidence = MultiSourceEvidence(sources=original.evidence.sources, evidence_frontier=(outcome, invariant), structured=(
        StructuredEvidence(ref=outcome, content="模型层、Agent Runtime、Obsidian Tools、UI、配置和会话状态模块化解耦。"),
        StructuredEvidence(ref=invariant, content="UI、Agent Runtime、Model Adapter、Obsidian Tools、配置持久化、会话状态必须保持模块化边界。"),
    ))
    bundle = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence, items=tuple(
        ResolvedEvidenceItem(requirement_id="state", ref=ref, content_hash=ref.content_hash, relevance_reason="Frozen mission section")
        for ref in evidence.evidence_frontier
    ))
    config = app_config_for("support-repair", None)
    config.models[0].curation_output_method = "prompt_json"
    calls = []
    authors = 0

    class Provider:
        async def ainvoke(self, messages, config):
            nonlocal authors
            body = messages[-1].content
            document = json.loads(body.split("<worker_input>", 1)[1].split("</worker_input>", 1)[0])
            role = "author" if "work_spec" in document else "verifier"
            calls.append((role, document))
            config["callbacks"][0].usage_metadata["support-repair"] = {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50}
            if role == "author":
                authors += 1
                ref = invariant if change and authors > 1 else outcome
                catalog = document["citation_catalog"]
                key = next(key for key, value in catalog.items() if value["item_id"] == ref.item_id)
                response = {"sections": [{"title": "UI boundaries", "claims": [{
                    "claim_key": "boundary", "statement": "UI 与 Model Adapter 必须保持模块化边界。",
                    "authority": "confirmed", "citations": [key], "requirement_ids": ["state"],
                    "question_ids": [ContextSynthesisValidator.question_id(spec.questions[0])],
                }]}]}
            else:
                if verifier_fails:
                    if verifier_fails == "cancel":
                        raise asyncio.CancelledError()
                    raise TimeoutError("verifier unavailable")
                fixed = document["evidence"][0]["source"]["ref"]["item_id"] == invariant.item_id
                response = {"assessments": [{"claim_id": claim["claim_id"],
                    "verdict": "supported" if fixed else verdict,
                    "reason": "PRIVATE: selected outcome has no Model Adapter; cite the invariant." if not fixed else "Invariant directly supports the claim.",
                } for claim in document["claims"]]}
            return AIMessage(content=json.dumps(response))

    monkeypatch.setattr("backend.app.desktop.agent_loop.structured_worker.create_chat_model", lambda **kwargs: Provider())
    author = RoleBoundStructuredModel(config, "dossier_synthesizer", max_attempts=2)
    verifier = RoleBoundStructuredModel(config, "claim_verifier", max_attempts=2)
    return StructuredContextSynthesisService(author, verifier), spec, bundle, author, verifier, calls


def test_live_pattern_repairs_citation_and_reverifies_new_claim(monkeypatch):
    async def run():
        service, spec, bundle, author, verifier, calls = _repair_service(monkeypatch)
        result = await service.synthesize(None, spec, bundle)
        assert result.dossier is not None, result.blocker_summary
        assert [role for role, _ in calls] == ["author", "verifier", "author", "verifier"]
        original, = result.rejected_reviews
        assert original.support_assessments[0].verdict == "unsupported"
        assert original.draft.claims[0].citations[0].item_id == "outcome"
        repaired = result.dossier.claims[0]
        assert repaired.citations[0].item_id == "required_invariants"
        assert repaired.claim_id != original.draft.claims[0].claim_id
        assert result.dossier.support_assessments[0].verdict == "supported"
        feedback = calls[2][1]["previous_attempt_correction"]
        assert feedback["rejected_review"] == original.model_dump(mode="json")
        assert feedback["rejected_review"]["support_assessments"][0]["reason"].startswith("PRIVATE:")
        assert "PRIVATE:" not in json.dumps(result.attempt_records)
        assert author.last_usage.model_calls == verifier.usage.model_calls == 2
        assert len(result.attempt_records) == 4
        assert sum(r["input_tokens"] for r in result.attempt_records) == 160
    asyncio.run(run())


@pytest.mark.parametrize("verdict", ["unsupported", "unknown"])
def test_unchanged_claim_does_not_reroll_verdict_and_exhausts_author_bound(monkeypatch, verdict):
    async def run():
        service, spec, bundle, author, verifier, calls = _repair_service(monkeypatch, change=False, verdict=verdict)
        result = await service.synthesize(None, spec, bundle)
        assert result.dossier is None and result.blocker_code == "synthesis_invalid"
        assert [role for role, _ in calls] == ["author", "verifier", "author"]
        assert author.last_usage.model_calls == 2 and verifier.usage.model_calls == 1
        assert result.review.support_assessments[0].verdict == verdict
        assert len(result.rejected_reviews) == 1
        assert "PRIVATE:" not in result.blocker_summary
        assert "PRIVATE:" not in json.dumps(result.attempt_records)
    asyncio.run(run())


def test_verifier_failure_does_not_spend_author_repair_attempts(monkeypatch):
    async def run():
        service, spec, bundle, author, verifier, calls = _repair_service(monkeypatch, verifier_fails=True)
        with pytest.raises(TimeoutError, match="verifier unavailable"):
            await service.synthesize(None, spec, bundle)
        assert [role for role, _ in calls] == ["author", "verifier", "verifier"]
        assert author.last_usage.model_calls == 1 and verifier.usage.model_calls == 2
    asyncio.run(run())


def test_verifier_cancellation_stops_both_roles(monkeypatch):
    async def run():
        service, spec, bundle, author, verifier, calls = _repair_service(monkeypatch, verifier_fails="cancel")
        with pytest.raises(asyncio.CancelledError):
            await service.synthesize(None, spec, bundle)
        assert [role for role, _ in calls] == ["author", "verifier"]
        assert author.last_attempt_records[0]["outcome"] == verifier.last_attempt_records[0]["outcome"] == "cancelled"
        assert author.last_usage.model_calls == verifier.usage.model_calls == 1
    asyncio.run(run())


def test_private_correction_is_admitted_before_spending_next_attempt(monkeypatch):
    async def run():
        service, spec, bundle, author, verifier, calls = _repair_service(monkeypatch)
        guarded = []
        async def guard(worker, schema, system, payload):
            guarded.append(payload)
            if "previous_attempt_correction" in payload:
                raise ValueError("test request budget exhausted")
        author.bind_request_guard(guard)
        result = await service.synthesize(None, spec, bundle)
        assert result.dossier is None and "test request budget exhausted" in result.blocker_summary
        assert [role for role, _ in calls] == ["author", "verifier"]
        assert len(guarded) == 2 and "PRIVATE:" in json.dumps(guarded[1])
        assert len(result.attempt_records) == 2
        assert "PRIVATE:" not in json.dumps(result.attempt_records)
        assert author.last_usage.model_calls == verifier.usage.model_calls == 1
    asyncio.run(run())


def test_repaired_dossier_and_original_rejection_survive_compiler_restore(tmp_path, monkeypatch, runtime_postgres_database):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        seeded = await _seed_loop(sessions, tmp_path, label="support-repair", started_at=datetime.now(UTC))
        try:
            service, spec, bundle, _, _, calls = _repair_service(monkeypatch)
            observation = _compiler_observation(seeded)
            opportunity = ExpansionOpportunity.create(loop_id=seeded["loop_id"], round_id=seeded["round_id"],
                observation_hash="a" * 64, work_spec=spec, manifest_sources=bundle.source_frontier)
            compiler = ContextExpansionPlanCompiler(sessions, None, synthesizer=service)
            dossier = await compiler._synthesize_dossier_stage(observation, opportunity, bundle, [],
                artifact_expansion_id=None, persist_artifacts=True)
            assert dossier.claims[0].citations[0].item_id == "required_invariants"
            assert len(calls) == 4
            readiness = await load_execution_readiness(sessions, observation, spec)
            inputs = (bundle.resolution_id, stable_expansion_hash("execution-readiness", readiness.model_dump(mode="json")))
            async with sessions() as session:
                repository = SemanticDerivationArtifactRepository()
                audit = await repository.stage_artifact(session, loop_id=opportunity.loop_id,
                    round_id=opportunity.round_id, stage="dossier_synthesis_reviews", input_identities=inputs,
                    version=service.VERSION)
                assert audit.outcome == "recorded"
                rejected, = audit.payload["reviews"]
                assert rejected["support_assessments"][0]["verdict"] == "unsupported"
                assert rejected["draft"]["claims"][0]["citations"][0]["item_id"] == "outcome"
                ready = await repository.stage_artifact(session, loop_id=opportunity.loop_id,
                    round_id=opportunity.round_id, stage="dossier_synthesis", input_identities=inputs,
                    version=service.VERSION)
                assert ready.outcome == "ready" and "reviews" not in ready.payload
                assert "PRIVATE:" not in json.dumps(ready.attempt_records)
                usage = await session.get(LoopBudgetUsage, opportunity.loop_id)
                assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (4, 160, 40)
            restored = await ContextExpansionPlanCompiler(sessions, None, synthesizer=service)._synthesize_dossier_stage(
                observation, opportunity, bundle, [], artifact_expansion_id=None, persist_artifacts=True)
            assert restored == dossier and len(calls) == 4
            assert await compiler._artifact_payload(opportunity, "dossier_synthesis_reviews", inputs, service.VERSION) is None
        finally:
            await _stop(seeded["service"], seeded["loop_id"])
            await engine.dispose()
    asyncio.run(run())
