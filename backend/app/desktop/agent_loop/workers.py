r"""本文件对外提供 LoopWorkerRuntime、仅验证声明检查的 StructuredCompletionVerifier 与 StructuredLaneAdvisor。

输入为 Patrol 已提交的可选 Worker request、可选 Loop scope、当前 Mission/round/Portfolio/workspace 证据和模型配置；输出为
无工具、无状态提交能力的完成证据或 Lane 建议。具体工作流为独立有界池持久领取 request、记录 attempt identity、
后台执行模型调用并严格解析结果，Curator 生命周期逐步提交且部分结果立即可见，失败有界重试；Patrol Session
所属 Curator 全部结束后将同一 round 标为 curated，旧式 Worker 批次仍创建新 observation round，最终判断仍归 Portfolio Patrol。
示例：`await runtime.drain()`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from typing import Any, ClassVar
import uuid

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.completion import CompletionEvidenceService
from backend.app.desktop.agent_loop.completion_policy import CompletionCheckPolicy
from backend.app.desktop.agent_loop.curator_assignments import CuratorAssignmentRepository
from backend.app.desktop.agent_loop.budgets import LoopBudgetGuard, configured_provider_count
from backend.app.desktop.agent_loop.mission_contract import LegacyMissionAdapter
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopContextMembership, LoopDelegationGrant, LoopEventOutbox, LoopGoalRevision, LoopRound, LoopWorkerRequest
from backend.app.desktop.agent_loop.patrol_session_models import LoopCuratorAssignment
from backend.app.desktop.agent_loop.patrol_session_models import LoopPatrolSession
from backend.app.desktop.agent_loop.patrol_session_repository import PatrolSessionRepository
from backend.app.desktop.agent_loop.patrol_session_state import PatrolActivity, PatrolPhase
from backend.app.desktop.agent_loop.schemas import CompletionVerificationContract, CriterionVerification
from backend.app.desktop.agent_loop.usage import LoopUsageDelta, LoopUsageLedger
from backend.app.desktop.agent_loop.wait_requests import open_recovery_wait
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot
from focus.config.app_config import AppConfig
from focus.models.factory import create_chat_model
from focus.runtime.runs.usage import ModelUsage, callback_usage


class CompletionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    criteria: tuple[CriterionVerification, ...] = Field(min_length=1)
    conclusion: str = Field(pattern=r"^(satisfied|unsatisfied|unknown)$")
    unresolved: tuple[str, ...] = ()


class LaneAdviceProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    _FORBIDDEN_EFFECTS: ClassVar[frozenset[str]] = frozenset(
        {"send_directive", "publish_portfolio", "change_mission", "change_contract", "verify_fact"}
    )

    rationale: str = Field(min_length=1, max_length=4000)
    proposals: tuple[dict[str, Any], ...]

    @model_validator(mode="after")
    def reject_authoritative_effects(self) -> "LaneAdviceProposal":
        for proposal in self.proposals:
            self._validate_unprivileged(proposal)
        return self

    @classmethod
    def _validate_unprivileged(cls, value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).strip().lower()
                if normalized in cls._FORBIDDEN_EFFECTS and bool(item):
                    raise ValueError(f"Curator proposal 不得执行权威 effect: {normalized}")
                if normalized in {"execute", "commit", "command", "authority_effect"} and str(item).strip().lower() in cls._FORBIDDEN_EFFECTS:
                    raise ValueError(f"Curator proposal 不得执行权威 effect: {item}")
                cls._validate_unprivileged(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                cls._validate_unprivileged(item)


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
        return await self._worker.invoke(CompletionProposal, "你是独立 Completion Verifier。你不推进任务、不调用工具、不改变状态，只能逐一返回 completion_checks 中已有的 check_id，并用每项允许的类型化证据检查。不得从 outcome、boundary 或其它文字创建检查；证据不足必须返回 unknown，冲突必须返回 unsatisfied 或 unknown，不得替 Patrol 宣布完成。", payload)


class StructuredLaneAdvisor:
    def __init__(self, worker: _StructuredWorker) -> None:
        self._worker = worker

    async def advise(self, payload: dict[str, Any]) -> LaneAdviceProposal:
        return await self._worker.invoke(LaneAdviceProposal, "你是无权 Lane Curator Worker。只针对 Patrol 分配的 Lane 和来源提供候选策展建议，不得请求运行、修改 Context、发布 Portfolio 或扩大范围。", payload)


class LoopWorkerRuntime:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        app_config: AppConfig,
        *,
        concurrency: int = 8,
    ) -> None:
        self._sessions = sessions
        self._app_config = app_config
        self._completion = CompletionEvidenceService(sessions)
        self._curator_assignments = CuratorAssignmentRepository()
        self._patrol_sessions = PatrolSessionRepository()
        self._concurrency = max(1, concurrency)
        self._tasks: dict[str, asyncio.Task] = {}

    async def drain(self, loop_id: str | None = None) -> int:
        self._reap()
        requests = await self._claim_many(self._concurrency - len(self._tasks), loop_id)
        for request in requests:
            self._tasks[request.worker_request_id] = asyncio.create_task(
                self._run_one(request),
                name=f"loop-curator:{request.loop_id}:{request.worker_request_id}",
            )
        return len(requests)

    async def close(self) -> None:
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

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
        except asyncio.CancelledError:
            await self._cancel(request, "component_stopped")
            raise
        except Exception as exc:
            await self._fail(request, exc)

    async def _require_budget(self, request: LoopWorkerRequest) -> None:
        async with self._sessions() as session:
            loop = await session.get(AgentLoop, request.loop_id)
            usage = await session.get(LoopBudgetUsage, request.loop_id)
            if loop is None or loop.status != "running":
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

    async def _claim_many(self, limit: int, loop_id: str | None = None) -> tuple[LoopWorkerRequest, ...]:
        if limit <= 0:
            return ()
        async with self._sessions.begin() as session:
            statement = (
                select(LoopWorkerRequest)
                .join(AgentLoop, AgentLoop.loop_id == LoopWorkerRequest.loop_id)
                .where(
                    LoopWorkerRequest.status == "pending",
                    LoopWorkerRequest.attempt <= LoopWorkerRequest.max_attempts,
                    AgentLoop.status == "running",
                )
                .order_by(LoopWorkerRequest.created_at)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
            if loop_id is not None:
                statement = statement.where(LoopWorkerRequest.loop_id == loop_id)
            rows = list(
                (
                    await session.scalars(statement)
                ).all()
            )
            for row in rows:
                row.status = "running"
                row.retry_identity = f"worker:{row.worker_request_id}:attempt:{row.attempt}"
                assignment = await self._curator_assignments.by_worker(session, row.worker_request_id, lock=True)
                if assignment is not None and assignment.state == "queued":
                    await self._curator_assignments.transition(
                        session,
                        assignment.assignment_id,
                        "reading",
                        "Curator 正在读取分配的 Context 证据",
                    )
            return tuple(rows)

    async def _verify_completion(self, request: LoopWorkerRequest) -> None:
        payload, loop, round_row = await self._evidence(request)
        checks = tuple(payload["mission"]["completion_checks"])
        verifier_payload = {key: value for key, value in payload.items() if key != "mission"} | {"completion_checks": checks}
        model_name = (loop.equipment or {}).get("verifier_model_name") or (loop.equipment or {}).get("model_name")
        worker = _StructuredWorker(self._app_config, model_name)
        try:
            proposal = await StructuredCompletionVerifier(worker).verify(verifier_payload)
        finally:
            await self._record_usage(loop.loop_id, worker.usage)
        CompletionCheckPolicy().validate(checks, proposal.criteria)
        contract = CompletionVerificationContract(verification_id=uuid.uuid4().hex, loop_id=loop.loop_id, round_id=round_row.round_id, goal_revision=loop.goal_revision, frontier_hash=round_row.frontier_hash, workspace_revision=round_row.workspace_revision, criteria=proposal.criteria, conclusion=proposal.conclusion, unresolved=proposal.unresolved)
        await self._completion.record(contract, request.worker_request_id)

    async def _advise_lanes(self, request: LoopWorkerRequest) -> None:
        await self._start_curator_analysis(request.worker_request_id)
        payload, loop, _ = await self._evidence(request)
        model_name = (loop.equipment or {}).get("curator_model_name") or (loop.equipment or {}).get("model_name")
        worker = _StructuredWorker(self._app_config, model_name)
        try:
            result = await StructuredLaneAdvisor(worker).advise({**payload, "assignments": request.scope.get("assignments", [])})
        finally:
            await self._record_usage(loop.loop_id, worker.usage)
        async with self._sessions.begin() as session:
            row = await session.get(LoopWorkerRequest, request.worker_request_id, with_for_update=True)
            loop = await session.get(AgentLoop, request.loop_id, with_for_update=True)
            if row is None or row.status != "running" or loop is None or loop.status != "running":
                if row is not None and row.status == "running":
                    row.status = "cancelled"
                    row.result = {"reason": "loop_not_running"}
                    row.completed_at = datetime.now(UTC)
                return
            row.status = "success"
            row.result = result.model_dump(mode="json")
            row.completed_at = datetime.now(UTC)
            assignment = await self._curator_assignments.by_worker(session, row.worker_request_id, lock=True)
            if assignment is not None and assignment.state == "analyzing":
                await self._curator_assignments.transition(
                    session,
                    assignment.assignment_id,
                    "proposed",
                    "Curator proposal 已提交，等待 Patrol 消费",
                    result_summary=result.rationale,
                )

    async def _start_curator_analysis(self, worker_request_id: str) -> None:
        async with self._sessions.begin() as session:
            assignment = await self._curator_assignments.by_worker(session, worker_request_id, lock=True)
            if assignment is not None and assignment.state == "reading":
                await self._curator_assignments.transition(
                    session,
                    assignment.assignment_id,
                    "analyzing",
                    "Curator 正在分析 Context 与 Lane 证据",
                )

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
            mission = await session.scalar(select(LoopMissionRevision).where(LoopMissionRevision.loop_id == loop.loop_id, LoopMissionRevision.revision == loop.goal_revision))
            goal = None if mission is not None else await session.scalar(select(LoopGoalRevision).where(LoopGoalRevision.loop_id == loop.loop_id, LoopGoalRevision.revision == loop.goal_revision))
            if mission is None and goal is None:
                raise LookupError("Worker 所属 Loop 缺少当前 Mission")
            mission_payload = (
                {"revision": mission.revision, "outcome": mission.outcome, "boundaries": mission.boundaries, "completion_checks": mission.completion_checks, "source_format": "structured"}
                if mission is not None
                else {**LegacyMissionAdapter.convert(goal=goal.goal, task_contract=goal.task_contract, acceptance_criteria=goal.acceptance_criteria).model_dump(mode="json"), "revision": goal.revision, "source_format": "legacy_adapter"}
            )
            runs = list((await session.scalars(select(DesktopRun).where(DesktopRun.loop_id == loop.loop_id).order_by(DesktopRun.created_at.desc()).limit(32))).all())
            slot = await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.workspace_id == loop.workspace_id, WorkspaceSlot.kind == "authoritative", WorkspaceSlot.lifecycle != "deleted"))
            payload = {"mission": mission_payload, "scope": request.scope, "frontier_hash": round_row.frontier_hash, "workspace": {"revision": slot.revision if slot else round_row.workspace_revision, "fingerprint": slot.current_fingerprint if slot else None}, "run_evidence": [{"run_id": row.run_id, "context_id": row.task_id, "status": row.status, "error": row.error, "workspace_result": row.workspace_result, "final_checkpoint_id": row.final_checkpoint_id} for row in runs]}
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
            curator_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(LoopCuratorAssignment)
                    .where(LoopCuratorAssignment.round_id == round_id)
                )
                or 0
            )
            if curator_count:
                current.status = "curated"
                loop.health = "deciding"
                return
            patrol = await session.scalar(
                select(LoopPatrolSession)
                .where(LoopPatrolSession.round_id == round_id, LoopPatrolSession.status == "active")
                .with_for_update()
            )
            if patrol is not None:
                await self._patrol_sessions.transition(
                    session,
                    patrol.session_id,
                    PatrolPhase.COMPLETED,
                    PatrolActivity(summary="Worker 证据已返回，本轮 Patrol 等待结束"),
                    terminal_outcome={"status": "completed", "source": "worker_results"},
                )
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
            if row is None or row.status != "running":
                return
            if loop is not None and loop.status == "running" and row.attempt < row.max_attempts:
                row.attempt += 1
                row.status = "pending"
                row.retry_identity = f"worker:{row.worker_request_id}:attempt:{row.attempt}"
                row.result = {"error": str(exc)[:2000], "retrying": True}
                usage = await session.get(LoopBudgetUsage, request.loop_id, with_for_update=True)
                if usage is not None:
                    LoopUsageLedger.apply(usage, LoopUsageDelta(retries=1))
                await self._mark_curator_retry(session, row.worker_request_id, exc)
                return
            row.status = "cancelled" if loop is None or loop.status != "running" else "error"
            row.result = {"error": str(exc)[:2000], "retrying": False}
            row.completed_at = datetime.now(UTC)
            await self._mark_curator_terminal(session, row.worker_request_id, row.status, exc)
            if loop is not None and loop.status == "running":
                loop.health = "degraded"
                await open_recovery_wait(
                    session,
                    loop,
                    f"{request.kind} Worker 失败: {str(exc)[:1000]}",
                    source="loop-worker",
                    round_id=request.round_id,
                    scope={"worker_request_id": request.worker_request_id, "worker_kind": request.kind},
                )

    async def _cancel(self, request: LoopWorkerRequest, reason: str) -> None:
        async with self._sessions.begin() as session:
            row = await session.get(LoopWorkerRequest, request.worker_request_id, with_for_update=True)
            if row is not None and row.status == "running":
                row.status = "cancelled"
                row.result = {"reason": reason}
                row.completed_at = datetime.now(UTC)
                assignment = await self._curator_assignments.by_worker(session, row.worker_request_id, lock=True)
                if assignment is not None and assignment.state not in {"consumed", "failed", "cancelled"}:
                    await self._curator_assignments.transition(
                        session,
                        assignment.assignment_id,
                        "cancelled",
                        "Curator assignment 已取消",
                        failure=reason,
                    )

    async def _mark_curator_retry(self, session: AsyncSession, worker_request_id: str, exc: Exception) -> None:
        assignment = await self._curator_assignments.by_worker(session, worker_request_id, lock=True)
        if assignment is None:
            return
        if assignment.state == "reading":
            await self._curator_assignments.transition(
                session,
                assignment.assignment_id,
                "analyzing",
                "Curator 分析失败，已安排有界重试",
                failure=str(exc),
            )
        elif assignment.state == "analyzing":
            await self._curator_assignments.transition(
                session,
                assignment.assignment_id,
                "analyzing",
                "Curator 分析失败，已安排有界重试",
                failure=str(exc),
            )

    async def _mark_curator_terminal(
        self,
        session: AsyncSession,
        worker_request_id: str,
        worker_status: str,
        exc: Exception,
    ) -> None:
        assignment = await self._curator_assignments.by_worker(session, worker_request_id, lock=True)
        if assignment is None or assignment.state in {"consumed", "failed", "cancelled"}:
            return
        target = "cancelled" if worker_status == "cancelled" else "failed"
        await self._curator_assignments.transition(
            session,
            assignment.assignment_id,
            target,
            "Curator assignment 未能完成",
            failure=str(exc),
        )

    def _reap(self) -> None:
        completed = tuple(request_id for request_id, task in self._tasks.items() if task.done())
        for request_id in completed:
            task = self._tasks.pop(request_id)
            if not task.cancelled():
                task.exception()
