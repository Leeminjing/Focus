"""本文件对外提供材料修订的真实 PostgreSQL 并发、恢复、计数和等待回归。

输入为独立测试库、真实 Loop/Grant/Lease、受控材料与独立 verdict；输出为唯一领取、新材料、原缓存和 stale-owner 拒绝断言。
具体工作流在现有测试库迁移合同下执行 reservation/result，再更换 owner 模拟进程恢复，不连接用户 Vault。
示例：python -m pytest backend/tests/test_context_quality_revision.py -q。
"""

import asyncio
from datetime import UTC, datetime, timedelta
import os
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.context_expansion.contracts import ExpansionOpportunity
from backend.app.desktop.agent_loop.context_expansion.quality import ContextQualityVerifier
from backend.app.desktop.agent_loop.context_expansion.quality_revision import QualityRevisionBlocked, QualityRevisionRepository, QualityRevisionCoordinator, admit_manual_revision
from backend.app.desktop.agent_loop.context_expansion.start_readiness import ExecutionReadiness, QuestionDisposition, work_question_id
from backend.app.desktop.agent_loop.context_expansion.synthesis import ContextSynthesisDraft, ContextSynthesisValidator, ContextSynthesisResult
from backend.app.desktop.agent_loop.context_expansion.synthesis_service import DeterministicTestContextSynthesisService
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant, LoopCoordinatorFence, LoopCoordinatorLease
from backend.app.desktop.agent_loop.wait_requests import LoopWaitRequestFactory
from backend.app.desktop.agent_loop.rounds import create_observation_round
from backend.app.desktop.agent_loop.models import LoopRound
from backend.tests.test_synthesis_claim_feedback import _inputs
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop


def _assessment(spec, bundle, dossier, number):
    return ContextQualityVerifier().verify(spec, bundle, dossier, {"dimensions": [
        {"dimension": d, "verdict": "fail" if d == "minimality" else "pass", "reasons": [f"Material needs revision {number}"]}
        for d in ("minimality", "sufficiency", "coherence")]}).assessment


def test_quality_revision_is_exclusive_recoverable_and_bounded(tmp_path, runtime_postgres_database):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        seeded = await _seed_loop(sessions, tmp_path, label="qrevision", started_at=datetime.now(UTC))
        try:
            async with sessions.begin() as session:
                grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == seeded["loop_id"]))
                grant.capabilities = [*grant.capabilities, "create_lane"]
                session.add(LoopCoordinatorFence(round_id=seeded["round_id"], fencing_token=1))
                session.add(LoopCoordinatorLease(lease_id=uuid.uuid4().hex, round_id=seeded["round_id"],
                    owner_id="revision-test", fencing_token="1", expires_at=datetime.now(UTC) + timedelta(minutes=5)))
            observation = SimpleNamespace(loop_id=seeded["loop_id"], round_id=seeded["round_id"],
                goal_revision=1, authority_revision=1, observed_frontier_hash="a" * 64)
            spec, bundle = _inputs()
            dossier = (await DeterministicTestContextSynthesisService().synthesize(None, spec, bundle)).dossier
            opportunity = ExpansionOpportunity.create(loop_id=seeded["loop_id"], round_id=seeded["round_id"],
                observation_hash="a" * 64, work_spec=spec, manifest_sources=bundle.source_frontier)
            assessment = _assessment(spec, bundle, dossier, 1)
            repo = QualityRevisionRepository(sessions)
            attempts = await asyncio.gather(repo.reserve(observation, opportunity, dossier, assessment),
                repo.reserve(observation, opportunity, dossier, assessment), return_exceptions=True)
            assert sum(isinstance(r, QualityRevisionBlocked) for r in attempts) == 1
            reservation = next(r[0] for r in attempts if isinstance(r, tuple))
            assert reservation.ordinal == 1 and await repo.count(observation, spec) == 1
            async with sessions.begin() as session:
                (await session.get(LoopCoordinatorFence, seeded["round_id"])).fencing_token = 2
                (await session.scalar(select(LoopCoordinatorLease).where(LoopCoordinatorLease.round_id == seeded["round_id"]))).fencing_token = "2"
            with pytest.raises(QualityRevisionBlocked, match="调用方 owner"):
                await QualityRevisionRepository(sessions, expected_token=1).reserve(observation, opportunity, dossier, assessment)
            recovered, cached = await QualityRevisionRepository(sessions).reserve(observation, opportunity, dossier, assessment)
            assert cached is None and recovered.ordinal == 1
            payload = {"dossier": dossier.model_dump(mode="json"), "parent_dossier_id": dossier.dossier_id}
            with pytest.raises(QualityRevisionBlocked):
                await repo.complete(observation, reservation, payload)
            await repo.complete(observation, recovered, payload)
            again, cached = await repo.reserve(observation, opportunity, dossier, assessment)
            assert again.ordinal == 1 and cached == payload
            second, _ = await repo.reserve(observation, opportunity, dossier, _assessment(spec, bundle, dossier, 2))
            assert second.ordinal == 2
            with pytest.raises(QualityRevisionBlocked, match="两次"):
                await repo.reserve(observation, opportunity, dossier, _assessment(spec, bundle, dossier, 3))
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, observation.loop_id, with_for_update=True)
                old_round = await session.get(LoopRound, observation.round_id)
                next_round = await create_observation_round(session, loop, old_round, "a" * 64)
                session.add(next_round)
                await session.flush()
                loop.current_round_id = next_round.round_id
                session.add(LoopCoordinatorFence(round_id=next_round.round_id, fencing_token=1))
                session.add(LoopCoordinatorLease(lease_id=uuid.uuid4().hex, round_id=next_round.round_id,
                    owner_id="revision-next-round", fencing_token="1", expires_at=datetime.now(UTC) + timedelta(minutes=5)))
                next_id = next_round.round_id
            next_observation = SimpleNamespace(**{**vars(observation), "round_id": next_id})
            next_opportunity = opportunity.model_copy(update={"round_id": next_id})
            with pytest.raises(QualityRevisionBlocked, match="两次"):
                await QualityRevisionRepository(sessions).reserve(next_observation, next_opportunity, dossier,
                    _assessment(spec, bundle, dossier, 3))
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, observation.loop_id, with_for_update=True)
                with pytest.raises(ValueError, match="次数"):
                    await admit_manual_revision(session, loop, {"lineage": repo.lineage(spec, 1), "goal_revision": 1})
            changed = SimpleNamespace(**{**vars(observation), "authority_revision": 2})
            with pytest.raises(QualityRevisionBlocked):
                await repo.reserve(changed, opportunity, dossier, assessment)
        finally:
            await _stop(seeded["service"], seeded["loop_id"])
            await engine.dispose()
    asyncio.run(run())


