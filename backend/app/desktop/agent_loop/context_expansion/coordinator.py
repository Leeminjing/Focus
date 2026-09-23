r"""本文件对外提供 ContextExpansionCoordinator、ContextExpansionStage 与 ExpansionResolution。

输入为冻结 LoopObservationEnvelope、可替换的 signal/projector/planner/policy 阶段与已存在 work-spec identities；输出为
确定性 ExpansionAssessment。具体工作流为 façade 依次传递不可变 signals、semantic manifests、WorkContextSpec 和 admission
结果；Stage 记录 lifecycle，并把 identity-only Patrol 选择编译为内部 LanePlan，不持有 Kernel 或 Portfolio 提交能力。
示例：`resolution = await stage.resolve(observation, intent)`。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from backend.app.desktop.agent_loop.context_expansion.repository import (
    ContextExpansionRepository,
)
from backend.app.desktop.agent_loop.context_expansion.signals import (
    ExpansionSignalCollector,
)
from backend.app.desktop.agent_loop.context_expansion.stage_telemetry import (
    DerivationStageTimer,
)
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
        policy: ExpansionAdmissionPolicy | None = None,
    ) -> None:
        self._signals = signals or ExpansionSignalCollector()
        self._projector = projector or CompositeSemanticManifestProjector()
        self._planner = planner or WorkerResultCognitivePlanner()
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
        projection_timer = DerivationStageTimer(
            "portfolio_projection",
            (signals.observation_hash,),
            self._projector.VERSION,
        )
        try:
            manifests = self._projector.project(observation)
        except (TypeError, ValueError) as exc:
            records.append(projection_timer.finish((), "Portfolio semantic projection 失败"))
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
            records.append(planning_timer.finish((), planned.failure.summary))
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
                required=work_spec.work_spec_id in planned.required_work_spec_ids,
            )
            for work_spec in planned.work_specs
        )
        admission_timer = DerivationStageTimer(
            "admission",
            tuple(item.work_spec_id for item in planned.work_specs),
            self._policy.VERSION,
        )
        assessment = self._policy.evaluate(
            observation,
            opportunities,
            existing_independence_keys=existing_independence_keys,
        )
        records.append(
            admission_timer.finish(
                tuple(item.opportunity_id for item in assessment.opportunities),
                f"Admission 结果为 {assessment.level}",
            )
        )
        return assessment.model_copy(update={"stage_records": tuple(records)})

    def _failed_assessment(
        self,
        observation: LoopObservationEnvelope,
        code: str,
        summary: str,
        *,
        retryable: bool = False,
        stage_records: tuple[DerivationStageRecord, ...] = (),
    ) -> ExpansionAssessment:
        return ExpansionAssessment(
            loop_id=observation.loop_id,
            round_id=observation.round_id,
            frontier_hash=observation.observed_frontier_hash,
            policy_version=self._policy.VERSION,
            level="not_applicable",
            blockers=(ExpansionBlocker(code=code, summary=summary, retryable=retryable),),
            stage_records=stage_records,
        )


@dataclass(frozen=True, slots=True)
class ExpansionResolution:
    intent: PatrolDecisionIntent | None
    blocker: ExpansionBlocker | None


class ContextExpansionStage:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], checkpointer) -> None:
        self._sessions = sessions
        self._repository = ContextExpansionRepository()
        self._coordinator = ContextExpansionCoordinator()
        self._compiler = ContextExpansionPlanCompiler(sessions, checkpointer)

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
        await self._record(observation, assessment)
        return assessment

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
        synthesis_state = "synthesis_omitted" if compiled.synthesis_omitted else "dossier_built"
        await self._transition(
            opportunity.opportunity_id,
            synthesis_state,
            "Dossier synthesis 已省略，原始 evidence 保持权威"
            if compiled.synthesis_omitted
            else "已构建带引用的 evidence-grounded dossier",
            result={
                "dossier_id": compiled.dossier_id,
                "synthesis_omitted": compiled.synthesis_omitted,
                "stage_record": records["dossier_synthesis"],
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
