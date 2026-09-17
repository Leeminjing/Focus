r"""本文件对外提供 Loop Console、完整会话、事实、Context Evolution 与 workspace slot 查询路由。

输入为 Desktop 会话下的 workspace/Context/Program/Loop identity、过滤器与游标；输出为轻量 Portfolio
拓扑、分页完整会话、可追溯事实、revision graph 和执行 slot。具体工作流为路由把只读参数交给专用
query service，不修改 Context 或模型输入；自主压缩 audit 连接 gate、candidate、resolution、Run 与 revision。
示例：`app.include_router(loop_query_router)`。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from backend.app.desktop.agent_loop.models import LoopAction, LoopDecision, LoopDirective, LoopPatrolAttempt, LoopPendingDecision, LoopWorkerRequest, MessageProvenance
from backend.app.desktop.agent_loop.compression_authority.models import LoopCompressionCandidate, LoopCompressionResolution
from backend.app.desktop.agent_loop.console_query import LoopConsoleQueryService
from backend.app.desktop.agent_loop.conversation_query import ContextConversationQueryService
from backend.app.desktop.agent_loop.fact_projection import LoopFactProjectionService
from backend.app.desktop.context_curation.models import CurationLane, CurationProgram, PortfolioLaneCandidate, PortfolioRevision
from backend.app.desktop.context_evolution import ContextEvolutionQueryService, ContextRevisionNotFound, ContextRevisionReader, ContextRevisionRepository
from backend.app.desktop.context_evolution.models import ContextRevision
from backend.app.desktop.models import DesktopRun, DesktopThread
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor, WorkspaceAdoption, WorkspaceLease, WorkspaceSlot


loop_query_router = APIRouter(prefix="/desktop/api", tags=["agent-loop-observability"])


@loop_query_router.get("/agent-loops/{loop_id}/console")
async def loop_console(loop_id: str, request: Request) -> dict:
    async with request.app.state.desktop_service.session_factory() as session:
        return await LoopConsoleQueryService().read(session, loop_id)


@loop_query_router.get("/agent-loops/{loop_id}/contexts/{context_id}/conversation")
async def context_conversation(
    loop_id: str,
    context_id: str,
    request: Request,
    revision_id: str | None = None,
    before: int | None = Query(default=None, ge=0),
    limit: int = Query(default=48, ge=1, le=120),
) -> dict:
    async with request.app.state.desktop_service.session_factory() as session:
        return await ContextConversationQueryService(
            request.app.state.desktop_service.checkpointer
        ).read(
            session,
            loop_id,
            context_id,
            revision_id=revision_id,
            before=before,
            limit=limit,
        )


@loop_query_router.get("/agent-loops/{loop_id}/facts")
async def loop_facts(
    loop_id: str,
    request: Request,
    context_id: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    before: int | None = Query(default=None, ge=0),
    limit: int = Query(default=80, ge=1, le=200),
) -> dict:
    async with request.app.state.desktop_service.session_factory() as session:
        return await LoopFactProjectionService(
            request.app.state.desktop_service.checkpointer
        ).read(
            session,
            loop_id,
            context_id=context_id,
            kind=kind,
            status=status,
            before=before,
            limit=limit,
        )


@loop_query_router.get("/workspaces/{workspace_id}/context-evolution")
async def context_evolution(workspace_id: str, request: Request) -> dict:
    async with request.app.state.desktop_service.session_factory() as session:
        result = await ContextEvolutionQueryService(ContextRevisionRepository()).graph(session, workspace_id)
        return result.model_dump(mode="json")


@loop_query_router.get("/workspaces/{workspace_id}/context-tree")
async def context_first_parent_tree(workspace_id: str, request: Request) -> dict:
    async with request.app.state.desktop_service.session_factory() as session:
        result = await ContextEvolutionQueryService(ContextRevisionRepository()).first_parent_tree(
            session,
            workspace_id,
        )
        return result.model_dump(mode="json")


@loop_query_router.get("/contexts/{context_id}/revisions")
async def context_revisions(context_id: str, request: Request) -> list[dict]:
    async with request.app.state.desktop_service.session_factory() as session:
        context = await session.get(DesktopThread, context_id)
        if context is None:
            raise HTTPException(404, "Context 不存在")
        rows = list((await session.scalars(select(ContextRevision).where(ContextRevision.context_id == context_id).order_by(ContextRevision.generation))).all())
        return [{"revision_id": row.revision_id, "context_id": row.context_id, "generation": row.generation, "checkpoint_id": row.checkpoint_id, "projection_status": row.projection_status, "content_hash": row.content_hash, "origin_kind": row.origin_kind, "origin_id": row.origin_id, "current": row.revision_id == context.current_revision_id, "created_at": row.created_at.isoformat()} for row in rows]


@loop_query_router.get("/context-revisions/{revision_id}")
async def context_revision_snapshot(revision_id: str, request: Request) -> dict:
    repository = ContextRevisionRepository()
    async with request.app.state.desktop_service.session_factory() as session:
        try:
            revision = await repository.get_by_id(session, revision_id)
        except ContextRevisionNotFound:
            raise HTTPException(404, "Context revision 不存在")
        view = await ContextRevisionReader(
            repository,
            request.app.state.desktop_service.checkpointer,
        ).read(session, revision.ref, "historical")
        return view.model_dump(mode="json")


@loop_query_router.get("/curation-programs/{program_id}")
async def curation_program(program_id: str, request: Request) -> dict:
    async with request.app.state.desktop_service.session_factory() as session:
        program = await session.get(CurationProgram, program_id)
        if program is None:
            raise HTTPException(404, "Curation Program 不存在")
        lanes = list((await session.scalars(select(CurationLane).where(CurationLane.program_id == program_id).order_by(CurationLane.created_at))).all())
        portfolios = list((await session.scalars(select(PortfolioRevision).where(PortfolioRevision.program_id == program_id).order_by(PortfolioRevision.generation))).all())
        candidates = list((await session.scalars(select(PortfolioLaneCandidate).where(PortfolioLaneCandidate.portfolio_revision_id.in_([row.portfolio_revision_id for row in portfolios])))).all()) if portfolios else []
        return {"program_id": program.program_id, "control_state": program.control_state, "revision": program.revision, "current_portfolio_revision_id": program.current_portfolio_revision_id, "lanes": [{"lane_id": row.lane_id, "purpose": row.purpose, "managed_context_id": row.managed_context_id, "lifecycle": row.lifecycle, "publisher_epoch": row.publisher_epoch} for row in lanes], "portfolios": [{"portfolio_revision_id": row.portfolio_revision_id, "generation": row.generation, "frontier_hash": row.frontier_hash, "status": row.status, "source_frontier": row.source_frontier, "candidates": [{"candidate_id": item.candidate_id, "lane_id": item.lane_id, "action": item.action, "status": item.status, "target_context_id": item.target_context_id, "candidate_context_revision_id": item.candidate_context_revision_id, "source_allocation": item.source_allocation} for item in candidates if item.portfolio_revision_id == row.portfolio_revision_id]} for row in portfolios]}


@loop_query_router.get("/agent-loops/{loop_id}/audit")
async def loop_audit(loop_id: str, request: Request) -> dict:
    async with request.app.state.desktop_service.session_factory() as session:
        decisions = list((await session.scalars(select(LoopDecision).where(LoopDecision.loop_id == loop_id).order_by(LoopDecision.created_at))).all())
        actions = list((await session.scalars(select(LoopAction).where(LoopAction.loop_id == loop_id).order_by(LoopAction.position))).all())
        directives = list((await session.scalars(select(LoopDirective).where(LoopDirective.loop_id == loop_id).order_by(LoopDirective.created_at))).all())
        attempts = list((await session.scalars(select(LoopPatrolAttempt).where(LoopPatrolAttempt.loop_id == loop_id).order_by(LoopPatrolAttempt.created_at))).all())
        workers = list((await session.scalars(select(LoopWorkerRequest).where(LoopWorkerRequest.loop_id == loop_id).order_by(LoopWorkerRequest.created_at))).all())
        runs = list((await session.scalars(select(DesktopRun).where(DesktopRun.loop_id == loop_id).order_by(DesktopRun.created_at))).all())
        pending_decisions = list((await session.scalars(select(LoopPendingDecision).where(LoopPendingDecision.loop_id == loop_id).order_by(LoopPendingDecision.created_at))).all())
        candidates = list((await session.scalars(select(LoopCompressionCandidate).where(LoopCompressionCandidate.loop_id == loop_id).order_by(LoopCompressionCandidate.created_at))).all())
        resolutions = list((await session.scalars(select(LoopCompressionResolution).where(LoopCompressionResolution.loop_id == loop_id).order_by(LoopCompressionResolution.created_at))).all())
        return {
            "decisions": [{"decision_id": row.decision_id, "round_id": row.round_id, "rationale": row.rationale, "evidence": row.evidence, "status": row.status, "rejection": row.rejection} for row in decisions],
            "actions": [{"action_id": row.action_id, "decision_id": row.decision_id, "type": row.action_type, "payload": row.payload, "status": row.status, "result": row.result} for row in actions],
            "directives": [{"directive_id": row.directive_id, "round_id": row.round_id, "decision_id": row.decision_id, "action_id": row.action_id, "message_id": row.message_id, "target_context_id": row.target_context_id, "target_context_revision_id": row.target_context_revision_id, "content": row.content, "actor_kind": row.actor_kind, "actor_id": row.actor_id, "grant_id": row.grant_id, "grant_revision": row.grant_revision, "goal_revision": row.goal_revision, "status": row.status, "launched_run_id": row.launched_run_id} for row in directives],
            "patrol_attempts": [{"attempt_id": row.patrol_attempt_id, "round_id": row.round_id, "attempt": row.attempt, "status": row.status, "error": row.error} for row in attempts],
            "workers": [{"worker_request_id": row.worker_request_id, "round_id": row.round_id, "kind": row.kind, "scope": row.scope, "status": row.status, "result": row.result} for row in workers],
            "runs": [{"run_id": row.run_id, "round_id": row.round_id, "action_id": row.action_id, "directive_id": row.directive_id, "context_id": row.task_id, "context_revision_id": row.context_revision_id, "status": row.status, "workspace_anchor": row.workspace_anchor, "workspace_result": row.workspace_result, "error": row.error} for row in runs],
            "pending_decisions": [{"pending_decision_id": row.pending_decision_id, "kind": row.kind, "delegable": row.delegable, "status": row.status, "payload": row.payload} for row in pending_decisions],
            "compression_candidates": [{"candidate_id": row.candidate_id, "pending_decision_id": row.pending_decision_id, "round_id": row.round_id, "context_id": row.context_id, "base_context_revision_id": row.base_context_revision_id, "base_checkpoint_id": row.base_checkpoint_id, "source_ranges": row.normalized_ranges, "before_tokens": row.before_tokens, "after_tokens": row.after_tokens, "estimated_reduction": row.before_tokens - row.after_tokens, "protection_evidence": row.protection_evidence, "status": row.status, "expires_at": row.expires_at.isoformat()} for row in candidates],
            "compression_resolutions": [{"resolution_id": row.resolution_id, "pending_decision_id": row.pending_decision_id, "candidate_id": row.candidate_id, "decision_id": row.decision_id, "action_id": row.action_id, "run_id": row.resume_run_id, "status": row.status, "result_checkpoint_id": row.result_checkpoint_id, "result_context_revision_id": row.result_context_revision_id, "actual_before_tokens": row.actual_before_tokens, "actual_after_tokens": row.actual_after_tokens, "actual_reduction": None if row.actual_before_tokens is None or row.actual_after_tokens is None else row.actual_before_tokens - row.actual_after_tokens, "error": row.error_evidence} for row in resolutions],
        }


@loop_query_router.get("/context-revisions/{revision_id}/message-provenance")
async def message_provenance(revision_id: str, request: Request) -> list[dict]:
    async with request.app.state.desktop_service.session_factory() as session:
        rows = list((await session.scalars(select(MessageProvenance).where(MessageProvenance.context_revision_id == revision_id))).all())
        return [{"provenance_id": row.provenance_id, "message_id": row.message_id, "source_kind": row.source_kind, "actor_id": row.actor_id, "directive_id": row.directive_id, "audit": row.audit} for row in rows]


@loop_query_router.get("/workspaces/{workspace_id}/slots")
async def workspace_slots(workspace_id: str, request: Request) -> list[dict]:
    async with request.app.state.desktop_service.session_factory() as session:
        slots = list((await session.scalars(select(WorkspaceSlot).where(WorkspaceSlot.workspace_id == workspace_id).order_by(WorkspaceSlot.kind, WorkspaceSlot.created_at))).all())
        payload = []
        for slot in slots:
            lease = await session.scalar(select(WorkspaceLease).where(WorkspaceLease.slot_id == slot.slot_id, WorkspaceLease.status == "active"))
            adoption = await session.scalar(select(WorkspaceAdoption).where(WorkspaceAdoption.source_slot_id == slot.slot_id).order_by(WorkspaceAdoption.created_at.desc()).limit(1))
            anchors = list(
                (
                    await session.scalars(
                        select(RunExecutionAnchor)
                        .where(RunExecutionAnchor.slot_id == slot.slot_id)
                        .order_by(RunExecutionAnchor.created_at.desc())
                        .limit(12)
                    )
                ).all()
            )
            payload.append({"slot_id": slot.slot_id, "kind": slot.kind, "root_path": slot.root_path, "provider": slot.provider, "base_revision": slot.base_revision, "revision": slot.revision, "current_fingerprint": slot.current_fingerprint, "owner_loop_id": slot.owner_loop_id, "owner_lane_id": slot.owner_lane_id, "lifecycle": slot.lifecycle, "lease": None if lease is None else {"lease_id": lease.lease_id, "run_id": lease.run_id, "mode": lease.mode, "fencing_token": lease.fencing_token, "expires_at": lease.expires_at.isoformat()}, "adoption": None if adoption is None else {"adoption_id": adoption.adoption_id, "status": adoption.status, "evidence": adoption.evidence, "conflict": adoption.conflict, "resulting_target_revision": adoption.resulting_target_revision}, "runs": [{"run_id": anchor.run_id, "directive_id": anchor.directive_id, "observed_revision": anchor.observed_workspace_revision, "resulting_revision": anchor.resulting_workspace_revision, "effect_evidence": anchor.effect_evidence, "adoption_state": anchor.adoption_state} for anchor in anchors]})
        return payload
