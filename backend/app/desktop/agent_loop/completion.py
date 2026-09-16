r"""本文件对外提供 CompletionVerifierPort、CompletionGuard 与 CompletionGuardResult。

输入为最小 goal/contract、候选 Portfolio、workspace/测试/产物证据和持久 verification；输出为逐 criterion
验证建议或确定性完成许可。具体工作流为独立 Worker 只返回证据，Guard 再检查新鲜度、unknown、
活动 Run、gate、publication、adoption 与 grant，不能由 Patrol 自证。示例：`guard.check(...)`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pydantic import BaseModel, ConfigDict

from backend.app.desktop.agent_loop.schemas import CompletionVerificationContract
from backend.app.desktop.agent_loop.models import CompletionVerification, LoopWorkerRequest


class CompletionVerifierPort(Protocol):
    async def verify(self, evidence: dict[str, Any]) -> CompletionVerificationContract: ...


class CompletionGuardResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    waiting_user: bool
    reasons: tuple[str, ...]


class CompletionGuard:
    def check(
        self,
        verification: CompletionVerificationContract,
        *,
        goal_revision: int,
        frontier_hash: str,
        workspace_revision: int,
        active_or_queued_runs: int,
        pending_human_gates: int,
        portfolio_published: bool,
        workspace_adopted: bool,
        grant_allows_completion: bool,
    ) -> CompletionGuardResult:
        reasons: list[str] = []
        if verification.goal_revision != goal_revision or verification.frontier_hash != frontier_hash or verification.workspace_revision != workspace_revision:
            reasons.append("verification_stale")
        if verification.conclusion != "satisfied" or any(item.status != "satisfied" for item in verification.criteria):
            reasons.append("criteria_not_satisfied")
        if verification.unresolved:
            reasons.append("unresolved_items")
        if active_or_queued_runs:
            reasons.append("runs_active")
        if pending_human_gates:
            reasons.append("human_gate_pending")
        if not portfolio_published:
            reasons.append("portfolio_not_published")
        if not workspace_adopted:
            reasons.append("workspace_not_adopted")
        if not grant_allows_completion:
            reasons.append("grant_disallows_completion")
        return CompletionGuardResult(
            allowed=not reasons,
            waiting_user=bool(reasons),
            reasons=tuple(reasons),
        )


class CompletionEvidenceService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record(
        self,
        contract: CompletionVerificationContract,
        worker_request_id: str,
    ) -> CompletionVerificationContract:
        async with self._sessions.begin() as session:
            existing = await session.get(CompletionVerification, contract.verification_id)
            if existing is not None:
                return contract
            worker = await session.get(LoopWorkerRequest, worker_request_id, with_for_update=True)
            if worker is None or worker.kind != "completion_verifier" or worker.loop_id != contract.loop_id or worker.round_id != contract.round_id:
                raise ValueError("Completion verification 必须来自当前 round 的独立 Verifier Worker")
            session.add(CompletionVerification(verification_id=contract.verification_id, loop_id=contract.loop_id, round_id=contract.round_id, goal_revision=contract.goal_revision, frontier_hash=contract.frontier_hash, workspace_revision=contract.workspace_revision, criteria=[item.model_dump(mode="json") for item in contract.criteria], conclusion=contract.conclusion, unresolved=list(contract.unresolved), worker_request_id=worker_request_id))
            worker.status = "success"
            worker.result = contract.model_dump(mode="json")
            worker.completed_at = datetime.now(UTC)
            return contract
