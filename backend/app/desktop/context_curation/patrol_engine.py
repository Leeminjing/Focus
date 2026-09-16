r"""本文件对外提供 DirectCurationRequest、PatrolCurationDecision 与 PatrolCurationEngine。

输入为 Patrol 已完成判断的权威 frontier hash、Lane plans 和多来源 evidence；输出为包含完整
compiled candidates 的单一 Patrol decision，且零 Worker request/attempt。具体工作流为在直达预算内
逐 Lane 验证并调用确定性 compiler，keep/pause/retire 保留控制操作，绝不发布或启动 Run。
示例：`decision = engine.decide_direct(request)`。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field

from backend.app.desktop.context_curation.compiler import (
    CompiledLaneCandidate,
    compile_lane,
)
from backend.app.desktop.context_curation.contracts import (
    CreateLanePlan,
    LanePlan,
    LanePlanValue,
    MultiSourceEvidence,
    PortfolioLanePlan,
    UpdateLanePlan,
)
from backend.app.desktop.context_curation.lane_curator import LaneCuratorOutcome


class PatrolCurationError(RuntimeError):
    pass


class DirectCurationBudgetExceeded(PatrolCurationError):
    pass


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DirectCurationRequest(_FrozenModel):
    program_id: str = Field(min_length=1)
    portfolio_revision_id: str = Field(min_length=1)
    observed_frontier_hash: str = Field(min_length=64, max_length=64)
    rationale: str = Field(min_length=1, max_length=2000)
    plan: PortfolioLanePlan
    evidence: MultiSourceEvidence


class PatrolLaneDecision(_FrozenModel):
    plan: LanePlanValue
    candidate: CompiledLaneCandidate | None


class PatrolCurationDecision(_FrozenModel):
    decision_id: str
    program_id: str
    portfolio_revision_id: str
    observed_frontier_hash: str
    rationale: str
    lanes: tuple[PatrolLaneDecision, ...]
    worker_request_ids: tuple[str, ...] = ()
    worker_attempt_ids: tuple[str, ...] = ()


class WorkerReviewVerdict(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    RETRY = "retry"


class PatrolWorkerReview(_FrozenModel):
    request_id: str
    attempt_id: str
    verdict: Literal["accept", "reject", "retry"]
    rationale: str = Field(min_length=1, max_length=2000)
    candidate: CompiledLaneCandidate | None


class PatrolCurationEngine:
    def __init__(self, *, max_direct_lane_mutations: int = 8) -> None:
        if max_direct_lane_mutations < 1:
            raise ValueError("max_direct_lane_mutations 必须大于零")
        self._max_direct_lane_mutations = max_direct_lane_mutations

    def decide_direct(
        self,
        request: DirectCurationRequest | dict,
    ) -> PatrolCurationDecision:
        parsed = (
            request
            if isinstance(request, DirectCurationRequest)
            else DirectCurationRequest.model_validate(request)
        )
        mutations = sum(
            isinstance(plan, (CreateLanePlan, UpdateLanePlan))
            for plan in parsed.plan.lanes
        )
        if mutations > self._max_direct_lane_mutations:
            raise DirectCurationBudgetExceeded(
                f"直接策展 Lane 数 {mutations} 超过本轮预算 {self._max_direct_lane_mutations}"
            )
        lanes = tuple(
            PatrolLaneDecision(
                plan=plan,
                candidate=(
                    compile_lane(LanePlan(root=plan), parsed.evidence)
                    if isinstance(plan, (CreateLanePlan, UpdateLanePlan))
                    else None
                ),
            )
            for plan in parsed.plan.lanes
        )
        return PatrolCurationDecision(
            decision_id=uuid.uuid4().hex,
            program_id=parsed.program_id,
            portfolio_revision_id=parsed.portfolio_revision_id,
            observed_frontier_hash=parsed.observed_frontier_hash,
            rationale=parsed.rationale,
            lanes=lanes,
        )

    def review_worker(
        self,
        outcome: LaneCuratorOutcome,
        verdict: WorkerReviewVerdict,
        rationale: str,
    ) -> PatrolWorkerReview:
        if verdict is WorkerReviewVerdict.ACCEPT and outcome.candidate is None:
            raise PatrolCurationError("不能接受没有 candidate 的 Worker 结果")
        return PatrolWorkerReview(
            request_id=outcome.request_id,
            attempt_id=outcome.attempt_id,
            verdict=verdict.value,
            rationale=rationale,
            candidate=(
                outcome.candidate if verdict is WorkerReviewVerdict.ACCEPT else None
            ),
        )
