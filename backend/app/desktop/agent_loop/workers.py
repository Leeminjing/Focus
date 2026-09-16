r"""本文件对外提供 LoopWorkerRuntime、StructuredCompletionVerifier 与 StructuredLaneAdvisor。

输入为 Patrol 已提交的可选 Worker request、当前 goal/round/Portfolio/workspace 证据和模型配置；输出为
无工具、无状态提交能力的完成证据或 Lane 建议。具体工作流为持久领取 request、独立模型调用、严格
解析并保存结果，所有同轮 Worker 结束后只创建新 observation round，最终判断仍归 Portfolio Patrol。
示例：`await runtime.drain()`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from typing import Any
import uuid

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.completion import CompletionEvidenceService
from backend.app.desktop.agent_loop.budgets import LoopBudgetGuard, configured_provider_count
from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopContextMembership, LoopDelegationGrant, LoopEventOutbox, LoopGoalRevision, LoopRound, LoopWorkerRequest
from backend.app.desktop.agent_loop.schemas import CompletionVerificationContract, CriterionVerification
from backend.app.desktop.agent_loop.usage import LoopUsageDelta, LoopUsageLedger
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot
from focus.config.app_config import AppConfig
from focus.models.factory import create_chat_model
from focus.runtime.runs.usage import ModelUsage, callback_usage


class CompletionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    criteria: tuple[CriterionVerification, ...]
    conclusion: str = Field(pattern=r"^(satisfied|unsatisfied|unknown)$")
    unresolved: tuple[str, ...] = ()


class LaneAdviceProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rationale: str = Field(min_length=1, max_length=4000)
    proposals: tuple[dict[str, Any], ...]


class _StructuredWorker:
    def __init__(self, app_config: AppConfig, model_name: str | None = None) -> None:
        self._app_config = app_config
        self._model_name = model_name
        self.usage = ModelUsage()

    async def invoke(self, schema, system: str, payload: dict[str, Any]):
        config = self._app_config.get_model(self._model_name or self._app_config.resolve_default_model_name())
        model = create_chat_model(name=config.name, app_config=self._app_config, max_tokens=config.curation_max_output_tokens)
        document = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        json_schema = json.dumps(schema.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
        messages = [SystemMessage(content=system), HumanMessage(content=f"<worker_input>{document}</worker_input>\nJSON Schema: {json_schema}")]
        callback = UsageMetadataCallbackHandler()
        try:
            invoke_config = {"callbacks": [callback]}
            if config.curation_output_method == "prompt_json":
                response = await model.ainvoke(messages, config=invoke_config)
                content = getattr(response, "content", response)
                text = content if isinstance(content, str) else "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
                candidate = text.strip()
                if candidate.startswith("```"):
                    lines = candidate.splitlines()
                    candidate = "\n".join(lines[1:-1]).strip()
                return schema.model_validate(json.loads(candidate))
            response = await model.with_structured_output(schema, method=config.curation_output_method).ainvoke(messages, config=invoke_config)
            return response if isinstance(response, schema) else schema.model_validate(response)
        finally:
            measured = callback_usage(callback)
            self.usage += measured if measured.model_calls else ModelUsage(model_calls=1)


class StructuredCompletionVerifier:
    def __init__(self, worker: _StructuredWorker) -> None:
        self._worker = worker

    async def verify(self, payload: dict[str, Any]) -> CompletionProposal:
        return await self._worker.invoke(CompletionProposal, "你是独立 Completion Verifier。你不推进任务、不调用工具、不改变状态，只按每条验收条件检查所给可追溯证据。证据不足必须返回 unknown，冲突必须返回 unsatisfied 或 unknown，不得替 Patrol 宣布完成。", payload)


class StructuredLaneAdvisor:
    def __init__(self, worker: _StructuredWorker) -> None:
        self._worker = worker

    async def advise(self, payload: dict[str, Any]) -> LaneAdviceProposal:
        return await self._worker.invoke(LaneAdviceProposal, "你是无权 Lane Curator Worker。只针对 Patrol 分配的 Lane 和来源提供候选策展建议，不得请求运行、修改 Context、发布 Portfolio 或扩大范围。", payload)


class LoopWorkerRuntime:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], app_config: AppConfig) -> None:
        self._sessions = sessions
        self._app_config = app_config
        self._completion = CompletionEvidenceService(sessions)

    async def drain(self) -> int:
        requests = await self._claim_many()
        if not requests:
            return 0
        await asyncio.gather(*(self._run_one(request) for request in requests))
        return len(requests)

    async def _run_one(self, request: LoopWorkerRequest) -> None:
        try:
            await self._require_budget(request)
            if request.kind == "completion_verifier":
                await self._verify_completion(request)
            elif request.kind == "lane_curator":
                await self._advise_lanes(request)
            else:
                raise ValueError(f"未知 Loop Worker: {request.kind}")
            await self._advance(request.loop_id, request.round_id)
        except Exception as exc:
            await self._fail(request, exc)

    async def _require_budget(self, request: LoopWorkerRequest) -> None:
        async with self._sessions() as session:
            loop = await session.get(AgentLoop, request.loop_id)
            usage = await session.get(LoopBudgetUsage, request.loop_id)
            if loop is None:
                raise LookupError("Worker 所属 Loop 不存在")
            grant = await session.scalar(
                select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == loop.loop_id,
                    LoopDelegationGrant.revision == loop.authority_revision,
                    LoopDelegationGrant.status == "active",
                )
            )
            usage_values = {
                field: int(getattr(usage, field, 0) or 0)
                for field in ("rounds", "model_calls", "input_tokens", "output_tokens", "retries", "lanes", "no_progress_count")
            }
            created_at = loop.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            usage_values["duration_seconds"] = max(0, int((datetime.now(UTC) - created_at).total_seconds()))
            usage_values["providers"] = configured_provider_count(loop.equipment or {})
            usage_values["contexts"] = int(
                await session.scalar(
                    select(func.count()).select_from(LoopContextMembership).where(
                        LoopContextMembership.loop_id == loop.loop_id,
                        LoopContextMembership.status != "discarded",
                    )
                )
                or 0
            )
            if grant is None or LoopBudgetGuard().evaluate(
                usage_values,
                grant.budgets,
                request.kind,
                {"model_calls": 1},
            ).status == "exhausted":
                raise RuntimeError("Loop Worker budget 已耗尽或 delegation 已撤销")

    async def _claim_many(self, limit: int = 8) -> tuple[LoopWorkerRequest, ...]:
        async with self._sessions.begin() as session:
            rows = list(
                (
                    await session.scalars(
                        select(LoopWorkerRequest)
                        .where(LoopWorkerRequest.status == "pending")
                        .order_by(LoopWorkerRequest.created_at)
                        .with_for_update(skip_locked=True)
                        .limit(limit)
                    )
                ).all()
            )
            for row in rows:
                row.status = "running"
            return tuple(rows)

    async def _verify_completion(self, request: LoopWorkerRequest) -> None:
        payload, loop, round_row = await self._evidence(request)
        model_name = (loop.equipment or {}).get("verifier_model_name") or (loop.equipment or {}).get("model_name")
        worker = _StructuredWorker(self._app_config, model_name)
        try:
            proposal = await StructuredCompletionVerifier(worker).verify(payload)
        finally:
            await self._record_usage(loop.loop_id, worker.usage)
        contract = CompletionVerificationContract(verification_id=uuid.uuid4().hex, loop_id=loop.loop_id, round_id=round_row.round_id, goal_revision=loop.goal_revision, frontier_hash=round_row.frontier_hash, workspace_revision=round_row.workspace_revision, criteria=proposal.criteria, conclusion=proposal.conclusion, unresolved=proposal.unresolved)
        await self._completion.record(contract, request.worker_request_id)

    async def _advise_lanes(self, request: LoopWorkerRequest) -> None:
        payload, loop, _ = await self._evidence(request)
        model_name = (loop.equipment or {}).get("curator_model_name") or (loop.equipment or {}).get("model_name")
        worker = _StructuredWorker(self._app_config, model_name)
        try:
            result = await StructuredLaneAdvisor(worker).advise({**payload, "assignments": request.scope.get("assignments", [])})
        finally:
            await self._record_usage(loop.loop_id, worker.usage)
        async with self._sessions.begin() as session:
            row = await session.get(LoopWorkerRequest, request.worker_request_id, with_for_update=True)
            row.status = "success"
            row.result = result.model_dump(mode="json")
            row.completed_at = datetime.now(UTC)

    async def _record_usage(self, loop_id: str, usage: ModelUsage) -> None:
        await LoopUsageLedger(self._sessions).record(
            loop_id,
            LoopUsageDelta.from_model_usage(usage),
        )

    async def _evidence(self, request: LoopWorkerRequest) -> tuple[dict[str, Any], AgentLoop, LoopRound]:
        async with self._sessions() as session:
            loop = await session.get(AgentLoop, request.loop_id)
            round_row = await session.get(LoopRound, request.round_id)
            if loop is None or round_row is None:
                raise LookupError("Worker 所属 Loop/round 不存在")
            goal = await session.scalar(select(LoopGoalRevision).where(LoopGoalRevision.loop_id == loop.loop_id, LoopGoalRevision.revision == loop.goal_revision))
            runs = list((await session.scalars(select(DesktopRun).where(DesktopRun.loop_id == loop.loop_id).order_by(DesktopRun.created_at.desc()).limit(32))).all())
            slot = await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.workspace_id == loop.workspace_id, WorkspaceSlot.kind == "authoritative", WorkspaceSlot.lifecycle != "deleted"))
            payload = {"goal": {"goal": goal.goal, "task_contract": goal.task_contract, "acceptance_criteria": goal.acceptance_criteria}, "scope": request.scope, "frontier_hash": round_row.frontier_hash, "workspace": {"revision": slot.revision if slot else round_row.workspace_revision, "fingerprint": slot.current_fingerprint if slot else None}, "run_evidence": [{"run_id": row.run_id, "context_id": row.task_id, "status": row.status, "error": row.error, "workspace_result": row.workspace_result, "final_checkpoint_id": row.final_checkpoint_id} for row in runs]}
            return payload, loop, round_row

    async def _advance(self, loop_id: str, round_id: str) -> None:
        async with self._sessions.begin() as session:
            pending = await session.scalar(select(func.count()).select_from(LoopWorkerRequest).where(LoopWorkerRequest.round_id == round_id, LoopWorkerRequest.status.in_(["pending", "running"])))
            if pending:
                return
            loop = await session.get(AgentLoop, loop_id, with_for_update=True)
            current = await session.get(LoopRound, round_id, with_for_update=True)
            if loop is None or current is None or loop.status != "running":
                return
            current.status = "settled"
            current.settled_at = datetime.now(UTC)
            number = int(await session.scalar(select(func.max(LoopRound.number)).where(LoopRound.loop_id == loop_id)) or 0) + 1
            next_round = LoopRound(round_id=uuid.uuid4().hex, loop_id=loop_id, number=number, authority_revision=loop.authority_revision, goal_revision=loop.goal_revision, frontier_hash=current.frontier_hash, workspace_revision=current.workspace_revision)
            loop.current_round_id = next_round.round_id
            loop.revision += 1
            loop.health = "observing"
            session.add(next_round)
            sequence = int(await session.scalar(select(func.coalesce(func.max(LoopEventOutbox.sequence), 0)).where(LoopEventOutbox.loop_id == loop_id)) or 0) + 1
            session.add(LoopEventOutbox(event_id=uuid.uuid4().hex, loop_id=loop_id, sequence=sequence, event_type="WorkerResultsReady", payload={"round_id": next_round.round_id, "source_round_id": round_id}, idempotency_key=f"workers:{round_id}:ready"))

    async def _fail(self, request: LoopWorkerRequest, exc: Exception) -> None:
        async with self._sessions.begin() as session:
            row = await session.get(LoopWorkerRequest, request.worker_request_id, with_for_update=True)
            loop = await session.get(AgentLoop, request.loop_id, with_for_update=True)
            if row is not None:
                row.status = "error"
                row.result = {"error": str(exc)[:2000]}
                row.completed_at = datetime.now(UTC)
            if loop is not None:
                loop.status = "waiting_user"
                loop.health = "degraded"
                loop.waiting_reason = f"{request.kind} Worker 失败: {str(exc)[:1000]}"
