r"""本文件对外提供 LoopWorkerRuntime、StructuredCompletionVerifier、测试兼容 StructuredLaneAdvisor 与 production worker 调度。

输入为 Patrol 已提交的可选 Worker request、可选 Loop scope、当前 Mission/round/Portfolio/workspace 证据和模型配置；输出为
无工具、无状态提交能力的完成证据、retrieval-session 约束的 WorkContextDraft 或角色专属结构化派生产物。具体工作流为独立有界池领取
request，lane curator 查询完整 Revision indexes；index/reconciliation/synthesis/claim/quality 角色绑定独立 schema、authority、重试、
attempt 与模型用量审计；最终状态判断和提交权仍归 Portfolio Patrol/Kernel。示例：`await runtime.drain()`。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from focus.config.app_config import AppConfig
from focus.runtime.runs.usage import ModelUsage
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.budgets import (
    LoopBudgetGuard,
    configured_provider_count,
)
from backend.app.desktop.agent_loop.completion import CompletionEvidenceService
from backend.app.desktop.agent_loop.completion_policy import CompletionCheckPolicy
from backend.app.desktop.agent_loop.context_expansion.artifact_repository import (
    SemanticDerivationArtifactRepository,
)
from backend.app.desktop.agent_loop.context_expansion.contracts import (
    DerivationStageRecord,
    WorkContextSpec,
    stable_expansion_hash,
)
from backend.app.desktop.agent_loop.context_expansion.quality_verifier import (
    QualityWorkerProposal,
)
from backend.app.desktop.agent_loop.context_expansion.reconciliation import (
    StructuredWorkSpecRelationEvaluator,
)
from backend.app.desktop.agent_loop.context_expansion.retrieval_planner import (
    LaneAdviceProposal,
    RetrievalBackedCognitiveAdvisor,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_indexer import (
    SemanticProjectionProposal,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import (
    PlanningRetrievalSession,
    PortfolioIndexCatalog,
    RetrievalBudget,
)
from backend.app.desktop.agent_loop.context_expansion.stage_telemetry import (
    DerivationStageTimer,
)
from backend.app.desktop.agent_loop.context_expansion.synthesis import (
    ClaimSupportProposal,
    ContextSynthesisWorkerDraft,
)
from backend.app.desktop.agent_loop.curator_assignments import (
    CuratorAssignmentRepository,
)
from backend.app.desktop.agent_loop.derivation_worker import RoleBoundStructuredModel
from backend.app.desktop.agent_loop.mission_contract import LegacyMissionAdapter
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopBudgetUsage,
    LoopContextMembership,
    LoopDelegationGrant,
    LoopEventOutbox,
    LoopGoalRevision,
    LoopRound,
    LoopWorkerRequest,
)
from backend.app.desktop.agent_loop.patrol_session_models import (
    LoopCuratorAssignment,
    LoopPatrolSession,
)
from backend.app.desktop.agent_loop.patrol_session_repository import (
    PatrolSessionRepository,
)
from backend.app.desktop.agent_loop.patrol_session_state import (
    PatrolActivity,
    PatrolPhase,
)
from backend.app.desktop.agent_loop.schemas import (
    CompletionVerificationContract,
    CriterionVerification,
)
from backend.app.desktop.agent_loop.structured_worker import StructuredWorkerModel
from backend.app.desktop.agent_loop.usage import LoopUsageDelta, LoopUsageLedger
from backend.app.desktop.agent_loop.wait_requests import open_recovery_wait
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot


class CompletionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    criteria: tuple[CriterionVerification, ...] = Field(min_length=1)
    conclusion: str = Field(pattern=r"^(satisfied|unsatisfied|unknown)$")
    unresolved: tuple[str, ...] = ()


class StructuredCompletionVerifier:
    def __init__(self, worker: StructuredWorkerModel) -> None:
        self._worker = worker

    async def verify(self, payload: dict[str, Any]) -> CompletionProposal:
        return await self._worker.invoke(CompletionProposal, "你是独立 Completion Verifier。你不推进任务、不调用工具、不改变状态，只能逐一返回 completion_checks 中已有的 check_id，并用每项允许的类型化证据检查。不得从 outcome、boundary 或其它文字创建检查；证据不足必须返回 unknown，冲突必须返回 unsatisfied 或 unknown，不得替 Patrol 宣布完成。", payload)


class StructuredLaneAdvisor:
    def __init__(self, worker: StructuredWorkerModel) -> None:
        self._worker = worker

    async def advise(self, payload: dict[str, Any]) -> LaneAdviceProposal:
        return await self._worker.invoke(LaneAdviceProposal, "你是无权 Cognitive Work Planner 测试适配器。只返回 WorkContextDraft；不得创建 Context、修改状态、运行工具或扩大 scope。生产路径使用冻结 semantic index 的 retrieval-backed planner。", payload)


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
        self._derivation_artifacts = SemanticDerivationArtifactRepository()
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
            elif request.kind == "lane_curator" or request.kind == "retrieval_cognitive_planner":
                await self._advise_lanes(request)
            elif request.kind in {
                "semantic_index_projector",
                "work_spec_reconciler",
                "dossier_synthesizer",
                "claim_verifier",
                "context_quality_verifier",
            }:
                await self._run_derivation_role(request)
            else:
                raise ValueError(f"未知 Loop Worker: {request.kind}")
            await self._advance(request.loop_id, request.round_id)
        except asyncio.CancelledError:
            await self._cancel(request, "component_stopped")
            raise
        except Exception as exc:  # noqa: BLE001
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
        worker = RoleBoundStructuredModel(
            self._app_config,
            request.kind,
            model_name,
        )
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
        worker = StructuredWorkerModel(self._app_config, model_name)
        try:
            index_ids = tuple(
                (request.scope.get("derivation_input") or {}).get("semantic_index_ids") or ()
            )
            if index_ids:
                result_payload = await self._retrieval_backed_advice(
                    request,
                    payload,
                    worker,
                    index_ids,
                )
                rationale = str(result_payload.get("rationale") or "retrieval-backed planning blocked")
            else:
                result = await StructuredLaneAdvisor(worker).advise(
                    {**payload, "assignments": request.scope.get("assignments", [])}
                )
                result_payload = result.model_dump(mode="json")
                rationale = result.rationale
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
            row.result = result_payload
            row.completed_at = datetime.now(UTC)
            assignment = await self._curator_assignments.by_worker(session, row.worker_request_id, lock=True)
            if assignment is not None and assignment.state == "analyzing":
                await self._curator_assignments.transition(
                    session,
                    assignment.assignment_id,
                    "proposed",
                    "Curator proposal 已提交，等待 Patrol 消费",
                    result_summary=rationale,
                )

    async def _retrieval_backed_advice(
        self,
        request: LoopWorkerRequest,
        payload: dict[str, Any],
        worker: StructuredWorkerModel,
        index_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        derivation = dict(request.scope.get("derivation_input") or {})
        catalog = PortfolioIndexCatalog.model_validate(derivation["portfolio_index_catalog"])
        budget_payload = dict((derivation.get("budget") or {}).get("limits") or {})
        budget = RetrievalBudget(
            max_queries=int(budget_payload.get("max_expansion_retrieval_queries", 6) or 6),
            max_candidates=int(budget_payload.get("max_expansion_retrieval_candidates", 64) or 64),
            max_exact_reads=int(budget_payload.get("max_expansion_exact_reads", 24) or 24),
            max_model_calls=int(budget_payload.get("max_expansion_planner_model_calls", 3) or 3),
            max_tokens=int(budget_payload.get("max_expansion_planner_tokens", 24000) or 24000),
        )
        observation_hash = stable_expansion_hash(
            "retrieval-planning-observation",
            request.loop_id,
            request.round_id,
            payload.get("frontier_hash"),
            derivation.get("mission"),
            tuple(request.scope.get("assignments") or ()),
        )
        planning = PlanningRetrievalSession.create(
            observation_hash=observation_hash,
            frontier_hash=str(payload.get("frontier_hash") or catalog.frontier_hash),
            catalog=catalog,
            planner_version=RetrievalBackedCognitiveAdvisor.VERSION,
            retrieval_version="authorized-lexical-retriever-v1",
            budget=budget,
        )
        async with self._sessions.begin() as session:
            existing = await self._derivation_artifacts.get_session(session, planning.session_id)
            indexes = await self._derivation_artifacts.indexes_by_ids(session, index_ids)
            planning = existing or planning
            if planning.frontier_hash != str(payload.get("frontier_hash") or catalog.frontier_hash):
                raise ValueError("planning retrieval session frontier 已 stale")
            await self._derivation_artifacts.save_session(
                session,
                planning,
                loop_id=request.loop_id,
                round_id=request.round_id,
                attempt_records=({"attempt": request.attempt, "outcome": "started"},),
            )
        async def checkpoint(current: PlanningRetrievalSession) -> None:
            async with self._sessions.begin() as session:
                await self._derivation_artifacts.save_session(
                    session,
                    current,
                    loop_id=request.loop_id,
                    round_id=request.round_id,
                    attempt_records=({"attempt": request.attempt, "outcome": current.state},),
                )

        timer = DerivationStageTimer(
            "retrieval_planning",
            (planning.session_id, *index_ids),
            RetrievalBackedCognitiveAdvisor.VERSION,
        )
        result = await RetrievalBackedCognitiveAdvisor(
            worker,
            checkpoint=checkpoint,
        ).plan(
            {**payload, "assignments": request.scope.get("assignments", [])},
            planning,
            indexes,
        )
        stage_record = timer.finish(
            (result.session.session_id,),
            result.blocker_summary or "已完成冻结 hierarchical retrieval",
            failure_code=result.blocker_code,
        )
        async with self._sessions.begin() as session:
            await self._derivation_artifacts.save_session(
                session,
                result.session,
                loop_id=request.loop_id,
                round_id=request.round_id,
                attempt_records=({"attempt": request.attempt, "outcome": result.session.state},),
            )
            existing_stage = await self._derivation_artifacts.stage_artifact(
                session,
                loop_id=request.loop_id,
                round_id=request.round_id,
                stage=stage_record.stage,
                input_identities=stage_record.input_identities,
                version=stage_record.version,
            )
            if existing_stage is None:
                await self._derivation_artifacts.put_stage_artifact(
                    session,
                    loop_id=request.loop_id,
                    round_id=request.round_id,
                    stage=stage_record.stage,
                    input_identities=stage_record.input_identities,
                    version=stage_record.version,
                    outcome="blocked" if result.blocker_code else "ready",
                    payload=stage_record.model_dump(mode="json"),
                    attempt_records=({"attempt": request.attempt, "outcome": result.session.state},),
                )
            else:
                stage_record = DerivationStageRecord.model_validate(existing_stage.payload)
        if result.proposal is None:
            return {
                "rationale": result.blocker_summary,
                "work_specs": [],
                "planning_blocker": {
                    "code": result.blocker_code,
                    "summary": result.blocker_summary,
                },
                "planning_session": result.session.model_dump(mode="json"),
                "portfolio_index_stage": derivation.get("portfolio_index_stage"),
                "retrieval_stage_record": stage_record.model_dump(mode="json"),
            }
        return {
            **result.proposal.model_dump(mode="json"),
            "planning_session": result.session.model_dump(mode="json"),
            "retrieval_candidates": tuple(item.model_dump(mode="json") for item in result.candidates),
            "exact_reads": tuple(item.model_dump(mode="json") for item in result.reads),
            "retrieved_manifests": tuple(item.model_dump(mode="json") for item in result.manifests),
            "portfolio_index_stage": derivation.get("portfolio_index_stage"),
            "retrieval_stage_record": stage_record.model_dump(mode="json"),
        }

    async def _run_derivation_role(self, request: LoopWorkerRequest) -> None:
        _payload, loop, _ = await self._evidence(request)
        model_name = (loop.equipment or {}).get("curator_model_name") or (loop.equipment or {}).get("model_name")
        worker = RoleBoundStructuredModel(
            self._app_config,
            request.kind,
            model_name,
        )
        role_payload = dict(request.scope.get("payload") or {})
        try:
            if request.kind == "semantic_index_projector":
                result = await worker.invoke(
                    SemanticProjectionProposal,
                    "你是无权 semantic_index_projector。只从输入冻结 segment 原文抽取原子 semantic units；分别输出自然语言 statement 与精确 supports，每个 support 的 message_id 必须来自输入且 quote 必须逐字复制原文。不得生成 WorkSpec、查询外部历史或执行状态变更。",
                    role_payload,
                )
            elif request.kind == "work_spec_reconciler":
                candidates = tuple(
                    WorkContextSpec.model_validate(item)
                    for item in tuple(role_payload.get("candidates") or ())
                )
                relations = await StructuredWorkSpecRelationEvaluator(worker).evaluate(candidates)
                result = {"relations": tuple(item.model_dump(mode="json") for item in relations)}
            elif request.kind == "dossier_synthesizer":
                result = await worker.invoke(
                    ContextSynthesisWorkerDraft,
                    "你是无权 dossier_synthesizer。只基于输入冻结 WorkSpec 与 resolved evidence 输出原子 claim graph；不得访问外部历史、改写工作或执行状态变更。",
                    role_payload,
                )
            elif request.kind == "claim_verifier":
                result = await worker.invoke(
                    ClaimSupportProposal,
                    "你是无权 claim_verifier。只判断输入 confirmed claims 是否被指定 citations 直接支持；不得补充证据、改写 claim 或执行工作。",
                    role_payload,
                )
            else:
                result = await worker.invoke(
                    QualityWorkerProposal,
                    "你是无权 context_quality_verifier。只返回 minimality、sufficiency、coherence 三维 verdict 与输入 identities；不得改写 work、dossier、evidence 或状态。",
                    role_payload,
                )
        finally:
            await self._record_usage(loop.loop_id, worker.usage)
        result_payload = result if isinstance(result, dict) else result.model_dump(mode="json")
        result_payload = {
            **result_payload,
            "worker_attempts": worker.last_attempt_records,
        }
        async with self._sessions.begin() as session:
            row = await session.get(LoopWorkerRequest, request.worker_request_id, with_for_update=True)
            loop_row = await session.get(AgentLoop, request.loop_id, with_for_update=True)
            if row is None or row.status != "running" or loop_row is None or loop_row.status != "running":
                return
            row.status = "success"
            row.result = result_payload
            row.completed_at = datetime.now(UTC)

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
        if assignment.state == "reading" or assignment.state == "analyzing":
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
