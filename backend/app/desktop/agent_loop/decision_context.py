"""本文件对外提供 PatrolDecisionContext、scoped_memory 与 DecisionSupplementRepository 的认知组合。

输入为不可变基础 Observation、持久 Curator 结果和 assessment；输出为带补充的模型读面与精确补充 identity。
scoped_memory 另接收 Context 集合，输出相关任务事项、增量与完整祖先，不重读当前世界。
具体工作流为只允许追加 worker_results/expansion_assessment，基础预算、授权、来源、血缘与 frontier 保持不变。
示例：PatrolDecisionContext(base, curator_results=results).model_observation()。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.agent_loop.task_progress.contracts import canonical_hash
from backend.app.desktop.agent_loop.task_progress.models import LoopDecisionSupplement
from backend.app.desktop.context_evolution.committed_lineage import (
    CommittedLineageReader,
)


def scoped_memory(observation: LoopObservationEnvelope, context_ids: set[str]) -> dict:
    progress, delta, lineage = (
        observation.previous_task_progress,
        observation.task_delta,
        observation.committed_lineage,
    )
    return {
        "decision_inputs_ref": observation.decision_inputs_ref,
        "stable_results": tuple(
            item
            for item in observation.stable_results
            if item.get("context_id") in context_ids
        ),
        "previous_task_progress": None
        if progress is None
        else {
            **progress,
            "items": [
                item
                for item in progress["items"]
                if not item.get("context_ids")
                or context_ids.intersection(item["context_ids"])
            ],
        },
        "task_delta": None
        if delta is None
        else {
            **delta,
            "sources": [
                item
                for item in delta["sources"]
                if item.get("context_id") is None
                or item.get("context_id") in context_ids
            ],
        },
        "committed_lineage": None
        if lineage is None
        else CommittedLineageReader.scoped(lineage, context_ids),
        "proposed_derivation_policy": "候选路径只属于 proposal；仅权威 publication 真正提交才改变真实 Lineage",
    }


@dataclass(frozen=True)
class PatrolDecisionContext:
    base: LoopObservationEnvelope
    curator_results: tuple[dict, ...] | None = None
    expansion_assessment: dict | None = None

    def model_observation(self) -> LoopObservationEnvelope:
        update = {}
        if self.curator_results is not None:
            update["worker_results"] = self.curator_results
        if self.expansion_assessment is not None:
            update["expansion_assessment"] = self.expansion_assessment
        return self.base.model_copy(deep=True, update=update)


class DecisionSupplementRepository:
    async def get(
        self, session: AsyncSession, observation_id: str, kind: str
    ) -> dict | None:
        row = await session.scalar(
            select(LoopDecisionSupplement).where(
                LoopDecisionSupplement.observation_id == observation_id,
                LoopDecisionSupplement.kind == kind,
            )
        )
        if row is None:
            return None
        if canonical_hash([observation_id, kind, row.payload]) != row.supplement_id:
            raise ValueError("认知补充 hash 不一致")
        return row.payload

    async def put(
        self, session: AsyncSession, observation_id: str, kind: str, payload: dict
    ) -> dict:
        if kind not in {"curator_results", "expansion_assessment"}:
            raise ValueError("认知补充不允许替换世界控制字段")
        existing = await self.get(session, observation_id, kind)
        if existing is not None:
            return existing
        identity = canonical_hash([observation_id, kind, payload])
        await session.execute(
            insert(LoopDecisionSupplement)
            .values(
                supplement_id=identity,
                observation_id=observation_id,
                kind=kind,
                payload=payload,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    LoopDecisionSupplement.observation_id,
                    LoopDecisionSupplement.kind,
                ]
            )
        )
        return await self.get(session, observation_id, kind)
