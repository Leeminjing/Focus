r"""本文件对外提供 CompletionVerifierPort、CompletionCandidateValidator、CompletionEvidenceService、CompletionGuard 与 CompletionGuardResult。

输入为当前 Mission 检查、候选 Portfolio、workspace/测试/产物证据和持久 verification；输出为逐 check_id
验证建议或确定性完成许可。具体工作流为候选在短只读事务内核对冻结来源及当前授权，模型有界纠错等待实际来源校验；独立 Worker 只返回类型化证据，持久化前再次核对唯一领取身份并拒绝未声明检查，Guard 再检查新鲜度、unknown、
活动 Run、gate、publication、adoption 与 grant，不能由 Patrol 自证。示例：`guard.check(...)`。
Verifier提交verification与Worker success时同事务补交原模型回执；结算失败回滚结果，已失效领取身份不能发布verification。
新版正文读面的候选及最终记录复用完整文本资格复检；历史冻结输入只保存原内容，不回填新正文。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy import select, text

from pydantic import BaseModel, ConfigDict

from backend.app.desktop.agent_loop.completion_policy import CompletionCheckPolicy
from backend.app.desktop.agent_loop.completion_sources import CompletionSourceValidator, CompletionSourceRejection
from backend.app.desktop.agent_loop.worker_attempts import WorkerAttemptAuthority, WorkerAttemptRejected
from backend.app.desktop.domain_evidence.identity import canonical_hash
from backend.app.desktop.agent_loop.mission_contract import LegacyMissionAdapter
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.schemas import CompletionVerificationContract
from backend.app.desktop.agent_loop.models import AgentLoop, CompletionVerification, LoopGoalRevision, LoopWorkerRequest


class CompletionVerifierPort(Protocol):
    async def verify(self, evidence: dict[str, Any]) -> CompletionVerificationContract: ...


class CompletionCandidateValidator:
    def __init__(self, sessions, request, identity, checks, sources, *, require_artifact_content=False):
        self._sessions, self._request = sessions, request
        self._identity, self._checks = identity, checks
        self._sources = tuple(sources)
        self._source_ids = frozenset(s["source_id"] for s in sources)
        self._require_artifact_content = require_artifact_content

    async def validate(self, proposal):
        CompletionCheckPolicy().validate(self._checks, proposal.criteria)
        for criterion in proposal.criteria:
            if criterion.status == "satisfied":
                for evidence in criterion.evidence:
                    if evidence.source_id not in self._source_ids:
                        raise CompletionSourceRejection("source_not_frozen", f"完成检查 {criterion.check_id}: 来源不属于冻结目录",
                                                        canonical_hash(["unrecognized_source_id", evidence.source_id]), self._identity["workspace_revision"])
        contract = CompletionVerificationContract(**self._identity, **proposal.model_dump(mode="json"))
        async with self._sessions() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            await self._authority(session)
            await CompletionSourceValidator().validate(session, self._checks, contract, frozen_sources=self._sources,
                require_artifact_content=self._require_artifact_content)

    async def admit(self, model, schema, system, payload):
        from backend.app.desktop.agent_loop.patrol_request_capacity import PatrolRequestCapacity
        from backend.app.desktop.agent_loop.structured_worker import StructuredWorkerModel

        async with self._sessions() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            grant = await self._authority(session)
            capacity = PatrolRequestCapacity(model.request_model_config, grant.budgets)
        capacity.check(StructuredWorkerModel.request_messages(schema, system, payload), schema)

    async def _authority(self, session):
        loop = await session.get(AgentLoop, self._request.loop_id)
        worker = await session.get(LoopWorkerRequest, self._request.worker_request_id)
        if (loop is None or worker is None or worker.status != "running"
            or worker.retry_identity != self._request.retry_identity):
            raise WorkerAttemptRejected("Completion候选 Worker attempt 已过期")
        return await WorkerAttemptAuthority().require_authorized(session, loop, worker)


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
        *,
        retry_identity: str,
        usage_receipts=None,
    ) -> CompletionVerificationContract:
        async with self._sessions.begin() as session:
            existing = await session.get(CompletionVerification, contract.verification_id)
            if existing is not None:
                return contract
            loop = await session.get(AgentLoop, contract.loop_id, with_for_update=True)
            worker = await session.get(LoopWorkerRequest, worker_request_id, with_for_update=True)
            if worker is None or worker.status != "running" or not retry_identity or worker.retry_identity != retry_identity or loop is None or loop.status != "running" or worker.kind != "completion_verifier" or worker.loop_id != contract.loop_id or worker.round_id != contract.round_id:
                raise ValueError("Completion verification 必须来自当前 round 的独立 Verifier Worker")
            from backend.app.desktop.agent_loop.worker_attempts import WorkerAttemptAuthority

            await WorkerAttemptAuthority().require_authorized(session, loop, worker)
            mission = await session.scalar(select(LoopMissionRevision).where(LoopMissionRevision.loop_id == contract.loop_id, LoopMissionRevision.revision == contract.goal_revision))
            goal = None if mission is not None else await session.scalar(select(LoopGoalRevision).where(LoopGoalRevision.loop_id == contract.loop_id, LoopGoalRevision.revision == contract.goal_revision))
            checks = (
                mission.completion_checks
                if mission is not None
                else [item.model_dump(mode="json") for item in LegacyMissionAdapter.convert(goal=goal.goal, task_contract=goal.task_contract, acceptance_criteria=goal.acceptance_criteria).completion_checks]
                if goal is not None
                else []
            )
            CompletionCheckPolicy().validate(checks, contract.criteria)
            frozen = worker.scope.get("frozen_worker_input")
            if frozen is not None:
                sources = {s["source_id"] for s in frozen.get("completion_sources", ())}
                if any(e.source_id not in sources for c in contract.criteria if c.status == "satisfied" for e in c.evidence):
                    raise ValueError("Completion verification 来源不属于冻结目录")
            await CompletionSourceValidator().validate(session, checks, contract,
                frozen_sources=frozen.get("completion_sources", ()) if frozen is not None else None,
                require_artifact_content=any(v.get('contract') == 'completion-current-sources-v2'
                    for v in worker.scope.get('completion_input_views', ())))
            if usage_receipts is not None:
                await usage_receipts.flush_pending(session)
            session.add(CompletionVerification(verification_id=contract.verification_id, loop_id=contract.loop_id, round_id=contract.round_id, goal_revision=contract.goal_revision, frontier_hash=contract.frontier_hash, workspace_revision=contract.workspace_revision, criteria=[item.model_dump(mode="json") for item in contract.criteria], conclusion=contract.conclusion, unresolved=list(contract.unresolved), worker_request_id=worker_request_id))
            worker.status = "success"
            worker.result = contract.model_dump(mode="json")
            WorkerAttemptAuthority.archive(worker, "success", worker.result)
            worker.completed_at = datetime.now(UTC)
            return contract
