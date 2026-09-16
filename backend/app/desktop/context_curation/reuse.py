r"""本文件对外提供 LaneReuseDecision、LaneReuseAction 与 LaneReuseResolver。

输入为 Program、规范化 purpose、选定 source frontier 和候选语义 fingerprint；输出为 create、
update 或 keep/no_change 决策及可复用的精确 Context revision。具体工作流为先定位稳定 Lane，
再比较有序 frontier hash 与去身份化消息 fingerprint；相同即复用，不同则更新同一 Lane。
示例：`decision = await resolver.resolve(session, program_id, purpose, frontier, fingerprint)`。
"""

from __future__ import annotations

from enum import StrEnum
import hashlib
import json

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.context_curation.models import CurationLane
from backend.app.desktop.context_curation.repository import CurationProgramRepository
from backend.app.desktop.context_evolution import (
    ContextRevisionRef,
    ContextRevisionRepository,
)


class LaneReuseAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    KEEP = "keep"


class LaneReuseDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: LaneReuseAction
    lane_id: str | None
    managed_context_id: str | None
    publisher_epoch: int | None
    base_context_revision: ContextRevisionRef | None
    source_frontier_hash: str
    semantic_fingerprint: str
    no_change: bool


class LaneReuseResolver:
    def __init__(self, revisions: ContextRevisionRepository) -> None:
        self._revisions = revisions

    async def resolve(
        self,
        session: AsyncSession,
        program_id: str,
        purpose: str,
        source_frontier: tuple[ContextRevisionRef, ...],
        semantic_fingerprint: str,
    ) -> LaneReuseDecision:
        normalized = CurationProgramRepository.normalize_purpose(purpose)
        frontier_hash = self.frontier_hash(source_frontier)
        lane = await session.scalar(
            select(CurationLane).where(
                CurationLane.program_id == program_id,
                CurationLane.normalized_purpose == normalized,
            )
        )
        if lane is None:
            return LaneReuseDecision(
                action=LaneReuseAction.CREATE,
                lane_id=None,
                managed_context_id=None,
                publisher_epoch=None,
                base_context_revision=None,
                source_frontier_hash=frontier_hash,
                semantic_fingerprint=semantic_fingerprint,
                no_change=False,
            )
        base = (
            await self._revisions.current(session, lane.managed_context_id)
            if lane.managed_context_id is not None
            else None
        )
        unchanged = (
            base is not None
            and lane.current_source_frontier_hash == frontier_hash
            and lane.current_semantic_fingerprint == semantic_fingerprint
        )
        return LaneReuseDecision(
            action=LaneReuseAction.KEEP if unchanged else LaneReuseAction.UPDATE,
            lane_id=lane.lane_id,
            managed_context_id=lane.managed_context_id,
            publisher_epoch=lane.publisher_epoch,
            base_context_revision=base.ref if base is not None else None,
            source_frontier_hash=frontier_hash,
            semantic_fingerprint=semantic_fingerprint,
            no_change=unchanged,
        )

    @staticmethod
    def frontier_hash(source_frontier: tuple[ContextRevisionRef, ...]) -> str:
        raw = json.dumps(
            [source.model_dump(mode="json") for source in source_frontier],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
