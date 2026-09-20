r"""本文件对外提供 LoopConsoleQueryService 的轻量 Context Portfolio 读模型。

输入为 Loop id 与只读 AsyncSession；输出为 Context identity 节点、当前 revision 来源边、Lane 主题、
最新 Run、事实计数、当前 Mission revision、压缩 resolution 状态、待处理用户意见，以及 Loop 的等待原因与当前 round 终态。
具体工作流为批量读取权威表后在内存按 id 归并，不加载完整消息历史，从而让拓扑图可高频刷新，并让"运行中却
零进展"的停顿可被控制台解释。示例：`await service.read(session, loop_id)`。
"""

from __future__ import annotations

from collections import defaultdict

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopContextMembership,
    LoopDirective,
    LoopRound,
    LoopUserIntent,
)
from backend.app.desktop.agent_loop.fact_sources import LoopFactBuilder
from backend.app.desktop.agent_loop.compression_authority.models import LoopCompressionCandidate, LoopCompressionResolution
from backend.app.desktop.context_curation.models import CurationLane
from backend.app.desktop.context_evolution.models import ContextRevision, ContextRevisionSource
from backend.app.desktop.models import DesktopRun, DesktopThread
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor


class LoopConsoleQueryService:
    async def read(self, session: AsyncSession, loop_id: str) -> dict:
        loop = await session.get(AgentLoop, loop_id)
        if loop is None:
            raise HTTPException(404, "Agent Loop 不存在")
        current_round = await session.get(LoopRound, loop.current_round_id) if loop.current_round_id else None
        memberships = list(
            (
                await session.scalars(
                    select(LoopContextMembership)
                    .where(LoopContextMembership.loop_id == loop_id)
                    .order_by(LoopContextMembership.created_at)
                )
            ).all()
        )
        context_ids = [row.context_id for row in memberships]
        contexts = await self._contexts(session, context_ids)
        lanes = await self._lanes(session, [row.lane_id for row in memberships if row.lane_id])
        revisions = await self._revisions(session, contexts)
        sources = await self._sources(session, [row.revision_id for row in revisions.values()])
        latest_runs, run_counts, evidence_counts = await self._runs(session, loop_id)
        directive_counts = await self._directive_counts(session, loop_id)
        intents = list(
            (
                await session.scalars(
                    select(LoopUserIntent)
                    .where(LoopUserIntent.loop_id == loop_id)
                    .order_by(LoopUserIntent.created_at.desc())
                    .limit(40)
                )
            ).all()
        )
        compression_candidates = list(
            (await session.scalars(select(LoopCompressionCandidate).where(LoopCompressionCandidate.loop_id == loop_id).order_by(LoopCompressionCandidate.created_at.desc()).limit(8))).all()
        )
        compression_resolutions = list(
            (await session.scalars(select(LoopCompressionResolution).where(LoopCompressionResolution.loop_id == loop_id).order_by(LoopCompressionResolution.created_at.desc()).limit(8))).all()
        )
        nodes = []
        for membership in memberships:
            context = contexts.get(membership.context_id)
            revision = revisions.get(membership.context_id)
            lane = lanes.get(membership.lane_id)
            run = latest_runs.get(membership.context_id)
            nodes.append(
                {
                    "context_id": membership.context_id,
                    "membership_id": membership.membership_id,
                    "title": context.title if context else membership.context_id,
                    "topic": lane.purpose if lane else (context.title if context else membership.role),
                    "purpose": lane.purpose if lane else membership.role.replace("_", " ").title(),
                    "role": membership.role,
                    "status": membership.status,
                    "required_barrier": membership.required_barrier,
                    "lane_id": membership.lane_id,
                    "lane_lifecycle": lane.lifecycle if lane else None,
                    "revision": None
                    if revision is None
                    else {
                        "revision_id": revision.revision_id,
                        "generation": revision.generation,
                        "projection_status": revision.projection_status,
                        "origin_kind": revision.origin_kind,
                        "created_at": revision.created_at.isoformat(),
                    },
                    "latest_run": None
                    if run is None
                    else {
                        "run_id": run.run_id,
                        "status": run.status,
                        "origin": run.origin,
                        "round_id": run.round_id,
                        "model_calls": run.model_call_count,
                        "input_tokens": run.prompt_input_tokens,
                        "output_tokens": run.prompt_output_tokens,
                        "created_at": run.created_at.isoformat(),
                        "settled_at": run.settled_at.isoformat() if run.settled_at else None,
                    },
                    "counts": {
                        "runs": run_counts.get(membership.context_id, 0),
                        "delegated_messages": directive_counts.get(membership.context_id, 0),
                        "workspace_changes": evidence_counts.get(membership.context_id, {}).get("workspace_changes", 0),
                        "artifacts": evidence_counts.get(membership.context_id, {}).get("artifacts", 0),
                    },
                }
            )
        edges = [
            {
                "source_context_id": row.source_context_id,
                "source_revision_id": row.source_revision_id,
                "target_context_id": self._target_context(revisions, row.target_revision_id),
                "target_revision_id": row.target_revision_id,
                "position": row.position,
            }
            for row in sources
        ]
        return {
            "loop_id": loop.loop_id,
            "loop_revision": loop.revision,
            "mission_revision": loop.goal_revision,
            "status": loop.status,
            "health": loop.health,
            "waiting_reason": loop.waiting_reason,
            "current_round_id": loop.current_round_id,
            "current_round": None if current_round is None else {
                "round_id": current_round.round_id,
                "number": current_round.number,
                "status": current_round.status,
                "decision_id": current_round.decision_id,
                "settled_at": current_round.settled_at.isoformat() if current_round.settled_at else None,
            },
            "current_portfolio_revision_id": loop.current_portfolio_revision_id,
            "initial_context_id": loop.initial_context_id,
            "compression": {
                "candidate": None if not compression_candidates else {
                    "candidate_id": compression_candidates[0].candidate_id,
                    "context_id": compression_candidates[0].context_id,
                    "status": compression_candidates[0].status,
                    "before_tokens": compression_candidates[0].before_tokens,
                    "after_tokens": compression_candidates[0].after_tokens,
                },
                "resolution": None if not compression_resolutions else {
                    "resolution_id": compression_resolutions[0].resolution_id,
                    "status": compression_resolutions[0].status,
                    "run_id": compression_resolutions[0].resume_run_id,
                    "result_context_revision_id": compression_resolutions[0].result_context_revision_id,
                    "actual_before_tokens": compression_resolutions[0].actual_before_tokens,
                    "actual_after_tokens": compression_resolutions[0].actual_after_tokens,
                    "error": compression_resolutions[0].error_evidence,
                },
            },
            "nodes": nodes,
            "edges": edges,
            "user_intents": [
                {
                    "intent_id": row.intent_id,
                    "scope": row.scope,
                    "context_id": row.target_context_id,
                    "content": row.content,
                    "status": row.status,
                    "observed_round_id": row.observed_round_id,
                    "created_at": row.created_at.isoformat(),
                }
                for row in intents
            ],
        }

    @staticmethod
    async def _contexts(session: AsyncSession, context_ids: list[str]) -> dict[str, DesktopThread]:
        if not context_ids:
            return {}
        rows = list((await session.scalars(select(DesktopThread).where(DesktopThread.task_id.in_(context_ids)))).all())
        return {row.task_id: row for row in rows}

    @staticmethod
    async def _lanes(session: AsyncSession, lane_ids: list[str]) -> dict[str, CurationLane]:
        if not lane_ids:
            return {}
        rows = list((await session.scalars(select(CurationLane).where(CurationLane.lane_id.in_(lane_ids)))).all())
        return {row.lane_id: row for row in rows}

    @staticmethod
    async def _revisions(
        session: AsyncSession,
        contexts: dict[str, DesktopThread],
    ) -> dict[str, ContextRevision]:
        revision_ids = [row.current_revision_id for row in contexts.values() if row.current_revision_id]
        if not revision_ids:
            return {}
        rows = list(
            (await session.scalars(select(ContextRevision).where(ContextRevision.revision_id.in_(revision_ids)))).all()
        )
        return {row.context_id: row for row in rows}

    @staticmethod
    async def _sources(session: AsyncSession, revision_ids: list[str]) -> list[ContextRevisionSource]:
        if not revision_ids:
            return []
        return list(
            (
                await session.scalars(
                    select(ContextRevisionSource)
                    .where(ContextRevisionSource.target_revision_id.in_(revision_ids))
                    .order_by(ContextRevisionSource.target_revision_id, ContextRevisionSource.position)
                )
            ).all()
        )

    @staticmethod
    async def _runs(
        session: AsyncSession,
        loop_id: str,
    ) -> tuple[dict[str, DesktopRun], dict[str, int], dict[str, dict[str, int]]]:
        rows = list(
            (
                await session.scalars(
                    select(DesktopRun)
                    .where(DesktopRun.loop_id == loop_id)
                    .order_by(DesktopRun.created_at.desc(), DesktopRun.run_id.desc())
                )
            ).all()
        )
        latest: dict[str, DesktopRun] = {}
        counts: dict[str, int] = defaultdict(int)
        evidence: dict[str, dict[str, int]] = defaultdict(
            lambda: {"workspace_changes": 0, "artifacts": 0}
        )
        anchors = {
            row.run_id: row
            for row in (
                await session.scalars(
                    select(RunExecutionAnchor).where(
                        RunExecutionAnchor.run_id.in_([row.run_id for row in rows])
                    )
                )
            ).all()
        } if rows else {}
        for row in rows:
            latest.setdefault(row.task_id, row)
            counts[row.task_id] += 1
            anchor = anchors.get(row.run_id)
            effects = list(anchor.effect_evidence or []) if anchor is not None else list(
                (row.workspace_result or {}).get("effect_evidence") or []
            )
            if any(isinstance(item, dict) and item.get("changed") is True for item in effects):
                evidence[row.task_id]["workspace_changes"] += 1
            evidence[row.task_id]["artifacts"] += len(
                LoopFactBuilder.artifacts(dict(row.workspace_result or {}), effects)
            )
        return latest, dict(counts), dict(evidence)

    @staticmethod
    async def _directive_counts(session: AsyncSession, loop_id: str) -> dict[str, int]:
        rows = list(
            (await session.scalars(select(LoopDirective).where(LoopDirective.loop_id == loop_id))).all()
        )
        counts: dict[str, int] = defaultdict(int)
        for row in rows:
            counts[row.target_context_id] += 1
        return dict(counts)

    @staticmethod
    def _target_context(revisions: dict[str, ContextRevision], revision_id: str) -> str | None:
        return next(
            (context_id for context_id, revision in revisions.items() if revision.revision_id == revision_id),
            None,
        )
