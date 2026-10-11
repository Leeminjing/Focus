"""本文件对外提供 CausalSnapshotReader 与 read_causal_snapshot 的输入时点证据读取。

输入为调用方一致事务、Loop、客户端可见引用或冻结因果条目；输出为完整活动身份、当时事实修订和可核验 Oi 正文。
具体工作流为读取权威实体及当时物化事实，分别核验客户端位置，保留投影缺口；历史读取仅按 Observation identity/hash 解引用。
不创建 Round、推进投影或读取实时输出。示例：await CausalSnapshotReader().capture(session, loop, visible_context)。
"""

from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import exists, or_, select

from backend.app.desktop.agent_loop.fact_models import LoopFact, LoopFactRevision
from backend.app.desktop.agent_loop.journal_models import LoopJournalSequence, LoopProjectorCursor, LoopReplayRetention
from backend.app.desktop.agent_loop.models import AgentLoop, LoopAction, LoopDecision, LoopDirective, LoopObservation, LoopRound, LoopUserIntent, LoopWorkerRequest
from backend.app.desktop.agent_loop.projection_models import LoopProjectionFailure
from backend.app.desktop.agent_loop.task_progress.contracts import canonical_hash
from backend.app.desktop.context_curation.models import PortfolioRevision
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.run_orchestration.models import RunDispatch
from backend.app.desktop.workspace_coordination.models import WorkspaceAdoption


