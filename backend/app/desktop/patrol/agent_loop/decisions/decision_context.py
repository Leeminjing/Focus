"""本文件对外提供 PatrolDecisionContext、scoped_memory 与 DecisionSupplementRepository 的认知组合。

输入为不可变基础 Observation、持久 Curator 结果和 assessment；输出为带补充的模型读面与精确补充 identity。
scoped_memory 另接收 Context 集合，输出相关任务事项、增量、完整祖先与因果来源清单；具体 Oi 正文由调用方按冻结身份装配，不重读当前世界。
具体工作流为只允许追加 worker_results/expansion_assessment，基础预算、授权、来源、血缘与 frontier 保持不变。
示例：PatrolDecisionContext(base, curator_results=results).model_observation()。
scoped_memory 同时传递策略目录及作用域内用户原文和文件版本引用；正文由精确 hash 选读，不重读当前安装。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.patrol.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.capabilities.evolution_strategies import strategy_catalog
from backend.app.desktop.patrol.agent_loop.task_progress.contracts import canonical_hash
from backend.app.desktop.patrol.agent_loop.task_progress.models import LoopDecisionSupplement
from backend.app.desktop.contexts.evolution.committed_lineage import (
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
        "observation_id": observation.observation_id,
        "observation_generation": observation.generation,
        "evolution_strategies": strategy_catalog(observation.evolution_strategies),
        "user_intents": tuple(item for item in observation.user_intents
            if item.get("scope") == "portfolio" or item.get("context_id") in context_ids),
        "causal_chain": observation.causal_chain,
        "intervention": observation.intervention,
        "context_permissions": tuple(item for item in observation.portfolio_frontier
            if item.get("context_id") in context_ids),
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
        from backend.app.desktop.patrol.agent_loop.decisions.evidence_jobs import evidence_matches

        update = {}
        if self.curator_results is not None:
            results = { (item.get("request_id"), canonical_hash(item.get("result") or {})): item
                       for item in self.base.worker_results }
            for item in self.curator_results:
                if evidence_matches(self.base, item):
                    results[(item.get("request_id"), item["result_hash"])] = item
            update["worker_results"] = tuple(results.values())
        if self.expansion_assessment is not None:
            update["expansion_assessment"] = self.expansion_assessment
        return self.base.model_copy(deep=True, update=update)


class DecisionSupplementRepository:
    async def assessment(self, session, observation, context_ids=()):
        from backend.app.desktop.patrol.agent_loop.decisions.evidence_jobs import EvidenceJob, source_revisions

        job = await session.scalar(select(EvidenceJob).where(
            EvidenceJob.observation_id == (observation.observation_id or observation.decision_inputs_ref),
            EvidenceJob.observation_generation == observation.generation,
            EvidenceJob.evidence_kind == "expansion_assessment",
            EvidenceJob.required_source_revisions == source_revisions(observation, context_ids)))
        if job is None:
            # Pre-upgrade supplements were Observation-bound, whole-Portfolio
            # assessments. They cannot satisfy a different scoped dependency.
            from backend.app.desktop.patrol.agent_loop.models import LoopObservation

            base = await session.get(LoopObservation, observation.observation_id or observation.decision_inputs_ref)
            if (base is not None and base.generation == observation.generation
                    and (base.loop_id, base.round_id) == (observation.loop_id, observation.round_id)
                    and source_revisions(LoopObservationEnvelope.model_validate(base.envelope)) == source_revisions(observation)
                    and source_revisions(observation, context_ids) == source_revisions(observation)):
                return await self.get(session, observation.observation_id or observation.decision_inputs_ref, "expansion_assessment")
            return None
        if job.status != "ready":
            return None
        if job.result_hash != canonical_hash(job.result):
            raise ValueError("evidence_result_hash_mismatch")
        return job.result["assessment"]

    async def curator_results(self, session, observation):
        from backend.app.desktop.patrol.agent_loop.decisions.evidence_jobs import EvidenceJobRepository

        # Per-Context jobs are append-only evidence. Never seal a partial batch
        # into the old single, first-writer-wins supplement.
        return await EvidenceJobRepository().results(session, observation)

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
