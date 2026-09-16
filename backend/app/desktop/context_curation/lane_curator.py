r"""本文件对外提供 LaneCuratorAssignment、LaneCuratorRunner、Worker backend 与结果合同。

输入为 Patrol 主动创建的单 Lane purpose、精确来源、策略、预算和 candidate identity；输出为不可
提交的 CompiledLaneCandidate 或持久化错误证据。具体工作流为每个 assignment 独立记录 attempt，
并行调用无权 backend，严格校验输出与 scope 后编译；Worker 夹带的 mutation 字段整体拒绝。
示例：`outcomes = await runner.run_many(assignments)`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.context_curation.compiler import (
    CompiledLaneCandidate,
    compile_lane,
)
from backend.app.desktop.context_curation.contracts import (
    CreateLanePlan,
    LanePlan,
    MultiSourceEvidence,
    UpdateLanePlan,
)
from backend.app.desktop.context_curation.models import (
    CurationAttempt,
    CurationAttemptStatus,
    CurationWorkerKind,
)
from backend.app.desktop.context_curation.repository import PortfolioRepository
from backend.app.desktop.context_evolution import ContextRevisionRef


class LaneCuratorError(RuntimeError):
    pass


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LaneCuratorBudget(_FrozenModel):
    max_source_messages: int = Field(ge=1, le=5000)
    max_input_chars: int = Field(ge=1, le=2_000_000)
    max_output_chars: int = Field(ge=1, le=500_000)


class LaneCuratorAssignment(_FrozenModel):
    request_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    attempt_number: int = Field(ge=1)
    program_id: str = Field(min_length=1)
    portfolio_revision_id: str = Field(min_length=1)
    action: Literal["create", "update"]
    lane_id: str | None = None
    purpose: str = Field(min_length=1)
    base_context_revision: ContextRevisionRef | None = None
    publisher_epoch: int | None = Field(default=None, ge=1)
    source_frontier: tuple[ContextRevisionRef, ...] = Field(min_length=1)
    evidence: MultiSourceEvidence
    lane_policy: dict[str, Any] = Field(default_factory=dict)
    budget: LaneCuratorBudget

    @model_validator(mode="after")
    def _validate_scope(self) -> Self:
        if self.action == "create" and any(
            value is not None
            for value in (self.lane_id, self.base_context_revision, self.publisher_epoch)
        ):
            raise ValueError("create assignment 不得预先占用现有 Lane publisher")
        if self.action == "update" and any(
            value is None
            for value in (self.lane_id, self.base_context_revision, self.publisher_epoch)
        ):
            raise ValueError("update assignment 必须冻结 Lane、base revision 与 publisher epoch")
        frontier = set(self.source_frontier)
        evidence_sources = {bundle.source for bundle in self.evidence.sources}
        if frontier != evidence_sources:
            raise ValueError("Lane Curator evidence 必须恰好等于分配的 source frontier")
        message_count = sum(len(bundle.messages) for bundle in self.evidence.sources)
        if message_count > self.budget.max_source_messages:
            raise ValueError("Lane Curator source messages 超过预算")
        size = len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False))
        if size > self.budget.max_input_chars:
            raise ValueError("Lane Curator input 超过字符预算")
        return self


class LaneCuratorProposal(_FrozenModel):
    request_id: str
    rationale: str = Field(min_length=1, max_length=2000)
    plan: LanePlan


class LaneCuratorOutcome(_FrozenModel):
    request_id: str
    attempt_id: str
    candidate: CompiledLaneCandidate | None = None
    error: str | None = None


class LaneCuratorBackend(Protocol):
    async def curate(self, assignment: LaneCuratorAssignment) -> dict[str, Any]: ...


class LaneCuratorRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        repository: PortfolioRepository,
        backend: LaneCuratorBackend,
        model_name: str,
    ) -> None:
        self._session_factory = session_factory
        self._repository = repository
        self._backend = backend
        self._model_name = model_name

    async def run_many(
        self,
        assignments: tuple[LaneCuratorAssignment, ...],
    ) -> tuple[LaneCuratorOutcome, ...]:
        return tuple(await asyncio.gather(*(self._run_one(item) for item in assignments)))

    async def _run_one(self, assignment: LaneCuratorAssignment) -> LaneCuratorOutcome:
        attempt_id = await self._start_attempt(assignment)
        raw: dict[str, Any] = {}
        try:
            raw = await self._backend.curate(assignment)
            self._require_output_budget(raw, assignment.budget.max_output_chars)
            proposal = LaneCuratorProposal.model_validate(raw)
            self._require_scope(assignment, proposal)
            candidate = compile_lane(proposal.plan, assignment.evidence)
            await self._finish_attempt(attempt_id, raw, proposal, None)
            return LaneCuratorOutcome(
                request_id=assignment.request_id,
                attempt_id=attempt_id,
                candidate=candidate,
            )
        except Exception as exc:
            await self._finish_attempt(attempt_id, raw, None, str(exc))
            return LaneCuratorOutcome(
                request_id=assignment.request_id,
                attempt_id=attempt_id,
                error=str(exc),
            )

    async def _start_attempt(self, assignment: LaneCuratorAssignment) -> str:
        async with self._session_factory.begin() as session:
            attempt = await self._repository.add_attempt(
                session,
                assignment.candidate_id,
                assignment.attempt_number,
                CurationWorkerKind.LANE_CURATOR,
                self._model_name,
                assignment.model_dump(mode="json"),
                status=CurationAttemptStatus.RUNNING,
            )
            attempt.started_at = datetime.now(UTC)
            return attempt.attempt_id

    async def _finish_attempt(
        self,
        attempt_id: str,
        raw: dict[str, Any],
        proposal: LaneCuratorProposal | None,
        error: str | None,
    ) -> None:
        async with self._session_factory.begin() as session:
            attempt = await session.get(CurationAttempt, attempt_id, with_for_update=True)
            if attempt is None:
                raise LaneCuratorError(f"Curation attempt 不存在: {attempt_id}")
            attempt.raw_output = raw
            attempt.parsed_output = (
                proposal.model_dump(mode="json") if proposal is not None else {}
            )
            attempt.status = (
                CurationAttemptStatus.SUCCESS.value
                if error is None
                else CurationAttemptStatus.ERROR.value
            )
            attempt.error = error
            attempt.completed_at = datetime.now(UTC)

    @staticmethod
    def _require_scope(
        assignment: LaneCuratorAssignment,
        proposal: LaneCuratorProposal,
    ) -> None:
        if proposal.request_id != assignment.request_id:
            raise LaneCuratorError("Lane Curator 返回了错误 request identity")
        plan = proposal.plan.root
        if plan.action != assignment.action or plan.purpose != assignment.purpose:
            raise LaneCuratorError("Lane Curator 改变了分配的 action 或 purpose")
        if tuple(plan.source_frontier) != tuple(assignment.source_frontier):
            raise LaneCuratorError("Lane Curator 改变了分配的 source frontier")
        if isinstance(plan, CreateLanePlan):
            return
        if not isinstance(plan, UpdateLanePlan):
            raise LaneCuratorError("Lane Curator 只能返回 create/update candidate")
        if (
            plan.lane_id != assignment.lane_id
            or plan.base_context_revision != assignment.base_context_revision
            or plan.publisher_epoch != assignment.publisher_epoch
        ):
            raise LaneCuratorError("Lane Curator 改变了冻结的 Lane publisher scope")

    @staticmethod
    def _require_output_budget(raw: dict[str, Any], max_chars: int) -> None:
        size = len(json.dumps(raw, ensure_ascii=False))
        if size > max_chars:
            raise LaneCuratorError(
                f"Lane Curator output 字符数 {size} 超过预算 {max_chars}"
            )
