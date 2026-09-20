r"""本文件对外提供 ActivationEligibility 与 LoopActivationEligibilityResolver。

输入为锁定事务、Context id 和可选 readiness token；输出为唯一最新直接用户 Main Run、前置 Loop 冲突与稳定一致性 token。
具体工作流为只读取最新直接用户候选，绝不回退旧 Run，再检查其绑定和该 Context 的非终态 Loop，创建事务复用同一解析结果。
示例：`eligibility = await resolver.resolve(session, context_id, lock=True)`。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.models import AgentLoop
from backend.app.desktop.models import DesktopRun


NONTERMINAL_LOOP_STATES = ("draft", "running", "pausing", "paused", "waiting_user", "completing", "stopping")
ELIGIBLE_RUN_STATES = ("pending", "running", "success", "error", "interrupted")


@dataclass(frozen=True, slots=True)
class ActivationEligibility:
    context_id: str
    eligible: bool
    candidate_run_id: str | None
    candidate_status: str | None
    predecessor_loop_id: str | None
    reason: str | None
    consistency_token: str


class LoopActivationEligibilityResolver:
    async def resolve(self, session: AsyncSession, context_id: str, *, lock: bool = False) -> ActivationEligibility:
        run_query = (
            select(DesktopRun)
            .where(
                DesktopRun.task_id == context_id,
                DesktopRun.agent_id == f"main:{context_id}",
                DesktopRun.kind == "main",
                DesktopRun.origin == "direct_user",
            )
            .order_by(DesktopRun.created_at.desc(), DesktopRun.run_id.desc())
            .limit(1)
        )
        predecessor_query = (
            select(AgentLoop)
            .where(AgentLoop.initial_context_id == context_id)
            .order_by(AgentLoop.created_at.desc(), AgentLoop.loop_id.desc())
            .limit(1)
        )
        if lock:
            run_query = run_query.with_for_update()
            predecessor_query = predecessor_query.with_for_update()
        run = await session.scalar(run_query)
        predecessor = await session.scalar(predecessor_query)
        conflict = predecessor if predecessor is not None and predecessor.status in NONTERMINAL_LOOP_STATES else None
        reason = self._ineligibility_reason(run, conflict)
        token = self._token(context_id, run, predecessor)
        return ActivationEligibility(
            context_id=context_id,
            eligible=reason is None,
            candidate_run_id=None if run is None else run.run_id,
            candidate_status=None if run is None else run.status,
            predecessor_loop_id=None if predecessor is None else predecessor.loop_id,
            reason=reason,
            consistency_token=token,
        )

    @staticmethod
    def _ineligibility_reason(run: DesktopRun | None, predecessor: AgentLoop | None) -> str | None:
        if run is None:
            return "no_direct_user_run"
        if run.loop_id is not None:
            return "newest_run_already_bound"
        if run.status not in ELIGIBLE_RUN_STATES:
            return "newest_run_status_ineligible"
        if predecessor is not None:
            return "nonterminal_predecessor"
        return None

    @staticmethod
    def _token(context_id: str, run: DesktopRun | None, predecessor: AgentLoop | None) -> str:
        parts = (
            context_id,
            "" if run is None else run.run_id,
            "" if run is None else run.status,
            "" if run is None or run.loop_id is None else run.loop_id,
            "" if predecessor is None else predecessor.loop_id,
            "" if predecessor is None else predecessor.status,
            "" if predecessor is None else str(predecessor.revision),
        )
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
