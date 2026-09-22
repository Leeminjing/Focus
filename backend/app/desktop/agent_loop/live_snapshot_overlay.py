r"""本文件对外提供 LoopLiveProjectionOverlay。

输入为 journal 投影、单一 sequence 边界和当前物化领域表；输出为补齐 Loop、Mission、Wait、Context/Run、Context 派生边、Curator、Expansion、Directive、Fact、恢复诊断与 Portfolio 的完整 snapshot。
具体工作流为批量读取各 current entity，按统一 ProjectedEntity 信封归并，并附加渲染所需授权与预算字段；派生边由
ContextLineageResolver 从权威 revision 来源解析，作为客户端在快照边界上的基线。示例：`await overlay.apply(session, projection, boundary)`。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.fact_models import LoopFact
from backend.app.desktop.agent_loop.context_expansion.models import LoopContextExpansion
from backend.app.desktop.agent_loop.live_projection_contract import LoopLiveProjection, ProjectedEntity
from backend.app.desktop.agent_loop.materialized_fact_query import MaterializedFactQueryService
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopContextMembership, LoopDelegationGrant, LoopDirective, LoopRound
from backend.app.desktop.agent_loop.patrol_session_models import LoopCuratorAssignment, LoopPatrolSession
from backend.app.desktop.agent_loop.projection_models import LoopProjectionFailure
from backend.app.desktop.agent_loop.wait_models import LoopWaitRequest, LoopWaitResponse
from backend.app.desktop.context_curation.models import PortfolioRevision
from backend.app.desktop.context_evolution.lineage import ContextLineageEdge, ContextLineageResolver
from backend.app.desktop.models import DesktopRun, DesktopThread
from backend.app.desktop.run_orchestration.models import RunDispatch


class LoopLiveProjectionOverlay:
    def __init__(self, lineage: ContextLineageResolver | None = None) -> None:
        self._lineage = lineage or ContextLineageResolver()

    async def apply(self, session: AsyncSession, projection: LoopLiveProjection, sequence: int) -> LoopLiveProjection:
        loop = await session.get(AgentLoop, projection.loop_id)
        mission = await session.scalar(select(LoopMissionRevision).where(LoopMissionRevision.loop_id == projection.loop_id, LoopMissionRevision.revision == loop.goal_revision))
        round_row = await session.get(LoopRound, loop.current_round_id) if loop.current_round_id else None
        patrol = await session.scalar(select(LoopPatrolSession).where(LoopPatrolSession.loop_id == projection.loop_id).order_by(LoopPatrolSession.started_at.desc()).limit(1))
        memberships = tuple((await session.scalars(select(LoopContextMembership).where(LoopContextMembership.loop_id == projection.loop_id))).all())
        membership_by_context = {item.context_id: item for item in memberships}
        context_ids = tuple(item.context_id for item in memberships)
        contexts = () if not context_ids else tuple((await session.scalars(select(DesktopThread).where(DesktopThread.task_id.in_(context_ids)))).all())
        runs = tuple((await session.scalars(select(DesktopRun).where(DesktopRun.loop_id == projection.loop_id).order_by(DesktopRun.created_at.desc()).limit(200))).all())
        run_ids = tuple(row.run_id for row in runs)
        dispatch_rows = () if not run_ids else tuple((await session.scalars(select(RunDispatch).where(RunDispatch.run_id.in_(run_ids)))).all())
        dispatch_by_run = {row.run_id: row for row in dispatch_rows}
        curators = tuple((await session.scalars(select(LoopCuratorAssignment).where(LoopCuratorAssignment.loop_id == projection.loop_id))).all())
        expansions = tuple((await session.scalars(select(LoopContextExpansion).where(LoopContextExpansion.loop_id == projection.loop_id))).all())
        directives = tuple((await session.scalars(select(LoopDirective).where(LoopDirective.loop_id == projection.loop_id))).all())
        facts = tuple((await session.scalars(select(LoopFact).where(LoopFact.loop_id == projection.loop_id))).all())
        wait_requests = tuple((await session.scalars(select(LoopWaitRequest).where(LoopWaitRequest.loop_id == projection.loop_id).order_by(LoopWaitRequest.created_at.desc()).limit(50))).all())
        wait_request_ids = tuple(row.request_id for row in wait_requests)
        wait_responses = () if not wait_request_ids else tuple((await session.scalars(select(LoopWaitResponse).where(LoopWaitResponse.request_id.in_(wait_request_ids)))).all())
        failures = tuple((await session.scalars(select(LoopProjectionFailure).where(LoopProjectionFailure.loop_id == projection.loop_id, LoopProjectionFailure.status != "resolved"))).all())
        grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == projection.loop_id).order_by(LoopDelegationGrant.revision.desc()).limit(1))
        usage = await session.get(LoopBudgetUsage, projection.loop_id)
        portfolio = await session.get(PortfolioRevision, loop.current_portfolio_revision_id) if loop.current_portfolio_revision_id else None
        lineage = await self._lineage.resolve(
            session,
            {row.task_id: row.current_revision_id for row in contexts if row.current_revision_id},
        )
        return projection.model_copy(update={
            "loop": self._entity(loop.loop_id, loop.revision, sequence, {"loop_id": loop.loop_id, "workspace_id": loop.workspace_id, "initial_context_id": loop.initial_context_id, "program_id": loop.program_id, "status": loop.status, "health": loop.health, "revision": loop.revision, "authority_revision": loop.authority_revision, "goal_revision": loop.goal_revision, "active_mission_revision": loop.goal_revision, "current_round_id": loop.current_round_id, "current_portfolio_revision_id": loop.current_portfolio_revision_id, "waiting_reason": loop.waiting_reason, "equipment": loop.equipment, "final_result": loop.final_result, "grant": self._grant(grant), "usage": self._usage(usage, len(contexts))}),
            "mission": None if mission is None else self._entity(mission.mission_revision_id, mission.revision, sequence, {"outcome": mission.outcome, "boundaries": mission.boundaries, "completion_checks": mission.completion_checks, "authored_by": mission.authored_by}),
            "round": None if round_row is None else self._entity(round_row.round_id, max(1, round_row.number), sequence, {"number": round_row.number, "status": round_row.status, "frontier_hash": round_row.frontier_hash, "workspace_revision": round_row.workspace_revision}),
            "patrol_session": None if patrol is None else self._entity(patrol.session_id, patrol.revision, sequence, {"round_id": patrol.round_id, "phase": patrol.current_phase, "status": patrol.status, "safe_summary": patrol.safe_summary, "wait_reason": patrol.wait_reason, "wait_targets": patrol.wait_targets, "terminal_outcome": patrol.terminal_outcome}),
            "contexts": {row.task_id: self._entity(row.task_id, 1, sequence, {"title": row.title, "current_revision_id": row.current_revision_id, "deleted": row.deleted_at is not None, "membership_id": membership_by_context[row.task_id].membership_id, "lane_id": membership_by_context[row.task_id].lane_id, "role": membership_by_context[row.task_id].role, "status": membership_by_context[row.task_id].status, "required_barrier": membership_by_context[row.task_id].required_barrier}) for row in contexts},
            "lineage": {
                edge.entity_id: self._entity(
                    edge.entity_id,
                    max(1, edge.target_generation),
                    sequence,
                    edge.payload(),
                )
                for edge in lineage
            },
            "runs": {row.run_id: self._entity(row.run_id, 2 if row.status in {"success", "error", "interrupted"} else 1, sequence, {"context_id": row.task_id, "status": row.status, "origin": row.origin, "directive_id": row.directive_id, "user_intent_id": row.user_intent_id, "workspace_result": row.workspace_result, "model_calls": row.model_call_count, "input_tokens": row.prompt_input_tokens, "output_tokens": row.prompt_output_tokens, "dispatch": None if dispatch_by_run.get(row.run_id) is None else {"dispatch_id": dispatch_by_run[row.run_id].dispatch_id, "status": dispatch_by_run[row.run_id].status, "attempt": dispatch_by_run[row.run_id].attempt, "error": dispatch_by_run[row.run_id].error}}) for row in runs},
            "curators": {row.assignment_id: self._entity(row.assignment_id, row.revision, sequence, {"round_id": row.round_id, "scope": row.scope, "state": row.state, "safe_summary": row.result_summary, "evidence_references": row.evidence_refs}) for row in curators},
            "expansions": {row.expansion_id: self._entity(row.expansion_id, row.revision, sequence, {"opportunity_id": row.opportunity_id, "round_id": row.round_id, "source_context_id": row.source_context_id, "source_revision_id": row.source_revision_id, "state": row.state, "level": row.level, "policy_version": row.policy_version, "workspace_mode": row.workspace_mode, "independence_key": row.independence_key, "safe_summary": row.safe_summary, "blocker_code": row.blocker_code, "result": row.result}) for row in expansions},
            "directives": {row.directive_id: self._entity(row.directive_id, max(1, row.revision), sequence, {"round_id": row.round_id, "origin": row.origin_kind, "target_context_id": row.target_context_id, "state": row.lifecycle_state, "run_id": row.launched_run_id, "reason": row.terminal_reason, "correlation_id": row.correlation_id}) for row in directives},
            "facts": {row.fact_id: self._entity(row.fact_id, row.current_revision, sequence, MaterializedFactQueryService.serialize(row)) for row in facts},
            "wait_requests": {row.request_id: self._entity(row.request_id, row.revision, sequence, {"request_id": row.request_id, "round_id": row.round_id, "kind": row.kind, "prompt": row.prompt, "response_mode": row.response_mode, "response_contract": row.response_contract, "scope": row.scope, "status": row.status, "correlation_id": row.correlation_id, "causation_id": row.causation_id, "created_by": row.created_by, "created_at": row.created_at.isoformat() if row.created_at else None}) for row in wait_requests},
            "wait_responses": {row.response_id: self._entity(row.response_id, 1, sequence, {"response_id": row.response_id, "request_id": row.request_id, "actor_id": row.actor_id, "answer": row.answer, "request_revision": row.request_revision, "status": "committed", "created_at": row.created_at.isoformat() if row.created_at else None}) for row in wait_responses},
            "portfolio": None if portfolio is None else self._entity(portfolio.portfolio_revision_id, max(1, portfolio.generation), sequence, {"status": portfolio.status, "source_frontier": portfolio.source_frontier, "target_lanes": portfolio.target_lanes}),
            "diagnostics": projection.diagnostics.model_copy(update={
                "recovery_status": "degraded" if failures else "healthy",
                "degraded_scope": tuple(sorted({row.projector_name for row in failures})),
                "quarantined_units": tuple(sorted(row.unit_id for row in failures if row.status == "quarantined")),
            }),
        })

    @staticmethod
    def _entity(entity_id: str, revision: int, sequence: int, state: dict[str, Any]) -> ProjectedEntity:
        return ProjectedEntity(entity_id=entity_id, revision=max(1, revision), updated_sequence=max(1, sequence), state=state)

    @staticmethod
    def _grant(grant: LoopDelegationGrant | None) -> dict | None:
        if grant is None:
            return None
        return {"grant_id": grant.grant_id, "revision": grant.revision, "status": grant.status, "capabilities": grant.capabilities, "context_scope": grant.context_scope, "permission_scope": grant.permission_scope, "budgets": grant.budgets, "delegable_gates": grant.delegable_gates, "compression_policy": grant.compression_policy, "expires_at": grant.expires_at.isoformat() if grant.expires_at else None}

    @staticmethod
    def _usage(usage: LoopBudgetUsage | None, context_count: int) -> dict:
        if usage is None:
            return {"rounds": 0, "model_calls": 0, "input_tokens": 0, "output_tokens": 0, "retries": 0, "lanes": 0, "contexts": context_count, "no_progress_count": 0}
        return {"rounds": usage.rounds, "model_calls": usage.model_calls, "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens, "retries": usage.retries, "lanes": usage.lanes, "contexts": context_count, "no_progress_count": usage.no_progress_count}
