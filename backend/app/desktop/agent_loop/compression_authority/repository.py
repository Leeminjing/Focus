r"""本文件对外提供 CompressionAuthorityRepository 的候选、resolution 与 supersession 原子操作。

输入为已锁定的 AsyncSession、Loop/pending/candidate identities 与状态转换参数；输出为唯一事实行或
幂等已有行。具体工作流为遵循 Loop→grant→round→pending→candidate→resolution 锁序，接受时以
数据库唯一约束保障 single writer，用户覆盖时统一 supersede 尚未执行的工作。示例：
`await repository.supersede(session, loop_id, "user_override")`。
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.compression_authority.models import LoopCompressionCandidate, LoopCompressionResolution
from backend.app.desktop.agent_loop.models import LoopAction, LoopEventOutbox, LoopPendingDecision


class CompressionAuthorityRepository:
    async def live_candidate(self, session: AsyncSession, pending_decision_id: str) -> LoopCompressionCandidate | None:
        return await session.scalar(
            select(LoopCompressionCandidate)
            .where(
                LoopCompressionCandidate.pending_decision_id == pending_decision_id,
                LoopCompressionCandidate.status.in_(["prepared", "accepted"]),
            )
            .with_for_update()
        )

    async def resolution_for_pending(self, session: AsyncSession, pending_decision_id: str) -> LoopCompressionResolution | None:
        return await session.scalar(
            select(LoopCompressionResolution)
            .where(LoopCompressionResolution.pending_decision_id == pending_decision_id)
            .with_for_update()
        )

    async def commit_resolution(
        self,
        session: AsyncSession,
        *,
        loop_id: str,
        round_id: str,
        grant_id: str,
        grant_revision: int,
        goal_revision: int,
        decision_id: str,
        action: LoopAction,
        candidate: LoopCompressionCandidate,
        pending: LoopPendingDecision,
        idempotency_key: str,
    ) -> LoopCompressionResolution:
        existing = await self.resolution_for_pending(session, pending.pending_decision_id)
        if existing is not None:
            return existing
        payload = {"type": "compression", "decision": "apply", "ranges": candidate.normalized_ranges}
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        resolution = LoopCompressionResolution(
            resolution_id=uuid.uuid4().hex,
            loop_id=loop_id,
            pending_decision_id=pending.pending_decision_id,
            candidate_id=candidate.candidate_id,
            decision_id=decision_id,
            action_id=action.action_id,
            round_id=round_id,
            grant_id=grant_id,
            grant_revision=grant_revision,
            goal_revision=goal_revision,
            resume_payload_hash=payload_hash,
            idempotency_key=idempotency_key,
        )
        candidate.status = "accepted"
        pending.status = "resolving"
        action.status = "committed"
        session.add(resolution)
        session.add(
            LoopEventOutbox(
                event_id=uuid.uuid4().hex,
                loop_id=loop_id,
                sequence=await self._next_sequence(session, loop_id),
                event_type="CompressionResolutionCommitted",
                payload={"resolution_id": resolution.resolution_id, "candidate_id": candidate.candidate_id},
                idempotency_key=f"compression-resolution:{resolution.resolution_id}",
            )
        )
        return resolution

    async def supersede(self, session: AsyncSession, loop_id: str, reason: str) -> None:
        candidates = list(
            (
                await session.scalars(
                    select(LoopCompressionCandidate)
                    .where(
                        LoopCompressionCandidate.loop_id == loop_id,
                        LoopCompressionCandidate.status.in_(["prepared", "accepted"]),
                    )
                    .with_for_update()
                )
            ).all()
        )
        for candidate in candidates:
            candidate.status = "superseded"
        resolutions = list(
            (
                await session.scalars(
                    select(LoopCompressionResolution)
                    .where(
                        LoopCompressionResolution.loop_id == loop_id,
                        LoopCompressionResolution.status.in_(["committed", "resuming"]),
                    )
                    .with_for_update()
                )
            ).all()
        )
        for resolution in resolutions:
            resolution.status = "superseded"
            resolution.error_evidence = {"reason": reason, "at": datetime.now(UTC).isoformat()}
            pending = await session.get(LoopPendingDecision, resolution.pending_decision_id, with_for_update=True)
            if pending is not None and pending.status in {"pending", "resolving"}:
                pending.status = "superseded"

    @staticmethod
    async def _next_sequence(session: AsyncSession, loop_id: str) -> int:
        from sqlalchemy import func

        return int(
            await session.scalar(
                select(func.coalesce(func.max(LoopEventOutbox.sequence), 0)).where(LoopEventOutbox.loop_id == loop_id)
            )
            or 0
        ) + 1
