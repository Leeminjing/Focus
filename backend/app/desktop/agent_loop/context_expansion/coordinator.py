r"""本文件对外提供 ContextExpansionCoordinator、ContextExpansionStage 与 ExpansionResolution。

输入为冻结 LoopObservationEnvelope、可替换的 signal/projector/planner/reconciler/policy 阶段与已存在 work-spec identities；输出为
确定性 ExpansionAssessment。具体工作流为 façade 汇总完整-index retrieval sessions，依次传递 signals、retrieved manifests、
WorkContextSpec、relation reconciliation 与 admission；Stage 记录 lifecycle，并把 identity-only Patrol 选择编译为经 synthesis/quality
验证的内部 LanePlan；Stage 另提供不创建 expansion/Context/Portfolio mutation 的 observe-only comparison，并可通过部署开关停止自动写入。
示例：`resolution = await stage.resolve(observation, intent)`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from focus.config.app_config import AppConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.context_expansion.artifact_repository import (
    SemanticDerivationArtifactRepository,
)
from backend.app.desktop.agent_loop.context_expansion.compiler import (
    ContextExpansionPlanCompiler,
)
from backend.app.desktop.agent_loop.context_expansion.contracts import (
    DerivationStageRecord,
    ExpansionAssessment,
    ExpansionBlocker,
    ExpansionOpportunity,
    SpawnContextIntent,
)
from backend.app.desktop.agent_loop.context_expansion.manifest_adapter import (
    CompositeSemanticManifestProjector,
)
from backend.app.desktop.agent_loop.context_expansion.planner import (
    CognitivePlannerPort,
    WorkerResultCognitivePlanner,
)
from backend.app.desktop.agent_loop.context_expansion.policy import (
    ExpansionAdmissionPolicy,
)
from backend.app.desktop.agent_loop.context_expansion.reconciliation import (
    DistinctByDefaultRelationEvaluator,
    StructuredWorkSpecRelationEvaluator,
    WorkSpecReconciler,
    WorkSpecRelationEvaluatorPort,
)
from backend.app.desktop.agent_loop.context_expansion.reconciliation_stage import (
    PersistedWorkSpecReconciliationStage,
)
from backend.app.desktop.agent_loop.context_expansion.repository import (
    ContextExpansionRepository,
)
from backend.app.desktop.agent_loop.context_expansion.shadow import (
    SemanticDerivationShadowReport,
    ShadowOpportunityOutcome,
)
from backend.app.desktop.agent_loop.context_expansion.signals import (
    ExpansionSignalCollector,
)
from backend.app.desktop.agent_loop.context_expansion.stage_telemetry import (
    DerivationStageTimer,
)
from backend.app.desktop.agent_loop.derivation_worker import RoleBoundStructuredModel
from backend.app.desktop.agent_loop.feature_flags import LoopFeatureFlags
from backend.app.desktop.agent_loop.schemas import (
    CreateLaneAction,
    LoopObservationEnvelope,
    PatrolDecisionIntent,
)


class ContextExpansionCoordinator:
    def __init__(
        self,
        signals: ExpansionSignalCollector | None = None,
        projector: CompositeSemanticManifestProjector | None = None,
        planner: CognitivePlannerPort | None = None,
        relation_evaluator: WorkSpecRelationEvaluatorPort | None = None,
        reconciler: WorkSpecReconciler | None = None,
        reconciliation_stage: PersistedWorkSpecReconciliationStage | None = None,
        policy: ExpansionAdmissionPolicy | None = None,
    ) -> None:
        self._signals = signals or ExpansionSignalCollector()
        self._projector = projector or CompositeSemanticManifestProjector()
        self._planner = planner or WorkerResultCognitivePlanner()
        self._relation_evaluator = relation_evaluator or DistinctByDefaultRelationEvaluator()
        self._reconciler = reconciler or WorkSpecReconciler()
        self._reconciliation_stage = reconciliation_stage
        self._policy = policy or ExpansionAdmissionPolicy()

    async def assess(
        self,
        observation: LoopObservationEnvelope,
        *,
        existing_independence_keys: frozenset[str] = frozenset(),
    ) -> ExpansionAssessment:
        records: list[DerivationStageRecord] = []
        signal_timer = DerivationStageTimer(
            "signal_collection",
            (observation.observed_frontier_hash,),
            self._signals.VERSION,
        )
        signals = self._signals.collect(observation)
        records.append(
            signal_timer.finish(
                tuple(item.signal_id for item in signals.signals),
                f"已收集 {len(signals.signals)} 项结构化派生信号",
            )
        )
        planning_payloads = tuple(
            item.get("result") or {}
            for item in observation.worker_results
            if item.get("kind") == "lane_curator"
            and (item.get("result") or {}).get("planning_session")
        )
        planning_sessions = tuple(item["planning_session"] for item in planning_payloads)
        if planning_sessions:
            index_ids = tuple(
                sorted(
                    {
                        index_id
                        for session in planning_sessions
                        for index_id in tuple(session.get("authorized_index_ids") or ())
                    }
                )
            )
            index_record = self._worker_stage_record(planning_payloads, "portfolio_index_stage")
            retrieval_record = self._worker_stage_record(planning_payloads, "retrieval_stage_record")
            records.append(
                index_record
                or DerivationStageTimer(
                    "portfolio_indexing",
                    (signals.observation_hash,),
                    "portfolio-semantic-index-service-v1",
                ).finish(index_ids, f"已准备 {len(index_ids)} 个完整 Revision semantic indexes")
            )
            records.append(
                retrieval_record
                or DerivationStageTimer(
                    "retrieval_planning",
                    index_ids,
                    "retrieval-backed-cognitive-advisor-v1",
                ).finish(
                    tuple(str(item["session_id"]) for item in planning_sessions),
                    f"已完成 {len(planning_sessions)} 个冻结 planning retrieval sessions",
                )
            )
        projection_timer = DerivationStageTimer(
            "portfolio_projection",
            (signals.observation_hash,),
            self._projector.VERSION,
        )
        try:
            manifests = self._projector.project(observation)
        except (TypeError, ValueError) as exc:
            records.append(
                projection_timer.finish(
                    (),
                    "Portfolio semantic projection 失败",
                    failure_code="portfolio_projection_failed",
                )
            )
            return self._failed_assessment(
                observation,
                "portfolio_projection_failed",
                f"冻结 Portfolio 无法投影为可信 semantic manifests：{exc}",
                stage_records=tuple(records),
            )
        records.append(
            projection_timer.finish(
                tuple(item.manifest_id for item in manifests),
                f"已冻结 {len(manifests)} 个 semantic manifests",
            )
        )
        planning_timer = DerivationStageTimer(
            "cognitive_planning",
            tuple(item.manifest_id for item in manifests),
            self._planner.VERSION,
        )
        planned = await self._planner.plan(observation, signals, manifests)
        if planned.failure is not None:
            records.append(
                planning_timer.finish(
                    (),
                    planned.failure.summary,
                    failure_code=planned.failure.code,
                )
            )
            return self._failed_assessment(
                observation,
                "cognitive_planning_failed",
                planned.failure.summary,
                retryable=planned.failure.retryable,
                stage_records=tuple(records),
            )
        records.append(
            planning_timer.finish(
                tuple(item.work_spec_id for item in planned.work_specs),
                f"已规划 {len(planned.work_specs)} 项独立认知工作",
            )
        )
        reconciliation_timer = DerivationStageTimer(
            "work_reconciliation",
            tuple(item.work_spec_id for item in planned.work_specs),
            self._reconciler.VERSION,
        )
        try:
            if self._reconciliation_stage is not None:
                reconciliation = await self._reconciliation_stage.reconcile(
                    observation.loop_id,
                    observation.round_id,
                    planned.work_specs,
                    planned.required_work_spec_ids,
                )
            else:
                relations = await self._relation_evaluator.evaluate(planned.work_specs)
                reconciliation = self._reconciler.reconcile(
                    planned.work_specs,
                    relations=relations,
                    required_candidate_ids=planned.required_work_spec_ids,
                )
        except (TypeError, ValueError) as exc:
            records.append(
                reconciliation_timer.finish(
                    (),
                    "WorkSpec reconciliation 失败",
                    failure_code="work_reconciliation_failed",
                )
            )
            return self._failed_assessment(
                observation,
                "work_reconciliation_failed",
                str(exc)[:1800],
                stage_records=tuple(records),
            )
        records.append(
            reconciliation_timer.finish(
                (reconciliation.reconciliation_id, *(item.work_spec_id for item in reconciliation.canonical_specs)),
                f"已将 {len(planned.work_specs)} 个候选归并为 {len(reconciliation.canonical_specs)} 个 canonical WorkSpecs",
            )
        )
        if reconciliation.conflicts:
            records[-1] = records[-1].model_copy(update={"failure_code": "work_spec_conflict"})
            return self._failed_assessment(
                observation,
                "work_spec_conflict",
                "WorkSpec candidates 存在互斥职责或 workspace 边界，拒绝自动合并",
                stage_records=tuple(records),
                reconciliation=reconciliation.model_dump(mode="json"),
            )
        opportunities = tuple(
            ExpansionOpportunity.create(
                loop_id=observation.loop_id,
                round_id=observation.round_id,
                observation_hash=signals.observation_hash,
                work_spec=work_spec,
                manifest_sources=tuple(manifest.source for manifest in manifests),
                manifest_ids=tuple(manifest.manifest_id for manifest in manifests),
                projector_versions=tuple(manifest.projector_version for manifest in manifests),
                signal_ids=tuple(signal.signal_id for signal in signals.signals),
                required=work_spec.work_spec_id in reconciliation.required_work_spec_ids,
            )
            for work_spec in reconciliation.canonical_specs
        )
        admission_timer = DerivationStageTimer(
            "admission",
            tuple(item.work_spec_id for item in reconciliation.canonical_specs),
            self._policy.VERSION,
        )
        assessment = self._policy.evaluate(
            observation,
            opportunities,
            existing_independence_keys=existing_independence_keys,
        )
        admission_failure = assessment.blockers[0].code if assessment.blockers else None
        records.append(
            admission_timer.finish(
                tuple(item.opportunity_id for item in assessment.opportunities),
                f"Admission 结果为 {assessment.level}",
                failure_code=admission_failure,
            )
        )
        return assessment.model_copy(
            update={
                "stage_records": tuple(records),
                "reconciliation": reconciliation.model_dump(mode="json"),
            }
        )

    @staticmethod
    def _worker_stage_record(
        payloads: tuple[dict[str, Any], ...],
        key: str,
    ) -> DerivationStageRecord | None:
        parsed = (
            DerivationStageRecord.model_validate(item[key])
            for item in payloads
            if item.get(key)
        )
        candidates = tuple({item.model_dump_json(): item for item in parsed}.values())
        if not candidates:
            return None
        first = candidates[0]
        if any(item.stage != first.stage or item.version != first.version for item in candidates):
            raise ValueError("worker derivation stage records 的 stage/version 不一致")
        failures = tuple(item.failure_code for item in candidates if item.failure_code)
        return DerivationStageRecord(
            stage=first.stage,
            input_identities=tuple(identity for item in candidates for identity in item.input_identities),
            output_identities=tuple(identity for item in candidates for identity in item.output_identities),
            version=first.version,
            duration_ms=sum(item.duration_ms for item in candidates),
            safe_summary="; ".join(item.safe_summary for item in candidates)[:1000],
            failure_code=failures[0] if failures else None,
        )

    def _failed_assessment(
        self,
        observation: LoopObservationEnvelope,
        code: str,
        summary: str,
        *,
        retryable: bool = False,
        stage_records: tuple[DerivationStageRecord, ...] = (),
        reconciliation: dict | None = None,
    ) -> ExpansionAssessment:
        return ExpansionAssessment(
            loop_id=observation.loop_id,
            round_id=observation.round_id,
            frontier_hash=observation.observed_frontier_hash,
            policy_version=self._policy.VERSION,
            level="not_applicable",
            blockers=(ExpansionBlocker(code=code, summary=summary, retryable=retryable),),
            stage_records=stage_records,
            reconciliation=reconciliation,
        )


@dataclass(frozen=True, slots=True)
class ExpansionResolution:
    intent: PatrolDecisionIntent | None
    blocker: ExpansionBlocker | None


class ContextExpansionStage:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        checkpointer,
        app_config: AppConfig | None = None,
        *,
        writes_enabled: bool | None = None,
    ) -> None:
        self._sessions = sessions
        self._repository = ContextExpansionRepository()
        self._artifacts = SemanticDerivationArtifactRepository()
        relation_evaluator = (
            StructuredWorkSpecRelationEvaluator(
                RoleBoundStructuredModel(app_config, "work_spec_reconciler")
            )
            if app_config is not None
            else DistinctByDefaultRelationEvaluator()
        )
        reconciliation_stage = PersistedWorkSpecReconciliationStage(
            sessions,
            relation_evaluator,
        )
        self._coordinator = ContextExpansionCoordinator(
            relation_evaluator=relation_evaluator,
            reconciliation_stage=reconciliation_stage,
        )
        self._compiler = ContextExpansionPlanCompiler(
            sessions,
            checkpointer,
            app_config=app_config,
        )
        self._writes_enabled = (
            LoopFeatureFlags.from_env().automatic_context_expansion_writes
            if writes_enabled is None
            else writes_enabled
        )

    async def assess(self, observation: LoopObservationEnvelope) -> ExpansionAssessment:
        async with self._sessions() as session:
            existing = await self._repository.active_independence_keys(
                session,
                observation.loop_id,
                exclude_round_id=observation.round_id,
            )
            prior = await self._repository.by_round(session, observation.round_id)
        assessment = await self._coordinator.assess(
            observation,
            existing_independence_keys=existing,
        )
        assessment = self._terminalized_assessment(assessment, prior)
        await self._record_stage_artifacts(observation, assessment)
        await self._record(observation, assessment)
        return assessment

    async def _record_stage_artifacts(
        self,
        observation: LoopObservationEnvelope,
        assessment: ExpansionAssessment,
    ) -> None:
        async with self._sessions.begin() as session:
            for record in assessment.stage_records:
                existing = await self._artifacts.stage_artifact(
                    session,
                    loop_id=observation.loop_id,
                    round_id=observation.round_id,
                    stage=record.stage,
                    input_identities=record.input_identities,
                    version=record.version,
                )
                if existing is not None:
                    continue
                await self._artifacts.put_stage_artifact(
                    session,
                    loop_id=observation.loop_id,
                    round_id=observation.round_id,
                    stage=record.stage,
                    input_identities=record.input_identities,
                    version=record.version,
                    outcome="blocked" if record.failure_code else "ready",
                    payload=record.model_dump(mode="json"),
                )

    async def resolve(
        self,
        observation: LoopObservationEnvelope,
        intent: PatrolDecisionIntent,
    ) -> ExpansionResolution:
        assessment = ExpansionAssessment.model_validate(observation.expansion_assessment or {})
        opportunities = {item.opportunity_id: item for item in assessment.opportunities}
        resolved = []
        for action in intent.actions:
            if action.action == "spawn_context":
                if not self._writes_enabled:
                    blocker = ExpansionBlocker(
                        code="automatic_expansion_disabled",
                        summary="自动 Context expansion 写入已由部署回滚开关禁用",
                        opportunity_id=action.opportunity_id,
                    )
                    if action.opportunity_id in opportunities:
                        await self._transition(
                            action.opportunity_id,
                            "blocked",
                            blocker.summary,
                            blocker=blocker,
                        )
                    return ExpansionResolution(intent=None, blocker=blocker)
                result = await self._resolve_spawn(observation, opportunities, action)
                if isinstance(result, ExpansionBlocker):
                    return ExpansionResolution(intent=None, blocker=result)
                resolved.append(result)
            elif action.action == "decline_expansion":
                await self._resolve_decline(opportunities, action)
                resolved.append(action)
            else:
                resolved.append(action)
        return ExpansionResolution(
            intent=intent.model_copy(update={"actions": tuple(resolved)}),
            blocker=None,
        )

    async def observe_only(
        self,
        observation: LoopObservationEnvelope,
    ) -> SemanticDerivationShadowReport:
        assessment = await self._coordinator.assess(observation)
        outcomes: list[ShadowOpportunityOutcome] = []
        for opportunity in assessment.opportunities:
            compiled = await self._compiler.compile(
                observation,
                opportunity,
                SpawnContextIntent(opportunity_id=opportunity.opportunity_id),
                persist_artifacts=False,
            )
            if isinstance(compiled, ExpansionBlocker):
                outcomes.append(
                    ShadowOpportunityOutcome(
                        opportunity_id=opportunity.opportunity_id,
                        resolved_evidence_count=0,
                        blocker_code=compiled.code,
                    )
                )
                continue
            dimensions = tuple(
                f"{item['dimension']}:{item['verdict']}"
                for item in tuple((compiled.quality_assessment_payload or {}).get("dimensions") or ())
            )
            outcomes.append(
                ShadowOpportunityOutcome(
                    opportunity_id=opportunity.opportunity_id,
                    resolved_evidence_count=len(compiled.resolution.items),
                    quality_verdicts=dimensions,
                )
            )
        index_ids = tuple(
            sorted(
                {
                    index_id
                    for worker in observation.worker_results
                    for index_id in tuple(
                        ((worker.get("result") or {}).get("planning_session") or {}).get(
                            "authorized_index_ids"
                        )
                        or ()
                    )
                }
            )
        )
        async with self._sessions() as session:
            indexes = await self._artifacts.indexes_by_ids(session, index_ids) if index_ids else ()
        report = SemanticDerivationShadowReport.create(
            frontier_hash=observation.observed_frontier_hash,
            preview_message_count=sum(
                len(tuple(item.get("message_evidence_preview") or ()))
                for item in observation.portfolio_frontier
            ),
            indexed_message_count=sum(len(item.messages) for item in indexes),
            canonical_candidate_count=len(assessment.opportunities),
            outcomes=tuple(outcomes),
        )
        async with self._sessions.begin() as session:
            await self._artifacts.put_stage_artifact(
                session,
                loop_id=observation.loop_id,
                round_id=observation.round_id,
                stage="shadow_comparison",
                input_identities=(observation.observed_frontier_hash,),
                version="semantic-derivation-shadow-v1",
                outcome="ready",
                payload=report.model_dump(mode="json"),
                attempt_records=({"role": "observe_only", "outcome": "ready"},),
            )
        return report

    async def _resolve_spawn(self, observation, opportunities, action) -> CreateLaneAction | ExpansionBlocker:
        opportunity = opportunities.get(action.opportunity_id)
        if opportunity is None:
            return ExpansionBlocker(
                code="compiler_failed",
                summary="spawn_context 引用了当前 assessment 之外的 opportunity",
                opportunity_id=action.opportunity_id,
            )
        await self._transition(opportunity.opportunity_id, "proposed", f"Patrol 提议派生：{opportunity.work_spec.objective}")
        compiled = await self._compiler.compile(
            observation,
            opportunity,
            SpawnContextIntent.model_validate(action.model_dump(mode="json")),
            artifact_expansion_id=opportunity.opportunity_id,
        )
        if isinstance(compiled, ExpansionBlocker):
            await self._transition(
                opportunity.opportunity_id,
                "blocked",
                compiled.summary,
                blocker=compiled,
                result={
                    "stage_records_append": [
                        item.model_dump(mode="json") for item in compiled.stage_records
                    ]
                },
            )
            return compiled
        await self._record_compiled_artifacts(opportunity, compiled)
        records = {item.stage: item.model_dump(mode="json") for item in compiled.stage_records}
        await self._transition(
            opportunity.opportunity_id,
            "evidence_resolved",
            f"已解析 {len(compiled.resolution.items)} 项 requirement evidence",
            result={
                "resolution": compiled.resolution.model_dump(mode="json"),
                "resolution_id": compiled.resolution.resolution_id,
                "evidence_frontier": [
                    ref.model_dump(mode="json")
                    for ref in compiled.resolution.evidence_frontier
                ],
                "resolver_version": "multi-source-evidence-resolver-v1",
                "stage_record": records["evidence_resolution"],
            },
        )
        await self._transition(
            opportunity.opportunity_id,
            "dossier_built",
            "已构建 claim-level evidence-grounded dossier",
            result={
                "dossier_id": compiled.dossier_id,
                "dossier": compiled.dossier_payload,
                "synthesis_omitted": False,
                "stage_record": records["dossier_synthesis"],
            },
        )
        await self._transition(
            opportunity.opportunity_id,
            "quality_verified",
            "Derived Context minimality、sufficiency、coherence 均已通过",
            result={
                "quality_assessment_id": compiled.quality_assessment_id,
                "quality_assessment": compiled.quality_assessment_payload,
                "stage_record": records["context_quality"],
            },
        )
        await self._transition(
            opportunity.opportunity_id,
            "compiled",
            "Semantic spawn 已编译为可运行的内部 LanePlan",
            result={
                "compiled_expansion_id": compiled.expansion_id,
                "definition_hash": compiled.definition_hash,
                "compiler_version": compiled.compiler_version,
                "compiled_plan": compiled.plan.model_dump(mode="json"),
                "stage_record": records["compilation"],
            },
        )
        return CreateLaneAction(action="create_lane", plan=compiled.plan, message=opportunity.work_spec.objective)

    @staticmethod
    def _terminalized_assessment(assessment: ExpansionAssessment, prior: tuple) -> ExpansionAssessment:
        terminal = {
            row.opportunity_id: row
            for row in prior
            if ContextExpansionRepository.is_terminal(row.state)
        }
        if not terminal:
            return assessment
        opportunities = tuple(
            item for item in assessment.opportunities if item.opportunity_id not in terminal
        )
        blockers = {item.opportunity_id: item for item in assessment.blockers}
        fallback_codes = {
            "declined": "not_independent",
            "blocked": "compiler_failed",
            "failed": "compiler_failed",
            "superseded": "stale_source",
            "dispatched": "duplicate_expansion",
        }
        for opportunity_id, row in terminal.items():
            blockers[opportunity_id] = ExpansionBlocker(
                code=row.blocker_code or fallback_codes[row.state],
                summary=row.safe_summary,
                opportunity_id=opportunity_id,
            )
        return assessment.model_copy(
            update={
                "level": assessment.level if opportunities else "not_applicable",
                "opportunities": opportunities,
                "blockers": tuple(blockers.values()),
                "decision_deadline_round": assessment.decision_deadline_round if opportunities else None,
            }
        )

    async def _resolve_decline(self, opportunities, action) -> None:
        if action.opportunity_id is None or action.opportunity_id not in opportunities:
            return
        await self._transition(
            action.opportunity_id,
            "declined",
            action.reason,
            blocker=ExpansionBlocker(
                code=action.blocker_code,
                summary=action.reason,
                opportunity_id=action.opportunity_id,
            ),
        )

    async def _record(self, observation: LoopObservationEnvelope, assessment: ExpansionAssessment) -> None:
        blockers = {item.opportunity_id: item for item in assessment.blockers if item.opportunity_id}
        async with self._sessions.begin() as session:
            for opportunity in assessment.opportunities:
                row = await self._repository.create(
                    session,
                    opportunity,
                    policy_version=assessment.policy_version,
                    level=assessment.level,
                    correlation_id=observation.round_id,
                    stage_records=assessment.stage_records,
                )
                blocker = blockers.get(opportunity.opportunity_id)
                if blocker is not None and not self._repository.is_terminal(row.state):
                    await self._repository.transition(
                        session,
                        row.expansion_id,
                        "blocked",
                        blocker.summary,
                        blocker_code=blocker.code,
                    )

    async def _record_compiled_artifacts(self, opportunity, compiled) -> None:
        payloads = (
            (
                "compilation",
                (
                    compiled.resolution.resolution_id,
                    str(compiled.dossier_id),
                    str(compiled.quality_assessment_id),
                ),
                compiled.compiler_version,
                {
                    "compiled_expansion_id": compiled.expansion_id,
                    "definition_hash": compiled.definition_hash,
                    "plan": compiled.plan.model_dump(mode="json"),
                },
            ),
        )
        async with self._sessions.begin() as session:
            for stage, inputs, version, payload in payloads:
                await self._artifacts.put_stage_artifact(
                    session,
                    loop_id=opportunity.loop_id,
                    round_id=opportunity.round_id,
                    expansion_id=opportunity.opportunity_id,
                    stage=stage,
                    input_identities=inputs,
                    version=version,
                    outcome="ready",
                    payload=payload,
                )

    async def _transition(
        self,
        expansion_id: str,
        target: str,
        summary: str,
        *,
        blocker: ExpansionBlocker | None = None,
        result: dict | None = None,
    ) -> None:
        async with self._sessions.begin() as session:
            await self._repository.transition(
                session,
                expansion_id,
                target,
                summary,
                blocker_code=blocker.code if blocker else None,
                result=result,
            )
