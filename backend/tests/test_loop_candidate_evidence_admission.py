"""本文件对外提供原生 Curator 证据角色错误的真实服务回归。

输入为隔离 PostgreSQL、问候后的真实 Mission 冻结和脚本化无权模型提案；输出为准入拒绝、持久审计、真实缺失阻断和唯一 Patrol 交付断言。
具体工作流为生产 Observation 服务冻结输入，经 ContextExpansionStage 或完整 Round 编排，再查询正式实体；不把 Mission 当作已通过测试。
示例：python -m pytest backend/tests/test_loop_candidate_evidence_admission.py；模型端口受控，数据库、准入、Patrol 和 Kernel 使用生产实现。
"""

import asyncio
from contextlib import asynccontextmanager
import os

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.context_expansion.contracts import CandidateEvidenceIdentity, CognitivePlanResult, ContextSemanticManifest, EvidenceRequirement, WorkContextSpec, stable_expansion_hash
from backend.app.desktop.agent_loop.context_expansion.coordinator import ContextExpansionStage
from backend.app.desktop.agent_loop.context_expansion.models import LoopContextExpansion, LoopContextDerivationArtifact
from backend.app.desktop.agent_loop.context_expansion.mission_sections import FrozenMissionSectionCatalog
from backend.app.desktop.agent_loop.mission_projection import EffectiveMissionProjector
from backend.app.desktop.agent_loop.models import LoopDirective, LoopDecision, LoopPatrolAttempt, AgentLoop
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.agent_loop.kernel import LoopKernel
from backend.app.desktop.agent_loop.round_orchestration import LoopRoundOrchestrator
from backend.tests.config_helpers import app_config_for
from backend.tests.test_loop_mission_bootstrap import _seed_greeting_loop, _FrozenCheckpointer, _script_first_patrol
from backend.app.desktop.context_evolution import ContextRevisionRef

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


@asynccontextmanager
async def _loop(tmp_path):
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    fixture = None
    try:
        fixture = await _seed_greeting_loop(sessions, tmp_path)
        yield sessions, fixture
    finally:
        if fixture:
            await fixture["service"].control(fixture["loop_id"], "stop")
        await engine.dispose()


class _Proposal:
    VERSION = "native-role-regression-v1"

    def __init__(self, role, *, missing=False):
        self.role, self.missing = role, missing

    async def plan(self, observation, signals, manifests):
        catalog = FrozenMissionSectionCatalog.from_mission(observation.loop_id,
            EffectiveMissionProjector.from_observation(observation))
        entry = next(item for item in catalog.entries if item.ref.item_id == "build")
        index_id, entry_id = catalog.catalog_id, stable_expansion_hash("entry", entry.ref.model_dump(mode="json"))
        candidate = CandidateEvidenceIdentity(candidate_id=stable_expansion_hash("retrieval-candidate-v2",
                index_id, "mission_section", entry_id, entry.ref.content_hash), index_id=index_id,
            entry_id=entry_id, entry_kind="mission_section", source_type="mission", mission_ref=entry.ref,
            source_content_hash=entry.ref.content_hash)
        spec = WorkContextSpec.create(planner_version=self.VERSION,
            objective="Produce the requested test and implementation artifacts.",
            separation_reason="Keep the verification work independently reviewable.",
            questions=("Which checks must the future implementation pass?",),
            completion_criteria=("Produce actual implementation files and successful test command evidence.",),
            workspace_requirement="read_only", evidence_requirements=(EvidenceRequirement(
                requirement_id="failure-regression-evidence" if self.missing else "mission-build-input",
                role=self.role, question="What frozen input supports this work?",
                coverage_criterion="Use the exact frozen evidence role.",
                candidate_refs=() if self.missing else (candidate,)),))
        return CognitivePlanResult(work_specs=(spec,), required_work_spec_ids=(spec.work_spec_id,))


class _GreetingManifest:
    VERSION = "greeting-frozen-manifest-v1"

    def project(self, observation):
        return tuple(ContextSemanticManifest.create(source=ContextRevisionRef.model_validate(item["revision"]),
            source_content_hash=item["content_hash"], projector_version=self.VERSION,
            role="primary", active_objective="Initial greeting", units=()) for item in observation.portfolio_frontier)


