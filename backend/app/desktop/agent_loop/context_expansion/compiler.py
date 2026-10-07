r"""本文件对外提供 ExpansionPlanCompilerPort、ContextExpansionPlanCompiler 与 DeterministicExpansionPlanCompiler。

输入为冻结 observation、identity-only SpawnContextIntent、WorkContext opportunity、resolved multi-source evidence、validated claim dossier
与三维 quality assessment；输出为 CompiledExpansion 或阶段专属 ExpansionBlocker。具体工作流为 production façade 重建 manifests、
读取授权 corpus、按冻结资源策略解析 required evidence 并记录证据账本、调用受监督 synthesis 与独立 quality gate，再由纯 compiler 只接受 identity 匹配且三维 pass
的 package，按 Work Contract、Ledger、Dossier、Primary Evidence 编译；不存在 extractive fallback 或 quality bypass。示例：
`compiled = await compiler.compile(observation, opportunity, intent)`。
生产 façade 以受保护的证据解析、dossier 合成和质量验证阶段编排恢复与阶段记录；质量模块重验恢复身份并重建原判定 blocker，不新增模型消费。
生产 façade 向 synthesis 与 quality gate 绑定所属 Loop/Round 的消费 receipt；已由 receipt 管理的调用不再重复记入聚合账本。
平台开始条件进入材料身份；合法质量失败交给独立耐久修订协调，最多两次改变材料，不重抽原判定；公开 blocker 只包含原因分类与身份。
失败 synthesis 的私有 blocked artifact 原样保留类型化候选 review，供冻结来源复查；公开 blocker/stage 摘要不携带候选或核验理由，blocked 仍不能作为 ready 缓存恢复。
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from focus.config.app_config import AppConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.context_expansion.artifact_repository import (
    SemanticDerivationArtifactRepository,
)
from backend.app.desktop.agent_loop.context_expansion.contracts import (
    CompiledExpansion,
    DerivationStageRecord,
    ExpansionBlocker,
    ExpansionOpportunity,
    ResolvedEvidenceBundle,
    SpawnContextIntent,
    stable_expansion_hash,
)
from backend.app.desktop.agent_loop.context_expansion.evidence_corpus import (
    EvidenceCorpusReadError,
    FrozenEvidenceCorpusReader,
)
from backend.app.desktop.agent_loop.context_expansion.evidence_resolver import (
    MultiSourceEvidenceResolver,
)
from backend.app.desktop.agent_loop.context_expansion.manifest_adapter import (
    CompositeSemanticManifestProjector,
)
from backend.app.desktop.agent_loop.context_expansion.quality import (
    ContextQualityAssessment,
    ContextQualityVerifier,
)
from backend.app.desktop.agent_loop.context_expansion.quality_verifier import (
    DeterministicTestContextQualityService,
    StructuredContextQualityService,
)
from backend.app.desktop.agent_loop.context_expansion.stage_telemetry import (
    DerivationStageTimer,
)
from backend.app.desktop.agent_loop.context_expansion.synthesis import (
    ContextSynthesisResult,
    ContextSynthesizerPort,
    ValidatedContextDossier,
    render_context_dossier,
)
from backend.app.desktop.agent_loop.context_expansion.synthesis_service import (
    DeterministicTestContextSynthesisService,
    StructuredContextSynthesisService,
)
from backend.app.desktop.agent_loop.derivation_worker import RoleBoundStructuredModel
from backend.app.desktop.agent_loop.expansion_resource_policy import resolve_expansion_resources
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.agent_loop.usage import LoopUsageDelta, LoopUsageLedger
from backend.app.desktop.context_curation import (
    ComposeMessage,
    CopyMessage,
    CreateLanePlan,
    MultiSourceEvidence,
    ToolExchange,
    ToolExchangeCall,
    compile_lane,
    evidence_ref_key,
)


class ExpansionPlanCompilerPort(Protocol):
    async def compile(
        self,
        observation: LoopObservationEnvelope,
        opportunity: ExpansionOpportunity,
        intent: SpawnContextIntent,
    ) -> CompiledExpansion | ExpansionBlocker: ...


class ContextQualityServicePort(Protocol):
    async def verify(
        self,
        work_spec,
        bundle,
        dossier,
    ): ...


class ContextExpansionPlanCompiler:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        checkpointer: Any,
        *,
        app_config: AppConfig | None = None,
        projector: CompositeSemanticManifestProjector | None = None,
        resolver: MultiSourceEvidenceResolver | None = None,
        synthesizer: ContextSynthesizerPort | None = None,
        quality_service: ContextQualityServicePort | None = None,
    ) -> None:
        self._projector = projector or CompositeSemanticManifestProjector()
        self._sessions = sessions
        self._artifacts = SemanticDerivationArtifactRepository()
        self._reader = FrozenEvidenceCorpusReader(sessions, checkpointer)
        self._resolver = resolver or MultiSourceEvidenceResolver()
        if synthesizer is not None:
            self._synthesizer = synthesizer
        elif app_config is not None:
            self._synthesizer = StructuredContextSynthesisService(
                RoleBoundStructuredModel(app_config, "dossier_synthesizer"),
                RoleBoundStructuredModel(app_config, "claim_verifier"),
            )
        else:
            self._synthesizer = DeterministicTestContextSynthesisService()
        if quality_service is not None:
            self._quality = quality_service
        elif app_config is not None:
            self._quality = StructuredContextQualityService(
                RoleBoundStructuredModel(app_config, "context_quality_verifier")
            )
        else:
            self._quality = DeterministicTestContextQualityService()
        self._compiler = DeterministicExpansionPlanCompiler()

    async def compile(
        self,
        observation: LoopObservationEnvelope,
        opportunity: ExpansionOpportunity,
        intent: SpawnContextIntent,
        *,
        artifact_expansion_id: str | None = None,
        persist_artifacts: bool = True,
    ) -> CompiledExpansion | ExpansionBlocker:
        if intent.opportunity_id != opportunity.opportunity_id:
            return self._blocked(opportunity, "stale_source", "semantic intent 与冻结 opportunity 不一致")
        from backend.app.desktop.agent_loop.model_usage_owner import OwnedModelUsage

        receipts = OwnedModelUsage(self._sessions, opportunity.loop_id, opportunity.round_id, "round", opportunity.round_id)
        for service in (self._synthesizer, self._quality):
            if hasattr(service, "bind_usage_receipts"):
                service.bind_usage_receipts(receipts)
        records: list[DerivationStageRecord] = []
        revision_owner = None
        if persist_artifacts and isinstance(self._synthesizer, StructuredContextSynthesisService):
            from backend.app.desktop.agent_loop.context_expansion.quality_revision import QualityRevisionRepository, QualityRevisionBlocked

            try:
                revision_owner = await QualityRevisionRepository(self._sessions).capture_owner(observation)
            except QualityRevisionBlocked as exc:
                return self._blocked(opportunity, "authority_missing", str(exc))
        resolved = await self._resolve_evidence_stage(
            observation, opportunity, records,
            artifact_expansion_id=artifact_expansion_id, persist_artifacts=persist_artifacts,
        )
        if isinstance(resolved, ExpansionBlocker):
            return resolved
        dossier = await self._synthesize_dossier_stage(
            observation, opportunity, resolved, records,
            artifact_expansion_id=artifact_expansion_id, persist_artifacts=persist_artifacts,
        )
        if isinstance(dossier, ExpansionBlocker):
            return dossier
        quality_assessment = await self._verify_quality_stage(
            opportunity, resolved, dossier, records,
            artifact_expansion_id=artifact_expansion_id, persist_artifacts=persist_artifacts,
        )
        if isinstance(quality_assessment, ExpansionBlocker):
            if (persist_artifacts and quality_assessment.code == "context_quality_failed"
                    and isinstance(self._synthesizer, StructuredContextSynthesisService)):
                dossier, quality_assessment = await self._revise_quality_stage(
                    observation, opportunity, resolved, dossier, quality_assessment, records,
                    artifact_expansion_id=artifact_expansion_id, revision_owner=revision_owner)
            if isinstance(quality_assessment, ExpansionBlocker):
                return quality_assessment
        compilation_timer = DerivationStageTimer(
            "compilation",
            (
                resolved.resolution_id,
                dossier.dossier_id,
                quality_assessment.assessment_id,
            ),
            self._compiler.VERSION,
        )
        compiled = self._compiler.compile(
            opportunity,
            intent,
            resolved,
            dossier=dossier,
            quality_assessment=quality_assessment,
            stage_records=tuple(records),
        )
        if isinstance(compiled, ExpansionBlocker):
            record = compilation_timer.finish((), compiled.summary, failure_code=compiled.code)
            return compiled.model_copy(update={"stage_records": (*compiled.stage_records, record)})
        record = compilation_timer.finish((compiled.definition_hash,), "已编译确定性 LanePlan")
        return compiled.model_copy(update={"stage_records": (*compiled.stage_records, record)})

    async def _resolve_evidence_stage(
        self,
        observation: LoopObservationEnvelope,
        opportunity: ExpansionOpportunity,
        records: list[DerivationStageRecord],
        *,
        artifact_expansion_id: str | None,
        persist_artifacts: bool,
    ) -> ResolvedEvidenceBundle | ExpansionBlocker:
        resolution_timer = DerivationStageTimer(
            "evidence_resolution",
            (opportunity.work_spec.work_spec_id, *opportunity.manifest_ids),
            self._resolver.VERSION,
        )
        resolution_inputs = (opportunity.work_spec.work_spec_id, *opportunity.manifest_ids)
        recovered_resolution = await self._artifact_payload(
            opportunity,
            "evidence_resolution",
            resolution_inputs,
            self._resolver.VERSION,
        ) if persist_artifacts else None
        if recovered_resolution is not None:
            resolved = ResolvedEvidenceBundle.model_validate(recovered_resolution)
        else:
            try:
                manifests = self._projector.project(observation)
                corpus = await self._reader.read(observation, opportunity, manifests)
            except EvidenceCorpusReadError as exc:
                records.append(resolution_timer.finish((), exc.summary, failure_code=exc.code))
                return self._blocked(opportunity, exc.code, exc.summary, tuple(records))
            except (TypeError, ValueError) as exc:
                records.append(
                    resolution_timer.finish(
                        (),
                        "Evidence corpus projection 失败",
                        failure_code="portfolio_projection_failed",
                    )
                )
                return self._blocked(opportunity, "portfolio_projection_failed", str(exc), tuple(records))
            resources = resolve_expansion_resources(
                dict(observation.budget.get("limits") or {}),
                observation.authority_revision,
                dict(observation.budget.get("usage") or {}),
            )
            resolved = self._resolver.resolve(
                opportunity,
                manifests,
                corpus,
                max_items=resources.policy.max_compiled_evidence_items,
            )
            if isinstance(resolved, ExpansionBlocker):
                record = resolution_timer.finish((), resolved.summary, failure_code=resolved.code)
                return resolved.model_copy(update={"stage_records": (*resolved.stage_records, record)})
            if persist_artifacts:
                await self._save_artifact(
                    opportunity,
                    "evidence_resolution",
                    resolution_inputs,
                    self._resolver.VERSION,
                    resolved.model_dump(mode="json"),
                    expansion_id=artifact_expansion_id,
                )
        records.append(
            resolution_timer.finish(
                (resolved.resolution_id,),
                f"已解析 {len(resolved.items)} 项 requirement evidence",
            )
        )
        return resolved

    async def _synthesize_dossier_stage(
        self,
        observation: LoopObservationEnvelope,
        opportunity: ExpansionOpportunity,
        resolved: ResolvedEvidenceBundle,
        records: list[DerivationStageRecord],
        *,
        artifact_expansion_id: str | None,
        persist_artifacts: bool,
    ) -> ValidatedContextDossier | ExpansionBlocker:
        dossier_timer = DerivationStageTimer(
            "dossier_synthesis",
            (resolved.resolution_id,),
            type(self._synthesizer).__name__,
        )
        dossier_inputs = (resolved.resolution_id,)
        readiness = None
        if isinstance(self._synthesizer, StructuredContextSynthesisService):
            from backend.app.desktop.agent_loop.context_expansion.start_readiness import load_execution_readiness
            try:
                readiness = await load_execution_readiness(self._sessions, observation, opportunity.work_spec)
            except ValueError as exc:
                return self._blocked(opportunity, "stale_source", str(exc), tuple(records))
            dossier_inputs += (stable_expansion_hash("execution-readiness", readiness.model_dump(mode="json")),)
        synthesizer_version = str(getattr(self._synthesizer, "VERSION", type(self._synthesizer).__name__))
        recovered_dossier = await self._artifact_payload(
            opportunity,
            "dossier_synthesis",
            dossier_inputs,
            synthesizer_version,
        ) if persist_artifacts else None
        if recovered_dossier is not None:
            dossier = ValidatedContextDossier.model_validate(recovered_dossier)
            dossier_result = ContextSynthesisResult(dossier=dossier)
        else:
            dossier_result = await self._synthesizer.synthesize(
                observation,
                opportunity.work_spec,
                resolved,
                **({"execution_readiness": readiness} if readiness else {}),
            )
            await self._record_attempt_usage(opportunity.loop_id, dossier_result.attempt_records)
            if dossier_result.dossier is not None and persist_artifacts:
                await self._save_artifact(
                    opportunity,
                    "dossier_synthesis",
                    dossier_inputs,
                    synthesizer_version,
                    dossier_result.dossier.model_dump(mode="json"),
                    expansion_id=artifact_expansion_id,
                    attempt_records=dossier_result.attempt_records,
                )
            elif dossier_result.dossier is None and persist_artifacts:
                await self._save_artifact(
                    opportunity,
                    "dossier_synthesis",
                    dossier_inputs,
                    synthesizer_version,
                    {
                        "blocker_code": dossier_result.blocker_code,
                        "blocker_summary": dossier_result.blocker_summary,
                        "review": dossier_result.review.model_dump(mode="json") if dossier_result.review else None,
                    },
                    outcome="blocked",
                    expansion_id=artifact_expansion_id,
                    attempt_records=dossier_result.attempt_records,
                )
        records.append(
            dossier_timer.finish(
                (dossier_result.dossier.dossier_id,) if dossier_result.dossier else (),
                dossier_result.blocker_summary or "已构建 claim-level evidence-grounded dossier",
                failure_code=dossier_result.blocker_code,
            )
        )
        if dossier_result.dossier is None:
            return self._blocked(
                opportunity,
                dossier_result.blocker_code or "synthesis_invalid",
                dossier_result.blocker_summary or "dossier synthesis 未产生有效结果",
                tuple(records),
            )
        return dossier_result.dossier

    async def _revise_quality_stage(self, observation, opportunity, resolved, dossier, blocker, records, *, artifact_expansion_id, revision_owner=None):
        from backend.app.desktop.agent_loop.context_expansion.quality_revision import QualityRevisionBlocked, QualityRevisionCoordinator, quality_recovery_diagnostics

        coordinator = QualityRevisionCoordinator(self._sessions, self._synthesizer, self._record_attempt_usage, expected_token=revision_owner)
        count = await coordinator.count(observation, opportunity.work_spec)
        diagnostics = None
        for _ in range(2):
            assessment = await self._saved_quality_assessment(opportunity, resolved, dossier)
            if assessment is None:
                break
            diagnostics = quality_recovery_diagnostics(assessment, opportunity.work_spec, observation, count)
            timer = DerivationStageTimer("quality_revision", (assessment.assessment_id,), coordinator.VERSION)
            try:
                candidate, count = await coordinator.revise(observation, opportunity, resolved, dossier, assessment)
            except QualityRevisionBlocked as exc:
                diagnostics["revision_count"] = await coordinator.count(observation, opportunity.work_spec)
                records.append(timer.finish((), str(exc), failure_code="context_quality_failed"))
                return dossier, blocker.model_copy(update={"summary": blocker.summary + "；" + str(exc),
                    "stage_records": tuple(records), "quality_recovery": diagnostics})
            records.append(timer.finish((candidate.dossier_id,), f"材料修订 {count}/2 已完成，重新独立评估"))
            dossier = candidate
            blocker = await self._verify_quality_stage(opportunity, resolved, dossier, records,
                artifact_expansion_id=artifact_expansion_id, persist_artifacts=True)
            if not isinstance(blocker, ExpansionBlocker) or blocker.code != "context_quality_failed":
                return dossier, blocker
        if isinstance(blocker, ExpansionBlocker):
            assessment = await self._saved_quality_assessment(opportunity, resolved, dossier)
            if assessment is not None:
                diagnostics = quality_recovery_diagnostics(assessment, opportunity.work_spec, observation, count)
            blocker = blocker.model_copy(update={"quality_recovery": diagnostics})
        return dossier, blocker

    async def _saved_quality_assessment(self, opportunity, resolved, dossier):
        payload = await self._artifact_payload(opportunity, "context_quality",
            (opportunity.work_spec.work_spec_id, resolved.resolution_id, dossier.dossier_id),
            str(getattr(self._quality, "VERSION", type(self._quality).__name__)))
        return ContextQualityAssessment.model_validate(payload) if payload and "assessment_id" in payload else None

    async def _verify_quality_stage(
        self,
        opportunity: ExpansionOpportunity,
        resolved: ResolvedEvidenceBundle,
        dossier: ValidatedContextDossier,
        records: list[DerivationStageRecord],
        *,
        artifact_expansion_id: str | None,
        persist_artifacts: bool,
    ) -> ContextQualityAssessment | ExpansionBlocker:
        quality_timer = DerivationStageTimer(
            "context_quality",
            (opportunity.work_spec.work_spec_id, resolved.resolution_id, dossier.dossier_id),
            type(self._quality).__name__,
        )
        quality_inputs = (
            opportunity.work_spec.work_spec_id,
            resolved.resolution_id,
            dossier.dossier_id,
        )
        quality_version = str(getattr(self._quality, "VERSION", type(self._quality).__name__))
        recovered_quality = await self._artifact_payload(
            opportunity,
            "context_quality",
            quality_inputs,
            quality_version,
        ) if persist_artifacts else None
        if recovered_quality is not None:
            quality_result = ContextQualityVerifier().restore(
                opportunity.work_spec,
                resolved,
                dossier,
                recovered_quality,
            )
        else:
            quality_result = await self._quality.verify(
                opportunity.work_spec,
                resolved,
                dossier,
            )
            await self._record_attempt_usage(opportunity.loop_id, quality_result.attempt_records)
            if quality_result.assessment is not None and persist_artifacts:
                await self._save_artifact(
                    opportunity,
                    "context_quality",
                    quality_inputs,
                    quality_version,
                    quality_result.assessment.model_dump(mode="json"),
                    expansion_id=artifact_expansion_id,
                    attempt_records=quality_result.attempt_records,
                )
            elif quality_result.assessment is None and persist_artifacts:
                await self._save_artifact(
                    opportunity,
                    "context_quality",
                    quality_inputs,
                    quality_version,
                    {
                        "preflight": quality_result.preflight.model_dump(mode="json"),
                        "blocker_code": quality_result.blocker_code,
                        "blocker_summary": quality_result.blocker_summary,
                    },
                    outcome="blocked",
                    expansion_id=artifact_expansion_id,
                    attempt_records=quality_result.attempt_records,
                )
        records.append(
            quality_timer.finish(
                (quality_result.assessment.assessment_id,) if quality_result.assessment else (),
                quality_result.blocker_summary or "minimality、sufficiency、coherence 均已通过",
                failure_code=quality_result.blocker_code,
            )
        )
        if quality_result.assessment is None or not quality_result.assessment.passes:
            return self._blocked(
                opportunity,
                quality_result.blocker_code or "context_quality_failed",
                quality_result.blocker_summary or "derived Context quality 未通过",
                tuple(records),
            )
        return quality_result.assessment

    @staticmethod
    def _blocked(
        opportunity: ExpansionOpportunity,
        code: str,
        summary: str,
        stage_records: tuple[DerivationStageRecord, ...] = (),
    ) -> ExpansionBlocker:
        return ExpansionBlocker(
            code=code,
            summary=summary[:2000],
            opportunity_id=opportunity.opportunity_id,
            stage_records=stage_records,
        )

    async def _artifact_payload(
        self,
        opportunity: ExpansionOpportunity,
        stage: str,
        input_identities: tuple[str, ...],
        version: str,
    ) -> dict | None:
        async with self._sessions() as session:
            row = await self._artifacts.stage_artifact(
                session,
                loop_id=opportunity.loop_id,
                round_id=opportunity.round_id,
                stage=stage,
                input_identities=input_identities,
                version=version,
            )
            return None if row is None or row.outcome != "ready" else row.payload

    async def _save_artifact(
        self,
        opportunity: ExpansionOpportunity,
        stage: str,
        input_identities: tuple[str, ...],
        version: str,
        payload: dict,
        *,
        outcome: str = "ready",
        expansion_id: str | None = None,
        attempt_records: tuple[dict[str, Any], ...] = (),
    ) -> None:
        async with self._sessions.begin() as session:
            await self._artifacts.put_stage_artifact(
                session,
                loop_id=opportunity.loop_id,
                round_id=opportunity.round_id,
                expansion_id=expansion_id,
                stage=stage,
                input_identities=input_identities,
                version=version,
                outcome=outcome,
                payload=payload,
                attempt_records=attempt_records,
            )

    async def _record_attempt_usage(
        self,
        loop_id: str,
        attempts: tuple[dict[str, Any], ...],
    ) -> None:
        attempts = tuple(item for item in attempts if not item.get("usage_managed"))
        if not attempts:
            return
        await LoopUsageLedger(self._sessions).record(
            loop_id,
            LoopUsageDelta(
                model_calls=sum(max(0, int(item.get("model_calls") or 0)) for item in attempts),
                input_tokens=sum(max(0, int(item.get("input_tokens") or 0)) for item in attempts),
                output_tokens=sum(max(0, int(item.get("output_tokens") or 0)) for item in attempts),
                retries=sum(1 for item in attempts if int(item.get("attempt") or 1) > 1),
            ),
        )


class DeterministicExpansionPlanCompiler:
    VERSION = "quality-gated-semantic-context-compiler-v3"

    def compile(
        self,
        opportunity: ExpansionOpportunity,
        intent: SpawnContextIntent,
        bundle: ResolvedEvidenceBundle,
        *,
        dossier: ValidatedContextDossier | None = None,
        quality_assessment: ContextQualityAssessment | None = None,
        stage_records: tuple[DerivationStageRecord, ...] = (),
    ) -> CompiledExpansion | ExpansionBlocker:
        try:
            verified_dossier, verified_quality = self._require_verified_package(
                opportunity,
                bundle,
                dossier,
                quality_assessment,
            )
            plan = self._plan(opportunity, bundle, verified_dossier)
            compiled_lane = compile_lane(plan.model_dump(mode="json"), bundle.evidence)
        except (KeyError, TypeError, ValueError) as exc:
            return ExpansionBlocker(
                code="compiler_failed",
                summary=f"无法从 resolved evidence 编译可运行 Context：{str(exc)[:1200]}",
                opportunity_id=opportunity.opportunity_id,
            )
        expansion_id = stable_expansion_hash(
            "compiled-expansion-v3",
            opportunity.opportunity_id,
            intent.model_dump(mode="json"),
            bundle.resolution_id,
            verified_dossier.dossier_id,
            verified_quality.assessment_id,
            compiled_lane.definition_hash,
            self.VERSION,
        )
        return CompiledExpansion(
            expansion_id=expansion_id,
            opportunity=opportunity,
            intent=intent,
            resolution=bundle,
            plan=plan,
            definition_hash=compiled_lane.definition_hash,
            compiler_version=self.VERSION,
            dossier_id=verified_dossier.dossier_id,
            synthesis_omitted=False,
            quality_assessment_id=verified_quality.assessment_id,
            dossier_payload=verified_dossier.model_dump(mode="json"),
            quality_assessment_payload=verified_quality.model_dump(mode="json"),
            stage_records=stage_records,
        )

    @staticmethod
    def _require_verified_package(
        opportunity: ExpansionOpportunity,
        bundle: ResolvedEvidenceBundle,
        dossier: ValidatedContextDossier | None,
        quality_assessment: ContextQualityAssessment | None,
    ) -> tuple[ValidatedContextDossier, ContextQualityAssessment]:
        if dossier is None:
            raise ValueError("automatic expansion 缺少 validated claim dossier")
        if quality_assessment is None:
            raise ValueError("automatic expansion 缺少 ContextQualityAssessment")
        if not quality_assessment.passes:
            raise ValueError("ContextQualityAssessment 未通过全部 required dimensions")
        expected = (
            opportunity.work_spec.work_spec_id,
            bundle.resolution_id,
            dossier.dossier_id,
        )
        actual = (
            quality_assessment.work_spec_id,
            quality_assessment.resolution_id,
            quality_assessment.dossier_id,
        )
        if expected != actual:
            raise ValueError("quality assessment identities 与 compilation package 不一致")
        if bundle.work_spec_id != opportunity.work_spec.work_spec_id:
            raise ValueError("resolved evidence 与 opportunity WorkSpec identity 不一致")
        if dossier.work_spec_id != opportunity.work_spec.work_spec_id or dossier.resolution_id != bundle.resolution_id:
            raise ValueError("validated dossier 与 compilation package identity 不一致")
        return dossier, quality_assessment

    def _plan(
        self,
        opportunity: ExpansionOpportunity,
        bundle: ResolvedEvidenceBundle,
        dossier: ValidatedContextDossier,
    ) -> CreateLanePlan:
        if not bundle.evidence_frontier:
            raise ValueError("自动派生 Context 至少需要一项 resolved evidence")
        items: list[ComposeMessage | CopyMessage | ToolExchange] = [
            ComposeMessage(
                type="compose_message",
                role="system",
                content=self._work_contract(opportunity),
                sources=bundle.evidence_frontier,
            ),
            ComposeMessage(
                type="compose_message",
                role="system",
                content=self._evidence_ledger(opportunity, bundle),
                sources=bundle.evidence_frontier,
            ),
        ]
        if dossier.claims:
            dossier_sources = tuple(
                sorted(
                    {
                        evidence_ref_key(ref): ref
                    for claim in dossier.claims
                    for ref in claim.citations
                    }.values(),
                    key=evidence_ref_key,
                )
            )
            items.append(
                ComposeMessage(
                    type="compose_message",
                    role="system",
                    content=render_context_dossier(dossier),
                    sources=dossier_sources,
                )
            )
        items.extend(self._primary_evidence(bundle.evidence))
        return CreateLanePlan(
            action="create",
            purpose=opportunity.work_spec.objective,
            source_frontier=bundle.source_frontier,
            evidence_frontier=bundle.evidence_frontier,
            items=tuple(items),
            lane_policy={
                "expansion_id": opportunity.opportunity_id,
                "work_spec_id": opportunity.work_spec.work_spec_id,
                "resolution_id": bundle.resolution_id,
                "dossier_id": dossier.dossier_id,
                "workspace_mode": opportunity.work_spec.workspace_requirement,
                "completion_criteria": opportunity.work_spec.completion_criteria,
            },
        )

    @staticmethod
    def _work_contract(opportunity: ExpansionOpportunity) -> str:
        spec = opportunity.work_spec
        questions = "\n".join(f"- {item}" for item in spec.questions)
        completion = "\n".join(f"- {item}" for item in spec.completion_criteria)
        return (
            "Work Contract\n"
            f"Objective: {spec.objective}\n"
            f"Separation reason: {spec.separation_reason}\n"
            f"Workspace requirement: {spec.workspace_requirement}\n"
            f"Questions:\n{questions}\n"
            f"Completion criteria:\n{completion}"
        )

    @staticmethod
    def _evidence_ledger(
        opportunity: ExpansionOpportunity,
        bundle: ResolvedEvidenceBundle,
    ) -> str:
        by_requirement: dict[str, list[str]] = {}
        for item in bundle.items:
            by_requirement.setdefault(item.requirement_id, []).append("/".join(evidence_ref_key(item.ref)))
        lines = ["Evidence Ledger"]
        for requirement in opportunity.work_spec.evidence_requirements:
            refs = sorted(by_requirement.get(requirement.requirement_id, ()))
            rendered = ", ".join(refs) if refs else "omitted (optional)"
            lines.append(f"- {requirement.requirement_id} [{requirement.role}/{requirement.necessity}]: {rendered}")
        return "\n".join(lines)

    @staticmethod
    def _primary_evidence(
        evidence: MultiSourceEvidence,
    ) -> tuple[ComposeMessage | CopyMessage | ToolExchange, ...]:
        items: list[ComposeMessage | CopyMessage | ToolExchange] = []
        for source in evidence.sources:
            results = {message.tool_call_id: message for message in source.messages if message.tool_call_id}
            consumed: set[tuple[str, str, str, str]] = set()
            for message in source.messages:
                if message.ref.key in consumed or message.role == "tool":
                    continue
                if message.tool_calls:
                    siblings = tuple(results.get(str(call.get("id") or "")) for call in message.tool_calls)
                    if any(result is None for result in siblings):
                        raise ValueError("resolved Tool Exchange 不完整")
                    calls = tuple(
                        ToolExchangeCall(
                            name=str(call.get("name") or ""),
                            args=dict(call.get("args") or {}),
                            result_content=result.content,
                            status=result.status or "success",
                        )
                        for call, result in zip(message.tool_calls, siblings, strict=True)
                        if result is not None
                    )
                    refs = (message.ref, *(result.ref for result in siblings if result is not None))
                    items.append(
                        ToolExchange(
                            type="tool_exchange",
                            assistant_content=str(message.content or ""),
                            calls=calls,
                            sources=refs,
                        )
                    )
                    consumed.update(ref.key for ref in refs)
                else:
                    items.append(CopyMessage(type="copy_message", source=message.ref))
                    consumed.add(message.ref.key)
        for item in sorted(evidence.structured, key=lambda value: evidence_ref_key(value.ref)):
            content = item.content if isinstance(item.content, str) else json.dumps(
                item.content,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            items.append(
                ComposeMessage(
                    type="compose_message",
                    role="system",
                    content=f"Primary structured evidence [{'/'.join(evidence_ref_key(item.ref))}]:\n{content}",
                    sources=(item.ref,),
                )
            )
        return tuple(items)
