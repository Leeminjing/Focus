r"""本文件对外提供 ContextExpansionCoordinator、ContextExpansionStage 与 ExpansionResolution。

输入为冻结 LoopObservationEnvelope、可选无权威 Curator adapter 与已存在 independence keys；输出为确定性
ExpansionAssessment。具体工作流为 detector 先生成结构化候选，Curator 可补充语义提案，coordinator 将提案绑定
冻结 source 后交给 policy admission；Stage 记录 assessment lifecycle，并把编译成功 intent 或结构化终态 blocker 返回编排层，
不持有 Kernel 或 Portfolio 提交能力。示例：`resolution = await stage.resolve(observation, intent)`。
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.desktop.agent_loop.context_expansion.compiler import ContextExpansionPlanCompiler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    CuratorExpansionProposal,
    ExpansionAssessment,
    ExpansionBlocker,
    ExpansionOpportunity,
    SpawnContextIntent,
)
from backend.app.desktop.agent_loop.context_expansion.curator import ExpansionCuratorPort, NoopExpansionCurator, WorkerResultExpansionCurator
from backend.app.desktop.agent_loop.context_expansion.detector import ExpansionOpportunityDetector
from backend.app.desktop.agent_loop.context_expansion.policy import ExpansionAdmissionPolicy
from backend.app.desktop.agent_loop.context_expansion.repository import ContextExpansionRepository
from backend.app.desktop.agent_loop.schemas import CreateLaneAction, LoopObservationEnvelope, PatrolDecisionIntent
from backend.app.desktop.context_evolution import ContextRevisionRef


class ContextExpansionCoordinator:
    def __init__(
        self,
        detector: ExpansionOpportunityDetector | None = None,
        policy: ExpansionAdmissionPolicy | None = None,
        curator: ExpansionCuratorPort | None = None,
    ) -> None:
        self._detector = detector or ExpansionOpportunityDetector()
        self._policy = policy or ExpansionAdmissionPolicy()
        self._curator = curator or NoopExpansionCurator()

    async def assess(
        self,
        observation: LoopObservationEnvelope,
        *,
        existing_independence_keys: frozenset[str] = frozenset(),
    ) -> ExpansionAssessment:
        detected = self._detector.detect(observation)
        proposals = await self._curator.propose(observation, detected)
        opportunities = self._merge(observation, detected, proposals)
        return self._policy.evaluate(
            observation,
            opportunities,
            existing_independence_keys=existing_independence_keys,
        )

    @staticmethod
    def _merge(
        observation: LoopObservationEnvelope,
        detected: tuple[ExpansionOpportunity, ...],
        proposals: tuple[CuratorExpansionProposal, ...],
    ) -> tuple[ExpansionOpportunity, ...]:
        by_key = {item.independence_key.casefold(): item for item in detected}
        sources = {
            item.source.context_id: item.source
            for item in detected
        }
        for frontier in observation.portfolio_frontier:
            if frontier.get("revision"):
                source = ContextRevisionRef.model_validate(frontier["revision"])
                sources.setdefault(source.context_id, source)
        for proposal in proposals:
            source = sources.get(proposal.source_context_id)
            if source is None:
                continue
            opportunity = ExpansionOpportunity.create(
                loop_id=observation.loop_id,
                round_id=observation.round_id,
                source=source,
                purpose=proposal.purpose,
                work_order=proposal.work_order,
                completion_check=proposal.completion_check,
                workspace_mode=proposal.workspace_mode,
                independence_key=proposal.independence_key,
                triggers=("curator_proposal",),
                evidence_hints=proposal.evidence_hints,
                required=proposal.required,
            )
            by_key.setdefault(opportunity.independence_key.casefold(), opportunity)
        return tuple(by_key.values())


@dataclass(frozen=True, slots=True)
class ExpansionResolution:
    intent: PatrolDecisionIntent | None
    blocker: ExpansionBlocker | None


class ContextExpansionStage:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], checkpointer) -> None:
        self._sessions = sessions
        self._repository = ContextExpansionRepository()
        self._coordinator = ContextExpansionCoordinator(curator=WorkerResultExpansionCurator())
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
        await self._transition(opportunity.opportunity_id, "proposed", f"Patrol 提议派生：{opportunity.purpose}")
        compiled = await self._compiler.compile(
            observation,
            opportunity,
            SpawnContextIntent.model_validate(action.model_dump(mode="json")),
        )
        if isinstance(compiled, ExpansionBlocker):
            await self._transition(opportunity.opportunity_id, "blocked", compiled.summary, blocker=compiled)
            return compiled
        await self._transition(
            opportunity.opportunity_id,
            "compiled",
            "Semantic spawn 已编译为可运行的内部 LanePlan",
            result={
                "compiled_expansion_id": compiled.expansion_id,
                "definition_hash": compiled.definition_hash,
                "compiler_version": compiled.compiler_version,
            },
        )
        return CreateLaneAction(action="create_lane", plan=compiled.plan, message=action.work_order)

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
                elif "curator_proposal" in opportunity.triggers and row.state == "detected":
                    await self._repository.transition(
                        session,
                        row.expansion_id,
                        "curated",
                        f"Curator 提出 Context 派生候选：{opportunity.purpose}",
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