@pytest.mark.parametrize("role", ["failure", "test", "implementation", "workspace_effect", "decision"])
def test_mission_cannot_become_completed_facts_and_rejection_is_durable(tmp_path, role):
    async def run():
        async with _loop(tmp_path) as (sessions, fixture):
            observation = await LoopObservationService(sessions, _FrozenCheckpointer()).capture(
                fixture["loop_id"], fixture["snapshot"]["current_round_id"])
            stage = ContextExpansionStage(sessions, _FrozenCheckpointer())
            stage._coordinator._planner = _Proposal(role)
            stage._coordinator._projector = _GreetingManifest()
            assessment = await stage.assess(observation)
            assert assessment.level == "not_applicable" and not assessment.opportunities
            assert assessment.rejected_candidates, assessment.model_dump(mode="json")
            rejected, = assessment.rejected_candidates
            assert rejected.code == "invalid_candidate"
            assert rejected.requested_role == role and rejected.actual_roles == ("requirement",)
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(LoopContextExpansion).where(
                    LoopContextExpansion.loop_id == fixture["loop_id"])) == 0
                artifact = await session.scalar(select(LoopContextDerivationArtifact).where(
                    LoopContextDerivationArtifact.loop_id == fixture["loop_id"],
                    LoopContextDerivationArtifact.stage == "candidate_admission"))
                assert artifact.payload["rejected_candidates"] == [rejected.model_dump(mode="json")]
            assert (await stage.assess(observation)).rejected_candidates == assessment.rejected_candidates
    asyncio.run(run())


def test_requirement_is_valid_input_but_real_missing_failure_remains_blocked(tmp_path):
    async def run():
        async with _loop(tmp_path) as (sessions, fixture):
            observation = await LoopObservationService(sessions, _FrozenCheckpointer()).capture(
                fixture["loop_id"], fixture["snapshot"]["current_round_id"])
            valid = ContextExpansionStage(sessions, _FrozenCheckpointer())
            valid._coordinator._planner = _Proposal("requirement")
            review = await valid._coordinator._candidate_admission.review(observation,
                await valid._coordinator._planner.plan(observation, None, ()), _GreetingManifest().project(observation))
            assert review.blocker is None and len(review.planned.work_specs) == 1
            assert review.planned.required_work_spec_ids and not review.rejected
            missing = _Proposal("failure", missing=True)
            review = await valid._coordinator._candidate_admission.review(observation,
                await missing.plan(observation, None, ()), _GreetingManifest().project(observation))
            assert review.blocker.code == "required_evidence_unresolved" and not review.rejected
    asyncio.run(run())


def test_invalid_candidate_does_not_replace_patrol_or_block_first_mission(tmp_path):
    async def run():
        async with _loop(tmp_path) as (sessions, fixture):
            coordinator = LoopCoordinator(sessions)
            claim = await coordinator.claim_for_loop(fixture["loop_id"], "candidate-regression")
            orchestrator = LoopRoundOrchestrator(sessions, app_config_for("patrol-test", None),
                LoopKernel(sessions), _FrozenCheckpointer())
            _script_first_patrol(orchestrator, sessions, fixture)
            orchestrator._expansions._coordinator._planner = _Proposal("test")
            orchestrator._expansions._coordinator._projector = _GreetingManifest()
            model = orchestrator._decision_model(None)
            original = model.__call__

            class Model:
                usage = model.usage

                async def __call__(self, observation):
                    assert observation.expansion_assessment["rejected_candidates"][0]["code"] == "invalid_candidate"
                    assert not observation.expansion_assessment["opportunities"]
                    return await original(observation)

            orchestrator._decision_model = lambda _: Model()
            result = await orchestrator.process(claim)
            assert result is not None and result.status == "committed"
            await coordinator.release(claim)
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                assert loop.status == "running"
                assert await session.scalar(select(func.count()).select_from(LoopDecision).where(
                    LoopDecision.round_id == claim.round_id)) == 1
                assert await session.scalar(select(func.count()).select_from(LoopPatrolAttempt).where(
                    LoopPatrolAttempt.round_id == claim.round_id)) == 1
                directive = await session.scalar(select(LoopDirective).where(LoopDirective.round_id == claim.round_id))
                assert directive.origin_kind == "patrol"
                assert fixture["mission"].outcome in directive.content
                assert "[build]" in directive.content and "[vault]" in directive.content
    asyncio.run(run())