class CausalSnapshotReader:
    async def capture(self, session, loop, visible_context=None):
        sequence = await session.get(LoopJournalSequence, loop.loop_id)
        boundary = int(sequence.last_sequence if sequence else 0)
        live_runs = exists(select(DesktopRun.run_id).where(DesktopRun.round_id == LoopRound.round_id,
            or_(DesktopRun.settled_at.is_(None), DesktopRun.status.in_(("pending", "running")))))
        live_workers = exists(select(LoopWorkerRequest.worker_request_id).where(
            LoopWorkerRequest.round_id == LoopRound.round_id, LoopWorkerRequest.completed_at.is_(None)))
        rounds = tuple(await session.scalars(select(LoopRound).where(LoopRound.loop_id == loop.loop_id,
            or_(LoopRound.status != "settled", live_runs, live_workers)).order_by(LoopRound.number)))
        self._complete(rounds)
        round_ids = [row.round_id for row in rounds]
        runs = tuple(await session.scalars(select(DesktopRun).where(DesktopRun.loop_id == loop.loop_id,
            DesktopRun.round_id.in_(round_ids)).order_by(DesktopRun.created_at, DesktopRun.run_id)))
        directives = tuple(await session.scalars(select(LoopDirective).where(LoopDirective.round_id.in_(round_ids))))
        workers = tuple(await session.scalars(select(LoopWorkerRequest).where(LoopWorkerRequest.round_id.in_(round_ids))))
        dispatches = tuple(await session.scalars(select(RunDispatch).where(RunDispatch.run_id.in_([row.run_id for row in runs]))))
        decisions = tuple(await session.scalars(select(LoopDecision).where(
            LoopDecision.decision_id.in_([row.decision_id for row in rounds if row.decision_id]))))
        actions = tuple(await session.scalars(select(LoopAction).where(LoopAction.decision_id.in_([row.decision_id for row in decisions]))))
        publications = tuple(await session.scalars(select(PortfolioRevision).where(PortfolioRevision.portfolio_revision_id.in_(
            [row.result["portfolio_revision_id"] for row in actions if row.result.get("portfolio_revision_id")]))))
        adoptions = tuple(await session.scalars(select(WorkspaceAdoption).where(WorkspaceAdoption.adoption_id.in_(
            [row.action_id for row in actions if row.action_type == "adopt_workspace_result"]))))
        facts = tuple(await session.scalars(select(LoopFact).where(LoopFact.loop_id == loop.loop_id).order_by(LoopFact.fact_id)))
        for collection in (runs, directives, workers, dispatches, actions, publications, adoptions, facts):
            self._complete(collection)
        current_observations = {row.observation_id: row for row in await session.scalars(
            select(LoopObservation).where(LoopObservation.observation_id.in_([row.observation_id for row in rounds if row.observation_id])))}
        decision_views = {row.decision_id: {**self._view(row, ("decision_id", "status", "observation_id", "authority_committed_at")),
            "intent": row.intent, "actions": [self._view(action, ("action_id", "action_type", "payload", "status", "result"))
                for action in actions if action.decision_id == row.decision_id]} for row in decisions}
        permission_modes = {row.decision_id: {item["context_id"]: item.get("access_mode")
            for item in row.intent.get("context_permissions", ())} for row in decisions}
        action_positions = {row.action_id: row.position for row in actions}
        cursor = await session.get(LoopProjectorCursor, (loop.loop_id, "loop_facts"))
        failures = tuple(await session.scalars(select(LoopProjectionFailure).where(
            LoopProjectionFailure.loop_id == loop.loop_id, LoopProjectionFailure.projector_name == "loop_facts",
            LoopProjectionFailure.status != "resolved").order_by(LoopProjectionFailure.failure_id)))
        self._complete(failures)
        return {
            "server_sequence": boundary,
            "rounds": [{**self._view(row, ("round_id", "number", "trigger_kind", "status", "observation_id", "decision_id", "settled_at")),
                "observation_hash": current_observations[row.observation_id].envelope_hash if row.observation_id in current_observations else None,
                "generation": current_observations[row.observation_id].generation if row.observation_id in current_observations else 0,
                "decision": decision_views.get(row.decision_id)} for row in rounds],
            "runs": [{**self._view(row, ("run_id", "round_id", "directive_id", "status", "settled_at", "context_revision_id", "workspace_slot_id", "workspace_anchor", "workspace_result")),
                "context_id": row.task_id, "permissions": (row.equipment or {}).get("permissions", []),
                "access_mode": (row.equipment or {}).get("access_mode"), "authority": (row.equipment or {}).get("round_authorization")} for row in runs],
            "directives": [{**self._view(row, ("directive_id", "round_id", "decision_id", "action_id", "target_context_id", "target_context_revision_id", "grant_id", "grant_revision", "goal_revision", "lifecycle_state", "launched_run_id", "content")),
                "access_mode": permission_modes.get(row.decision_id, {}).get(row.target_context_id,
                    permission_modes.get(row.decision_id, {}).get(f"action:{action_positions.get(row.action_id)}"))} for row in directives],
            "workers": [self._view(row, ("worker_request_id", "round_id", "observation_id", "observation_generation", "kind", "status", "completed_at")) for row in workers],
            "dispatches": [self._view(row, ("dispatch_id", "run_id", "status", "attempt", "settled_at")) for row in dispatches],
            "publications": [self._view(row, ("portfolio_revision_id", "status", "program_id")) for row in publications],
            "adoptions": [self._view(row, ("adoption_id", "status", "source_slot_id", "source_revision", "expected_target_revision", "resulting_target_revision", "conflict")) for row in adoptions],
            "facts": [{**self._view(row, ("fact_id", "state", "fact_type", "source_context_id", "source_run_id", "presentation", "evidence", "observer", "verifier")), "revision": row.current_revision} for row in facts],
            "projection": {"cursor": int(cursor.last_sequence if cursor else 0),
                "lag": max(0, boundary - int(cursor.last_sequence if cursor else 0)),
                "failures": [self._view(row, ("failure_id", "unit_kind", "unit_id", "status", "boundary_sequence", "error_class")) for row in failures]},
            "visible_context": await self._visible(session, loop.loop_id, boundary, visible_context),
        }

    @staticmethod
    def _view(row, fields):
        return {key: value.isoformat() if isinstance(value, datetime) else value
            for key in fields if (value := getattr(row, key, None)) is not None}

    @staticmethod
    def _complete(rows):
        if len(rows) > 4096:
            raise ValueError("intervention_snapshot_budget_exceeded: 证据身份未完整冻结")

    @staticmethod
    async def _visible(session, loop_id, boundary, visible):
        if visible is None:
            return {"status": "unknown", "live_sequence": None, "fact_refs": [], "run_previews": []}
        if visible.loop_id != loop_id:
            raise HTTPException(409, "可见上下文不属于当前 Loop")
        if visible.live_sequence is not None and visible.live_sequence > boundary:
            raise HTTPException(409, "可见事件位置尚未进入受理视图")
        for reference in visible.fact_refs:
            revision = await session.scalar(select(LoopFactRevision).where(LoopFactRevision.loop_id == loop_id,
                LoopFactRevision.fact_id == reference.fact_id, LoopFactRevision.revision == reference.revision))
            if revision is None:
                raise HTTPException(409, "可见事实修订不存在或不属于当前 Loop")
        for preview in visible.run_previews:
            run = await session.get(DesktopRun, preview.run_id)
            if run is None or run.loop_id != loop_id or run.task_id != preview.context_id:
                raise HTTPException(409, "运行预览身份不属于当前 Loop/Context")
        retention = await session.get(LoopReplayRetention, loop_id)
        status = "unknown" if visible.live_sequence is None else (
            "expired" if retention and visible.live_sequence < retention.minimum_sequence - 1 else "declared")
        return {**visible.model_dump(mode="json"), "status": status, "preview_provenance": "client_reported"}


async def read_causal_snapshot(session, loop_id, entry):
    if not entry.get("observation_id"):
        raise ValueError("causal_history_unavailable: 历史输入未保存独立快照")
    observation = await session.get(LoopObservation, entry["observation_id"])
    intent = await session.get(LoopUserIntent, entry["intent_id"])
    if (observation is None or intent is None or intent.introduced_observation_id != observation.observation_id
            or intent.snapshot_hash != entry["snapshot_hash"] or intent.loop_id != observation.loop_id):
        raise ValueError("因果快照与原输入引用不一致")
    if observation.loop_id != loop_id:
        current, origin = await session.get(AgentLoop, loop_id), await session.get(AgentLoop, observation.loop_id)
        if not (current and origin and current.workspace_id == origin.workspace_id
                and current.interaction_mode == origin.interaction_mode == "workspace_patrol"
                and origin.status in {"completed", "stopped", "failed"}):
            raise ValueError("因果快照不属于当前工作区输入历史")
    if entry.get("generation") != observation.generation:
        raise ValueError("因果快照不属于当前 Loop")
    state = observation.envelope.get("pre_input_state")
    if (state is None or canonical_hash(state) != entry["snapshot_hash"]
            or observation.envelope.get("pre_input_hash") != entry["snapshot_hash"]):
        raise ValueError("因果快照 hash 不一致")
    return state
