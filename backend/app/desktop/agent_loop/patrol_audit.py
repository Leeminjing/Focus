r"""本文件对外提供 PatrolAuditRepository。

输入为 Loop identity 与可选 round identity；输出为已持久 Patrol phase、Curator assignment 当前结果及各自稳定因果标识。
具体工作流为读取不可变 phase history，再按 Session 批量装配 Curator scope、状态、evidence 和安全摘要，不依赖 live 连接
或 prose message 重建。示例：`records = await repository.round_history(session, loop_id, round_id)`。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.patrol_session_models import LoopCuratorAssignment, LoopPatrolSession
from backend.app.desktop.agent_loop.patrol_session_repository import PatrolSessionRepository


class PatrolAuditRepository:
    def __init__(self) -> None:
        self._sessions = PatrolSessionRepository()

    async def round_history(self, session: AsyncSession, loop_id: str, round_id: str) -> dict:
        patrol = await session.scalar(
            select(LoopPatrolSession).where(
                LoopPatrolSession.loop_id == loop_id,
                LoopPatrolSession.round_id == round_id,
            )
        )
        phases = await self._sessions.history(session, loop_id, round_id)
        if patrol is None:
            return {"session": None, "phases": phases, "curators": ()}
        curators = tuple(
            (
                await session.scalars(
                    select(LoopCuratorAssignment)
                    .where(LoopCuratorAssignment.session_id == patrol.session_id)
                    .order_by(LoopCuratorAssignment.created_at, LoopCuratorAssignment.assignment_id)
                )
            ).all()
        )
        return {
            "session": {
                "session_id": patrol.session_id,
                "round_id": patrol.round_id,
                "status": patrol.status,
                "phase": patrol.current_phase,
                "terminal_outcome": patrol.terminal_outcome,
            },
            "phases": phases,
            "curators": tuple(
                {
                    "assignment_id": item.assignment_id,
                    "worker_request_id": item.worker_request_id,
                    "state": item.state,
                    "scope": item.scope,
                    "evidence_refs": item.evidence_refs,
                    "result_summary": item.result_summary,
                    "failure": item.failure,
                }
                for item in curators
            ),
        }
