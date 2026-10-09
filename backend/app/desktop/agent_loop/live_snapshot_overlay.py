r"""本文件对外提供 LoopLiveProjectionOverlay。

输入为 journal 投影、单一 sequence 边界和当前物化领域表；输出为补齐 Loop、Mission、Wait、Context/Run、Context 派生边、Curator、Expansion、Directive、Fact、恢复诊断与 Portfolio 的完整 snapshot。
具体工作流为批量读取各 current entity，按统一 ProjectedEntity 信封归并，并附加渲染所需授权与预算字段；派生边由
ContextLineageResolver 从权威 revision 来源解析，作为客户端在快照边界上的基线；公开事实集合排除历史 tool rows。
事务轮次与预留/未知用量由共享 AccountingQuery 读取，并提供独立 accounting 实体，使消费事件不改变控制版本；Loop 和 Round 沿用同一实体的 journal 版本，尚未发布事件时使用初始版本一，控制版本和轮号不代替实体版本；Run 的业务终态和结算清理状态分别公开。
示例：`await overlay.apply(session, projection, boundary)`。
interventions 从耐久用户消息表补齐类型、来源、交付状态和 Run 身份，快照重启不丢失接受提交后的状态。
Round 快照公开真实 Observation/Decision 身份，accounting 历史诊断不回写旧事务。
Mission交付调用与兼容快照/Directive事件相同的只读评估；重连使用当前事实，旧journal不被补写。
task_progress 从既有持久 head 读取并独立补齐；Loop 显式提供交互模式，用户受理不构造任务结果。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.context_expansion.models import LoopContextExpansion
from backend.app.desktop.agent_loop.fact_models import LoopFact
from backend.app.desktop.agent_loop.live_projection_contract import (
    LoopLiveProjection,
    ProjectedEntity,
)
from backend.app.desktop.agent_loop.materialized_fact_query import (
    MaterializedFactQueryService,
)
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.mission_bootstrap import MissionBootstrapStage
from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopBudgetUsage,
    LoopContextMembership,
    LoopDelegationGrant,
    LoopDirective,
    LoopRound,
    LoopUserIntent,
)
from backend.app.desktop.agent_loop.patrol_session_models import (
    LoopCuratorAssignment,
    LoopPatrolSession,
)
from backend.app.desktop.agent_loop.projection_models import LoopProjectionFailure
from backend.app.desktop.agent_loop.wait_models import LoopWaitRequest, LoopWaitResponse
from backend.app.desktop.context_curation.models import PortfolioRevision
from backend.app.desktop.context_evolution.lineage import (
    ContextLineageResolver,
)
from backend.app.desktop.models import DesktopRun, DesktopThread
from backend.app.desktop.run_orchestration.models import RunDispatch
from backend.app.desktop.agent_loop.accounting_query import LoopAccountingQuery


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
        interventions = tuple((await session.scalars(select(LoopUserIntent).where(
            LoopUserIntent.loop_id == projection.loop_id).order_by(LoopUserIntent.created_at.desc()).limit(200))).all())
        facts = tuple((await session.scalars(select(LoopFact).where(LoopFact.loop_id == projection.loop_id, LoopFact.fact_type != "tool"))).all())
        wait_requests = tuple((await session.scalars(select(LoopWaitRequest).where(LoopWaitRequest.loop_id == projection.loop_id).order_by(LoopWaitRequest.created_at.desc()).limit(50))).all())
        wait_request_ids = tuple(row.request_id for row in wait_requests)
        wait_responses = () if not wait_request_ids else tuple((await session.scalars(select(LoopWaitResponse).where(LoopWaitResponse.request_id.in_(wait_request_ids)))).all())
        failures = tuple((await session.scalars(select(LoopProjectionFailure).where(LoopProjectionFailure.loop_id == projection.loop_id, LoopProjectionFailure.status != "resolved"))).all())
        grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == projection.loop_id).order_by(LoopDelegationGrant.revision.desc()).limit(1))
        usage = await session.get(LoopBudgetUsage, projection.loop_id)
        portfolio = await session.get(PortfolioRevision, loop.current_portfolio_revision_id) if loop.current_portfolio_revision_id else None
        accounting = await LoopAccountingQuery().read(session, loop.loop_id)
        delivery = await MissionBootstrapStage().assess(session, loop, round_row) if round_row is not None else None
        lineage = await self._lineage.resolve(
            session,
            {row.task_id: row.current_revision_id for row in contexts if row.current_revision_id},
        )
        from backend.app.desktop.agent_loop.task_progress.repository import TaskProgressRepository
        progress = await TaskProgressRepository().current(session, loop.loop_id)
        return projection.model_copy(update={
            "task_progress": None if progress is None else self._entity(loop.loop_id, progress.generation + 1, sequence,
                {"progress_id": progress.progress_id, "generation": progress.generation,
                 "content_hash": progress.content_hash, "document": progress.document}),
            "accounting": self._entity(loop.loop_id, projection.accounting.revision if projection.accounting else 1,
                sequence, {"accounting": accounting, "usage": {**self._usage(usage, len(contexts)), "rounds": accounting["completed_rounds"]}}),
            "loop": self._entity(loop.loop_id, projection.loop.revision if projection.loop is not None else 1, sequence, {"loop_id": loop.loop_id, "workspace_id": loop.workspace_id, "interaction_mode": loop.interaction_mode, "initial_context_id": loop.initial_context_id, "program_id": loop.program_id, "status": loop.status, "health": loop.health, "revision": loop.revision, "authority_revision": loop.authority_revision, "goal_revision": loop.goal_revision, "active_mission_revision": loop.goal_revision, "current_round_id": loop.current_round_id, "current_portfolio_revision_id": loop.current_portfolio_revision_id, "waiting_reason": loop.waiting_reason, "equipment": loop.equipment, "final_result": loop.final_result, "grant": self._grant(grant), "usage": {**self._usage(usage, len(contexts)), "rounds": accounting["completed_rounds"]}, "accounting": accounting, "mission_delivery": None if delivery is None else delivery.to_payload()}),
            "mission": None if mission is None else self._entity(mission.mission_revision_id, mission.revision, sequence, {"outcome": mission.outcome, "boundaries": mission.boundaries, "completion_checks": mission.completion_checks, "authored_by": mission.authored_by}),
            "round": None if round_row is None else self._entity(round_row.round_id, projection.round.revision if projection.round is not None and projection.round.entity_id == round_row.round_id else 1, sequence, {"round_id": round_row.round_id, "number": round_row.number, "status": round_row.status, "observation_id": round_row.observation_id, "decision_id": round_row.decision_id, "barrier": round_row.barrier, "frontier_hash": round_row.frontier_hash, "workspace_revision": round_row.workspace_revision}),
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
            "runs": {row.run_id: self._run_entity(row, dispatch_by_run.get(row.run_id), sequence, projection.runs.get(row.run_id)) for row in runs},
            "interventions": {row.intent_id: self._entity(row.intent_id, row.revision, sequence, {
                "intent_kind": row.intent_kind, "origin": row.origin_kind, "target_context_id": row.target_context_id,
                "state": row.delivery_state, "status": row.status, "run_id": row.resulting_run_id,
                "correlation_id": row.correlation_id, "reason": row.terminal_reason}) for row in interventions},
            "curators": {row.assignment_id: self._entity(row.assignment_id, row.revision, sequence, {"round_id": row.round_id, "scope": row.scope, "state": row.state, "safe_summary": row.result_summary, "evidence_references": row.evidence_refs}) for row in curators},
            "expansions": {row.expansion_id: self._entity(row.expansion_id, row.revision, sequence, {"opportunity_id": row.opportunity_id, "round_id": row.round_id, "work_spec": row.work_spec, "manifest_ids": row.manifest_ids, "source_frontier": row.source_frontier, "resolution": row.resolution, "evidence_frontier": row.evidence_frontier, "stage_identities": row.stage_identities, "definition_hash": row.definition_hash, "state": row.state, "level": row.level, "policy_version": row.policy_version, "workspace_mode": row.workspace_mode, "independence_key": row.independence_key, "safe_summary": row.safe_summary, "blocker_code": row.blocker_code, "result": row.result}) for row in expansions},
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
    def _run_entity(run: DesktopRun, dispatch: RunDispatch | None, sequence: int, prior: ProjectedEntity | None) -> ProjectedEntity:
        state = {
            "context_id": run.task_id,
            "status": run.status,
            "origin": run.origin,
            "directive_id": run.directive_id,
            "user_intent_id": run.user_intent_id,
            "parent_run_id": run.parent_run_id,
            "round_id": run.round_id,
            "settlement_status": "settled" if run.settled_at else "pending",
            "workspace_result": run.workspace_result,
            "model_calls": run.model_call_count,
            "input_tokens": run.prompt_input_tokens,
            "output_tokens": run.prompt_output_tokens,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "dispatch": None if dispatch is None else {
                "dispatch_id": dispatch.dispatch_id,
                "status": dispatch.status,
                "attempt": dispatch.attempt,
                "error": dispatch.error,
            },
            "last_activity_at": None if prior is None else prior.state.get("last_activity_at"),
            "last_activity_kind": None if prior is None else prior.state.get("last_activity_kind"),
            "last_activity_summary": None if prior is None else prior.state.get("last_activity_summary"),
        }
        return LoopLiveProjectionOverlay._entity(
            run.run_id, 2 if run.status in {"success", "error", "interrupted"} else 1, sequence, state
        )

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