def test_revision_changes_material_and_never_relabels_original_verdict(monkeypatch):
    async def run():
        spec, bundle = _inputs()
        original = (await DeterministicTestContextSynthesisService().synthesize(None, spec, bundle)).dossier
        scope = ExecutionReadiness(workspace_id="test", goal_revision=1, authority_revision=1,
            workspace_mode="read_only", capabilities=("read",))
        dossier = original.model_copy(update={"execution_readiness": scope})
        assessment = _assessment(spec, bundle, dossier, 1)
        calls, usage = [], []
        class Repository:
            async def reserve(self, *args):
                return SimpleNamespace(ordinal=1, lineage="test"), None
            async def complete(self, *args):
                pass
        class Synthesis:
            async def synthesize(self, observation, work, evidence, *, execution_readiness, feedback):
                calls.append(feedback)
                draft = ContextSynthesisDraft(sections=original.sections, claims=original.claims,
                    unresolved_questions=spec.questions, question_dispositions=(QuestionDisposition(
                        question_id=work_question_id(spec.questions[0]), disposition="execution_research",
                        investigation="Read the actual source and investigate remaining implementation choices", required_capabilities=("read",)),))
                return ContextSynthesisResult(dossier=ContextSynthesisValidator().validate(spec, bundle, draft,
                    original.support_assessments, synthesizer_version="revised-fixture", execution_readiness=scope))
        async def record(loop_id, attempts):
            usage.append(attempts)
        coordinator = QualityRevisionCoordinator(None, Synthesis(), record)
        coordinator._repository = Repository()
        candidate, number = await coordinator.revise(SimpleNamespace(), SimpleNamespace(loop_id="test", work_spec=spec),
            bundle, dossier, assessment)
        assert number == 1 and candidate.dossier_id != dossier.dossier_id
        assert len(calls) == 1 and usage == [()]
        assert not assessment.passes and calls[0]["parent_assessment_id"] == assessment.assessment_id
    asyncio.run(run())


def test_quality_wait_actions_keep_legacy_retry_and_limit_new_revision():
    legacy = LoopWaitRequestFactory.retry_or_stop("error")
    assert [a["action"] for a in legacy.response_contract["actions"]] == ["retry", "stop"]
    quality = LoopWaitRequestFactory.retry_or_stop("quality", {"quality_recovery": {"revision_count": 1}})
    assert [a["action"] for a in quality.response_contract["actions"]] == ["retry", "revise_material", "stop"]
    exhausted = LoopWaitRequestFactory.retry_or_stop("quality", {"quality_recovery": {"revision_count": 2}})
    assert [a["action"] for a in exhausted.response_contract["actions"]] == ["retry", "stop"]
