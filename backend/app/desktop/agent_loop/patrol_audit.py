r"""本文件对外提供 PatrolAuditRepository 与 proposal_failure_diagnostic。

输入为 Loop identity、可选 round identity 或已解析 Patrol actions；输出为已持久 Patrol phase、Curator assignment 当前结果及各自稳定因果标识，
以及不含提案正文的 proposal_failure_diagnostic。失败动作保留类型及等待 cause/证据种类/版本；未受信引用和所需输入只保存哈希与长度。
具体工作流为读取不可变 phase history，再按 Session 批量装配 Curator scope、状态、evidence 和安全摘要，不依赖 live 连接
或 prose message 重建，失败诊断从原 typed action 投影，不依赖截断的原文前缀、不调用模型或更改判定。
示例：`records = await repository.round_history(session, loop_id, round_id)`；`diagnostic = proposal_failure_diagnostic(proposal.actions)`。
"""

from __future__ import annotations

from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.patrol_session_models import LoopCuratorAssignment, LoopPatrolSession
from backend.app.desktop.agent_loop.patrol_session_repository import PatrolSessionRepository
from backend.app.desktop.agent_loop.schemas import PatrolModelAction, WaitForUserAction


def _text_identity(value: str) -> dict:
    return {"sha256": sha256(value.encode("utf-8")).hexdigest(), "chars": len(value)}


def proposal_failure_diagnostic(actions: tuple[PatrolModelAction, ...]) -> dict:
    return {
        "version": "patrol-proposal-diagnostic-v1",
        "action_types": tuple(action.action for action in actions),
        "wait_requests": tuple(
            {
                "position": position,
                "cause": action.cause,
                "evidence_kind": action.evidence_identity.kind if action.evidence_identity else None,
                "evidence_revision": action.evidence_identity.revision if action.evidence_identity else None,
                "reference_identity": _text_identity(action.evidence_identity.reference_id) if action.evidence_identity else None,
                "required_input_identity": _text_identity(action.required_input) if action.required_input else None,
            }
            for position, action in enumerate(actions)
            if isinstance(action, WaitForUserAction)
        ),
    }


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
