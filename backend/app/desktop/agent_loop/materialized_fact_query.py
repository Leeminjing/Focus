r"""本文件对外提供 MaterializedFactQueryService 与 FactParityService。

输入为 Loop、事实过滤、事实游标和可选旧版事实列表；输出为 current fact 页、单事实完整修订/关系历史或 parity 差异。
具体工作流为直接读取 `loop_facts` current rows，以 fact_id/occurred_at 稳定分页，详情连接不可变 revisions 和双向关系；
parity 仅比较规范化类型、来源与展示语义，不改变读取状态。示例：`page = await service.read(session, loop_id, ...)`。
"""

from __future__ import annotations

import json

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.fact_models import LoopFact, LoopFactRelationship, LoopFactRevision
from backend.app.desktop.agent_loop.models import AgentLoop, LoopContextMembership


class MaterializedFactQueryService:
    async def read(
        self,
        session: AsyncSession,
        loop_id: str,
        *,
        context_id: str | None,
        kind: str | None,
        status: str | None,
        before: int | None,
        limit: int,
    ) -> dict:
        await self._authorize_scope(session, loop_id, context_id)
        predicates = [LoopFact.loop_id == loop_id]
        if context_id:
            predicates.append(LoopFact.source_context_id == context_id)
        if kind:
            predicates.append(LoopFact.fact_type == kind)
        if status:
            predicates.append(LoopFact.state == status)
        total = int(await session.scalar(select(func.count()).select_from(LoopFact).where(*predicates)) or 0)
        end = total if before is None else min(max(before, 0), total)
        start = max(0, end - max(1, limit))
        rows = tuple((await session.scalars(select(LoopFact).where(*predicates).order_by(LoopFact.occurred_at, LoopFact.fact_id).offset(start).limit(end - start))).all())
        return {
            "loop_id": loop_id,
            "cursor_unit": "fact",
            "total": total,
            "total_facts_in_page": len(rows),
            "range": {"start": start, "end": end},
            "next_before": start if start > 0 else None,
            "has_more": start > 0,
            "facts": [self.serialize(row) for row in rows],
        }

    async def detail(self, session: AsyncSession, loop_id: str, fact_id: str) -> dict:
        fact = await session.get(LoopFact, fact_id)
        if fact is None or fact.loop_id != loop_id:
            raise HTTPException(404, "Fact 不存在")
        revisions = tuple((await session.scalars(select(LoopFactRevision).where(LoopFactRevision.fact_id == fact_id).order_by(LoopFactRevision.revision))).all())
        relationships = tuple((await session.scalars(select(LoopFactRelationship).where(LoopFactRelationship.loop_id == loop_id, or_(LoopFactRelationship.source_fact_id == fact_id, LoopFactRelationship.target_fact_id == fact_id)).order_by(LoopFactRelationship.created_at, LoopFactRelationship.relationship_id))).all())
        return {
            "fact": self.serialize(fact),
            "revisions": [
                {
                    "revision": row.revision,
                    "state": row.state,
                    "presentation": row.presentation,
                    "evidence": row.evidence,
                    "observer": row.observer,
                    "verifier": row.verifier,
                    "reason": row.reason,
                    "cause_event_id": row.cause_event_id,
                    "occurred_at": row.occurred_at.isoformat(),
                    "created_at": row.created_at.isoformat(),
                }
                for row in revisions
            ],
            "relationships": [
                {
                    "relation": row.relation,
                    "source_fact_id": row.source_fact_id,
                    "target_fact_id": row.target_fact_id,
                    "cause_event_id": row.cause_event_id,
                }
                for row in relationships
            ],
        }

    @staticmethod
    def serialize(fact: LoopFact) -> dict:
        return {
            "fact_id": fact.fact_id,
            "revision": fact.current_revision,
            "context_id": fact.source_context_id,
            "run_id": fact.source_run_id,
            "kind": fact.fact_type,
            "status": fact.state,
            "normalized_subject": fact.normalized_subject,
            "title": fact.presentation.get("title"),
            "summary": fact.presentation.get("summary"),
            "metrics": fact.presentation.get("metrics") or {},
            "outcome_status": fact.presentation.get("outcome_status"),
            "evidence": fact.evidence,
            "observer": fact.observer,
            "verifier": fact.verifier,
            "correlation_id": fact.correlation_id,
            "occurred_at": fact.occurred_at.isoformat(),
            "updated_at": fact.updated_at.isoformat(),
        }

    @staticmethod
    async def _authorize_scope(session: AsyncSession, loop_id: str, context_id: str | None) -> None:
        loop = await session.get(AgentLoop, loop_id)
        if loop is None:
            raise HTTPException(404, "Agent Loop 不存在")
        if context_id is None:
            return
        membership = await session.scalar(select(LoopContextMembership.membership_id).where(LoopContextMembership.loop_id == loop_id, LoopContextMembership.context_id == context_id))
        if membership is None:
            raise HTTPException(404, "Context 不属于当前 Loop")


class FactParityService:
    @staticmethod
    def compare(legacy: list[dict], materialized: list[dict]) -> dict:
        legacy_keys = {FactParityService._key(item, legacy=True) for item in legacy}
        materialized_keys = {FactParityService._key(item, legacy=False) for item in materialized}
        return {
            "equal": legacy_keys == materialized_keys,
            "legacy_only": sorted(legacy_keys - materialized_keys),
            "materialized_only": sorted(materialized_keys - legacy_keys),
            "intentional_differences": [
                "materialized status 表示事实验证生命周期；旧版 status 混合了测试业务结果，后者现位于 outcome_status",
                "materialized evidence 使用类型化引用并保留不可变 revision history",
            ],
        }

    @staticmethod
    def _key(item: dict, *, legacy: bool) -> str:
        evidence = item.get("evidence") or {}
        if isinstance(evidence, list):
            source_run = item.get("run_id") or next((entry.get("run_id") for entry in evidence if isinstance(entry, dict) and entry.get("run_id")), None)
        else:
            source_run = evidence.get("run_id")
        payload = {
            "kind": item.get("kind"),
            "run_id": source_run,
            "summary": item.get("summary"),
            "metrics": item.get("metrics") or {},
            "outcome_status": item.get("status") if legacy else item.get("outcome_status"),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
