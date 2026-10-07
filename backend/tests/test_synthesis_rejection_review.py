"""本文件对外提供 synthesis 拒绝候选的来源保留与持久化回归。
输入为生产 RoleBound 入口、冻结证据、真实 observation 及独立 unsupported 判定；输出为候选/判定精确关联与私有产物断言。
具体工作流为只替换远端响应，调用真实服务及 PostgreSQL 编译阶段，从新连接读取失败产物；失败记录不能作为 ready dossier 恢复或发布。
示例：pytest backend/tests/test_synthesis_rejection_review.py -q；不会调用真实 Provider、用户 Vault 或修改原历史。
"""

import asyncio
from datetime import UTC, datetime
import os

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.context_expansion.artifact_repository import SemanticDerivationArtifactRepository
from backend.app.desktop.agent_loop.context_expansion.compiler import ContextExpansionPlanCompiler
from backend.app.desktop.agent_loop.context_expansion.contracts import ExpansionOpportunity, stable_expansion_hash
from backend.app.desktop.agent_loop.context_expansion.start_readiness import load_execution_readiness
from backend.app.desktop.agent_loop.context_expansion.synthesis import ContextSynthesisReview
from backend.app.desktop.context_curation import evidence_ref_key
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.test_synthesis_claim_feedback import _compiler_observation, _service


def test_rejected_candidate_preserves_actual_independent_assessment(monkeypatch):
    async def run():
        service, spec, bundle, synthesis, verifier, calls = _service(monkeypatch, verifier_error="unsupported")
        result = await service.synthesize(None, spec, bundle)
        review = getattr(result, "review", None)
        assert review is not None, "support rejection discarded its candidate and assessment"
        assert result.dossier is None and result.blocker_code == "synthesis_invalid"
        assert review.work_spec_id == spec.work_spec_id and review.resolution_id == bundle.resolution_id
        claim = review.draft.claims[0]
        assessment = review.support_assessments[0]
        assert assessment.claim_id == claim.claim_id
        assert claim.statement == calls[1][1]["claims"][0]["statement"]
        assert assessment.verdict == "unsupported"
        assert assessment.reason == "Frozen source supports the statement"
        assert assessment.citation_keys == tuple(evidence_ref_key(ref) for ref in claim.citations)
        restored = ContextSynthesisReview.model_validate_json(review.model_dump_json())
        assert restored == review and restored.support_assessments[0].verdict == "unsupported"
        assert claim.statement not in result.blocker_summary and assessment.reason not in result.blocker_summary
        assert [role for role, _ in calls] == ["synthesis", "verifier"]
        assert synthesis.last_usage.model_calls == verifier.last_usage.model_calls == 1
    asyncio.run(run())


def test_invalid_author_has_no_fabricated_candidate_or_assessment(monkeypatch):
    async def run():
        service, spec, bundle, _, _, calls = _service(monkeypatch, synthesis_error="citation", persistent=True)
        result = await service.synthesize(None, spec, bundle)
        assert result.dossier is None and getattr(result, "review", None) is None
        assert [role for role, _ in calls] == ["synthesis", "synthesis"]
    asyncio.run(run())


def test_invalid_verifier_retains_candidate_with_explicitly_unfinished_assessment(monkeypatch):
    async def run():
        service, spec, bundle, _, _, calls = _service(monkeypatch, verifier_error="missing", persistent=True)
        result = await service.synthesize(None, spec, bundle)
        assert result.dossier is None and result.review is not None
        assert result.review.draft.claims and result.review.support_assessments is None
        assert [role for role, _ in calls] == ["synthesis", "verifier", "verifier"]
        assert len(result.attempt_records) == 3
    asyncio.run(run())


def test_successful_dossier_does_not_carry_rejected_review(monkeypatch):
    async def run():
        service, spec, bundle, _, _, _ = _service(monkeypatch)
        result = await service.synthesize(None, spec, bundle)
        assert result.dossier is not None and result.review is None
    asyncio.run(run())


@pytest.mark.parametrize("invalid", ["missing", "duplicate", "unknown"])
def test_review_cannot_misassociate_independent_assessment(monkeypatch, invalid):
    async def run():
        service, spec, bundle, _, _, _ = _service(monkeypatch, verifier_error="unsupported")
        result = await service.synthesize(None, spec, bundle)
        payload = result.review.model_dump(mode="json")
        if invalid == "missing":
            payload["support_assessments"] = []
        elif invalid == "duplicate":
            payload["support_assessments"] *= 2
        else:
            payload["support_assessments"][0]["claim_id"] = "0" * 64
        with pytest.raises(ValidationError, match="恰好覆盖"):
            ContextSynthesisReview.model_validate(payload)
    asyncio.run(run())


def test_compiler_persists_rejection_review_without_ready_cache(tmp_path, monkeypatch, runtime_postgres_database):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        seeded = await _seed_loop(sessions, tmp_path, label="synrev", started_at=datetime.now(UTC))
        try:
            service, spec, bundle, _, _, calls = _service(monkeypatch, verifier_error="unsupported")
            observation = _compiler_observation(seeded)
            readiness = await load_execution_readiness(sessions, observation, spec)
            inputs = (bundle.resolution_id, stable_expansion_hash("execution-readiness", readiness.model_dump(mode="json")))
            opportunity = ExpansionOpportunity.create(loop_id=seeded["loop_id"], round_id=seeded["round_id"],
                observation_hash="a" * 64, work_spec=spec, manifest_sources=bundle.source_frontier)
            compiler = ContextExpansionPlanCompiler(sessions, None, synthesizer=service)
            blocker = await compiler._synthesize_dossier_stage(observation, opportunity, bundle, [],
                artifact_expansion_id=None, persist_artifacts=True)
            assert blocker.code == "synthesis_invalid", blocker.summary
            async with sessions() as session:
                artifact = await SemanticDerivationArtifactRepository().stage_artifact(session,
                    loop_id=opportunity.loop_id, round_id=opportunity.round_id, stage="dossier_synthesis",
                    input_identities=inputs, version=service.VERSION)
                assert artifact is not None and artifact.outcome == "blocked"
                review = artifact.payload.get("review")
                assert review is not None, "durable rejection retained only its summary"
                assert review["draft"]["claims"][0]["claim_id"] == review["support_assessments"][0]["claim_id"]
                assert review["support_assessments"][0]["verdict"] == "unsupported"
                assert len(artifact.attempt_records) == len(calls) == 2
            assert await compiler._artifact_payload(opportunity, "dossier_synthesis", inputs, service.VERSION) is None
            assert review["draft"]["claims"][0]["statement"] not in blocker.summary
        finally:
            await _stop(seeded["service"], seeded["loop_id"])
            await engine.dispose()
    asyncio.run(run())
