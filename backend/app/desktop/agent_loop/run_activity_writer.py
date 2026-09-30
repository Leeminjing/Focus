r"""本文件对外提供 LoopRunActivityWriter 与 LoopRunActivityWriteError。

输入为 Loop 身份和已去重的安全活动草稿；输出为提交后的 journal 事件，或可在 Live 诊断中看到的失败记录。
具体工作流为先独立提交可信 Test/Artifact 领域结果，再使用独立事务追加幂等展示事件，短暂写入失败后重试；
领域结果保留不依赖 journal retention。耗尽重试时记录降级单元并向调用方报告失败，后续重放成功则解除降级。
示例：`event = await writer.write(draft)`。
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.event_contract import (
    CanonicalEventDraft,
    CanonicalEventEnvelope,
)
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.projection_recovery import (
    ProjectionRecoveryRepository,
)
from backend.app.desktop.domain_evidence.repository import DomainResultRepository

logger = logging.getLogger(__name__)


class LoopRunActivityWriteError(RuntimeError):
    pass


class LoopRunActivityWriter:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], loop_id: str) -> None:
        self._sessions = sessions
        self._loop_id = loop_id
        self._journal = LoopEventJournal()
        self._recovery = ProjectionRecoveryRepository()
        self._failed_units: set[str] = set()

    async def write(self, draft: CanonicalEventDraft) -> CanonicalEventEnvelope | None:
        identity = draft.idempotency_key or draft.event_id or f"{draft.kind}:{draft.entity_id}"
        unit_id = identity if len(identity) <= 160 else uuid.uuid5(uuid.NAMESPACE_URL, identity).hex
        await self._persist_domain(draft)
        for attempt in range(3):
            try:
                async with self._sessions.begin() as session:
                    event = await self._journal.append(session, self._loop_id, draft)
                    if event is not None and unit_id in self._failed_units:
                        await self._recovery.resolve(
                            session, loop_id=self._loop_id, projector_name="run_activity",
                            unit_kind="event", unit_id=unit_id,
                        )
                self._failed_units.discard(unit_id)
                return event
            except Exception as exc:
                if attempt < 2:
                    await asyncio.sleep(0.05 * (2 ** attempt))
                    continue
                try:
                    async with self._sessions.begin() as session:
                        await self._recovery.record_failure(
                            session, loop_id=self._loop_id, projector_name="run_activity",
                            unit_kind="event", unit_id=unit_id, error=exc, max_attempts=1,
                        )
                    self._failed_units.add(unit_id)
                except Exception:
                    logger.exception("Loop Run activity failure diagnostic could not be persisted: %s", unit_id)
                raise LoopRunActivityWriteError(f"Loop Run activity journal append failed: {unit_id}") from exc

    async def _persist_domain(self, draft: CanonicalEventDraft) -> None:
        if draft.kind == "context.test.completed":
            kind, fields = "test", ("status", "summary", "metrics")
        elif draft.kind == "context.artifact.observed" and draft.payload.get("path"):
            kind, fields = "artifact", ("path", "status")
        else:
            return
        async with self._sessions.begin() as session:
            await DomainResultRepository().record(session, kind=kind, source_id=draft.entity_id, loop_id=self._loop_id, context_id=draft.payload.get("context_id"), run_id=draft.payload.get("run_id"), payload={key: draft.payload[key] for key in fields if key in draft.payload})
