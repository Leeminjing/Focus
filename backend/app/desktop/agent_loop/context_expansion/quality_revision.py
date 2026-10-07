"""本文件对外提供 QualityRevisionRepository 与 QualityRevisionCoordinator。

输入为冻结观察、工作谱系、失败评估和已有 synthesis/quality 端口；输出为新候选或保留原判定的阻断。
具体工作流为 Loop 行锁下预留最多两次修订，阶段产物保存父子身份，模型调用在事务外执行；恢复复用已完成材料，旧 owner 不可提交。
反馈只改变材料和问题调查计划，独立质量门仍负责判定；公开诊断只使用固定原因分类，不发布私有评语。
示例：candidate = await coordinator.revise(observation, opportunity, bundle, dossier, assessment)。
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, func

from backend.app.desktop.agent_loop.context_expansion.artifact_repository import SemanticDerivationArtifactRepository
from backend.app.desktop.agent_loop.context_expansion.contracts import stable_expansion_hash
from backend.app.desktop.agent_loop.context_expansion.models import LoopContextDerivationArtifact
from backend.app.desktop.agent_loop.context_expansion.synthesis import ValidatedContextDossier
from backend.app.desktop.agent_loop.models import AgentLoop, LoopCoordinatorFence, LoopDelegationGrant
from backend.app.desktop.agent_loop.ownership import LoopFencingGuard, KernelFencingRejected
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft


class QualityRevisionBlocked(ValueError):
    pass


@dataclass(frozen=True)
class RevisionReservation:
    artifact_id: str
    round_id: str
    lineage: str
    ordinal: int
    token: int


class QualityRevisionRepository:
    VERSION = "context-quality-revision-v1"
    LIMIT = 2

    def __init__(self, sessions, expected_token=None):
        self._sessions = sessions
        self._expected_token = expected_token
        self._artifacts = SemanticDerivationArtifactRepository()

    @staticmethod
    def lineage(work, goal_revision):
        checks = tuple(sorted({ref.mission_ref.item_id for requirement in work.evidence_requirements
            for ref in requirement.candidate_refs if ref.mission_ref and ref.mission_ref.section_kind == "completion_check"}))
        return stable_expansion_hash("quality-revision-lineage", goal_revision,
            checks or (tuple(sorted(r.requirement_id for r in work.evidence_requirements)),
                       tuple(sorted(" ".join(q.split()).casefold() for q in work.questions))), work.workspace_requirement)

    async def reserve(self, observation, opportunity, dossier, assessment):
        lineage = self.lineage(opportunity.work_spec, observation.goal_revision)
        async with self._sessions.begin() as session:
            token = await self._validate_control(session, observation)
            rows = tuple(await session.scalars(select(LoopContextDerivationArtifact).where(
                LoopContextDerivationArtifact.loop_id == observation.loop_id,
                LoopContextDerivationArtifact.stage == "quality_revision_reservation",
                LoopContextDerivationArtifact.payload["lineage"].astext == lineage).order_by(LoopContextDerivationArtifact.created_at)))
            previous = next((r for r in rows if r.payload["parent_assessment_id"] == assessment.assessment_id), None)
            if previous:
                if (previous.payload["source_frontier_hash"] != observation.observed_frontier_hash
                        or previous.payload["authority_revision"] != observation.authority_revision):
                    raise QualityRevisionBlocked("旧修订来源或授权已改变，须重新规划")
                result = await self._result(session, previous)
                if result is not None:
                    return self._reservation(previous, token), result.payload
                claims = tuple(await session.scalars(select(LoopContextDerivationArtifact).where(
                    LoopContextDerivationArtifact.loop_id == observation.loop_id,
                    LoopContextDerivationArtifact.stage == "quality_revision_claim",
                    LoopContextDerivationArtifact.payload["reservation_id"].astext == previous.artifact_id)))
                if any(r.payload["owner_round"] == observation.round_id and r.payload["token"] == token for r in claims):
                    raise QualityRevisionBlocked("修订已被当前 owner 领取，等待完成或恢复")
                row = previous
            else:
                if len(rows) >= self.LIMIT:
                    raise QualityRevisionBlocked("自主材料修订已达到两次上限，需要新的输入或用户决定")
                row = await self._artifacts.put_stage_artifact(session, loop_id=observation.loop_id,
                    round_id=observation.round_id, stage="quality_revision_reservation",
                    input_identities=(lineage, assessment.assessment_id), version=self.VERSION, outcome="reserved",
                    payload={"lineage": lineage, "ordinal": len(rows) + 1,
                        "parent_assessment_id": assessment.assessment_id, "parent_dossier_id": dossier.dossier_id,
                        "source_frontier_hash": observation.observed_frontier_hash,
                        "goal_revision": observation.goal_revision, "authority_revision": observation.authority_revision})
            if (row.payload["source_frontier_hash"] != observation.observed_frontier_hash
                    or row.payload["authority_revision"] != observation.authority_revision):
                raise QualityRevisionBlocked("旧修订来源或授权已改变，须重新规划")
            await self._artifacts.put_stage_artifact(session, loop_id=observation.loop_id, round_id=observation.round_id,
                stage="quality_revision_claim", input_identities=(row.artifact_id, observation.round_id, str(token)),
                version=self.VERSION, outcome="reserved",
                payload={"reservation_id": row.artifact_id, "owner_round": observation.round_id, "token": token})
            await self._event(session, observation.loop_id, row.artifact_id, 1, "started",
                {"revision_count": row.payload["ordinal"], "parent_assessment_id": assessment.assessment_id,
                 "parent_dossier_id": dossier.dossier_id, "lineage": lineage})
            return self._reservation(row, token), None

    async def capture_owner(self, observation):
        async with self._sessions.begin() as session:
            return await self._validate_control(session, observation)

    async def complete(self, observation, reservation, result):
        async with self._sessions.begin() as session:
            token = await self._validate_control(session, observation)
            if token != reservation.token:
                raise QualityRevisionBlocked("修订 owner 已过期")
            row = await self._artifacts.put_stage_artifact(session, loop_id=observation.loop_id,
                round_id=reservation.round_id, stage="quality_revision_result",
                input_identities=(reservation.artifact_id,), version=self.VERSION, outcome="ready", payload=result)
            await self._event(session, observation.loop_id, reservation.artifact_id, 2, "completed",
                {"revision_count": reservation.ordinal, "parent_dossier_id": result["parent_dossier_id"],
                 "dossier_id": (result.get("dossier") or {}).get("dossier_id"), "material_available": bool(result.get("dossier")),
                 "quality_passed": False})
            return row

    @staticmethod
    async def _event(session, loop_id, identity, revision, state, payload):
        await LoopEventJournal().append(session, loop_id, CanonicalEventDraft(
            kind=f"context_expansion.quality_revision_{state}", entity_type="context_quality_revision",
            entity_id=identity, entity_revision=revision, correlation_id=identity,
            payload=payload, idempotency_key=f"quality-revision:{identity}:{state}"))

    async def count(self, observation, work):
        async with self._sessions() as session:
            return int(await session.scalar(select(func.count()).select_from(LoopContextDerivationArtifact).where(
                LoopContextDerivationArtifact.loop_id == observation.loop_id,
                LoopContextDerivationArtifact.stage == "quality_revision_reservation",
                LoopContextDerivationArtifact.payload["lineage"].astext == self.lineage(work, observation.goal_revision))) or 0)

    async def _validate_control(self, session, observation):
        loop = await session.get(AgentLoop, observation.loop_id, with_for_update=True, populate_existing=True)
        if (loop is None or loop.status != "running" or loop.current_round_id != observation.round_id
                or loop.goal_revision != observation.goal_revision or loop.authority_revision != observation.authority_revision):
            raise QualityRevisionBlocked("修订的 Loop 来源或控制版本已失效")
        grant = await session.scalar(select(LoopDelegationGrant).where(
            LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.revision == loop.authority_revision,
            LoopDelegationGrant.status == "active"))
        if grant is None or "create_lane" not in grant.capabilities:
            raise QualityRevisionBlocked("当前授权不允许派生工作材料")
        if grant.expires_at is not None and grant.expires_at <= datetime.now(UTC):
            raise QualityRevisionBlocked("材料修订授权已到期")
        fence = await session.get(LoopCoordinatorFence, observation.round_id)
        token = fence.fencing_token if fence else 0
        try:
            await LoopFencingGuard().validate_active(session, observation.round_id, token)
        except KernelFencingRejected as exc:
            raise QualityRevisionBlocked(str(exc)) from exc
        if self._expected_token is not None and token != self._expected_token:
            raise QualityRevisionBlocked("修订调用方 owner 已失效")
        return token

    async def _result(self, session, reservation):
        return await self._artifacts.stage_artifact(session, loop_id=reservation.loop_id,
            round_id=reservation.round_id, stage="quality_revision_result", input_identities=(reservation.artifact_id,),
            version=self.VERSION)

    @staticmethod
    def _reservation(row, token):
        return RevisionReservation(row.artifact_id, row.round_id, row.payload["lineage"], row.payload["ordinal"], token)


class QualityRevisionCoordinator:
    VERSION = QualityRevisionRepository.VERSION
    def __init__(self, sessions, synthesizer, record_usage, *, expected_token=None):
        self._repository = QualityRevisionRepository(sessions, expected_token)
        self._synthesizer = synthesizer
        self._record_usage = record_usage

    async def count(self, observation, work):
        return await self._repository.count(observation, work)

    async def revise(self, observation, opportunity, bundle, dossier, assessment):
        failed = tuple(d for d in assessment.dimensions if d.verdict != "pass")
        if any(d.dimension == "coherence" for d in failed):
            raise QualityRevisionBlocked("来源一致性未通过，需要澄清来源，不能自动丢弃冲突")
        scope = dossier.execution_readiness
        if scope is None or not scope.capabilities:
            raise QualityRevisionBlocked("缺少实际执行条件，无法自主修订调查路径")
        reservation, saved = await self._repository.reserve(observation, opportunity, dossier, assessment)
        if saved is None:
            result = await self._synthesizer.synthesize(observation, opportunity.work_spec, bundle,
                execution_readiness=scope, feedback={"parent_dossier": dossier.model_dump(mode="json"),
                    "parent_assessment_id": assessment.assessment_id,
                    "dimensions": [d.model_dump(mode="json") for d in failed]})
            await self._record_usage(opportunity.loop_id, result.attempt_records)
            saved = {"ordinal": reservation.ordinal, "lineage": reservation.lineage,
                "parent_assessment_id": assessment.assessment_id, "parent_dossier_id": dossier.dossier_id,
                "dossier": result.dossier.model_dump(mode="json") if result.dossier else None,
                "rejected_reviews": [review.model_dump(mode="json") for review in result.rejected_reviews],
                "blocker_code": result.blocker_code}
            await self._repository.complete(observation, reservation, saved)
        if saved["dossier"] is None:
            raise QualityRevisionBlocked("材料修订未生成合法候选，等待补充输入")
        candidate = ValidatedContextDossier.model_validate(saved["dossier"])
        if self._semantic_content(candidate) == self._semantic_content(dossier):
            raise QualityRevisionBlocked("材料未发生实质变化，保留原质量判定")
        return candidate, reservation.ordinal

    @staticmethod
    def _semantic_content(dossier):
        return (dossier.claims, dossier.unresolved_questions, dossier.question_dispositions)


async def admit_manual_revision(session, loop, diagnostics):
    if not diagnostics or not diagnostics.get("lineage"):
        raise ValueError("恢复请求缺少质量修订来源")
    if diagnostics.get("goal_revision") != loop.goal_revision:
        raise ValueError("恢复请求的 Mission 已过期")
    grant = await session.scalar(select(LoopDelegationGrant).where(
        LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.revision == loop.authority_revision,
        LoopDelegationGrant.status == "active"))
    if grant is None or "create_lane" not in grant.capabilities:
        raise ValueError("当前授权不允许修订派生材料")
    if grant.expires_at is not None and grant.expires_at <= datetime.now(UTC):
        raise ValueError("材料修订授权已到期")
    count = await session.scalar(select(func.count()).select_from(LoopContextDerivationArtifact).where(
        LoopContextDerivationArtifact.loop_id == loop.loop_id,
        LoopContextDerivationArtifact.stage == "quality_revision_reservation",
        LoopContextDerivationArtifact.payload["lineage"].astext == diagnostics["lineage"]))
    if int(count or 0) >= QualityRevisionRepository.LIMIT:
        raise ValueError("材料修订次数已耗尽，请补充输入或改变目标后重新规划")


def quality_recovery_diagnostics(assessment, work, observation, count):
    reasons = {"minimality": "材料包含与当前工作不相关或重复的声明",
        "sufficiency": "开始条件不足：需要补充来源或明确实际可执行的调查路径",
        "coherence": "来源存在矛盾或需要澄清"}
    failed = [d.dimension for d in assessment.dimensions if d.verdict != "pass"]
    return {"work_spec_id": work.work_spec_id, "assessment_id": assessment.assessment_id,
        "dossier_id": assessment.dossier_id, "goal_revision": observation.goal_revision,
        "lineage": QualityRevisionRepository.lineage(work, observation.goal_revision),
        "failed_dimensions": failed, "revision_count": count,
        "reasons": [{"dimension": d, "reason": reasons[d]} for d in failed]}
