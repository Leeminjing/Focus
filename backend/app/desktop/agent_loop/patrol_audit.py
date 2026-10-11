r"""本文件对外提供 PatrolAuditRepository 与 proposal_failure_diagnostic。

输入为 Loop/round identity、可选精确 Observation identity 或已解析 Patrol actions；输出为所选版本的已持久 Patrol phase、Curator assignment 结果及各自稳定因果标识，
以及不含提案正文的 proposal_failure_diagnostic。失败动作保留类型及等待 cause/证据种类/版本；未受信引用和所需输入只保存哈希与长度。
具体工作流为缺省选择 Round 当前 Observation，指定版本时校验 Loop/round 归属，再读取同一 Session 的不可变 phase history 和 Curator scope、状态、evidence、安全摘要；尚未创建当前 Session 时返回空记录，不回退旧版本，不依赖 live 连接
或 prose message 重建，失败诊断从原 typed action 投影，不依赖截断的原文前缀、不调用模型或更改判定。
示例：`records = await repository.round_history(session, loop_id, round_id)`；`diagnostic = proposal_failure_diagnostic(proposal.actions)`。
"""

from __future__ import annotations

from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.patrol_session_models import LoopCuratorAssignment, LoopPatrolSession
from backend.app.desktop.agent_loop.models import LoopObservation, LoopRound
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

    async def round_history(self, session: AsyncSession, loop_id: str, round_id: str, *, observation_id: str | None = None) -> dict:
        round_row = await session.get(LoopRound, round_id, populate_existing=True)
        if round_row is None or round_row.loop_id != loop_id:
            raise LookupError("Patrol 审计 Round 不属于指定 Loop")
        selected_id = observation_id if observation_id is not None else round_row.observation_id
        if selected_id is None:
            return {"session": None, "phases": (), "curators": ()}
        observation = await session.get(LoopObservation, selected_id)
        if observation is None or observation.loop_id != loop_id or observation.round_id != round_id:
            raise LookupError("Patrol 审计 Observation 不属于指定 Round")
        patrol = await session.scalar(
            select(LoopPatrolSession).where(
                LoopPatrolSession.loop_id == loop_id,
                LoopPatrolSession.round_id == round_id,
                LoopPatrolSession.observation_id == selected_id,
            )
        )
        if patrol is None:
            return {"session": None, "phases": (), "curators": ()}
        phases = await self._sessions.history(session, loop_id, round_id, session_id=patrol.session_id)
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
                "observation_id": patrol.observation_id,
                "observation_generation": patrol.observation_generation,
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
