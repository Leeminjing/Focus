"""本文件对外提供 LoopLiveHistoryService。

输入为 Loop identity、排他的 before_sequence 与有界 limit；输出为按时间正序排列的一页活动、下一页游标及保留边界。
具体工作流为每页复查当前权限，利用 journal sequence 索引逆序取 limit+1 条并脱敏；历史不改变 SSE 或快照游标。
示例：`await LoopLiveHistoryService().read(session, loop_id, before_sequence=100, limit=50)`。"""

from sqlalchemy import select

from backend.app.desktop.agent_loop.event_contract import LiveEventAuthorizationPolicy
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent, LoopJournalSequence, LoopReplayRetention
from backend.app.desktop.agent_loop.live_access import LoopLiveAccessPolicy
from backend.app.desktop.agent_loop.live_projection_reducer import LoopLiveProjectionReducer


class LoopLiveHistoryService:
    async def read(self, session, loop_id: str, before_sequence: int | None = None, limit: int = 50) -> dict:
        access = await LoopLiveAccessPolicy().resolve(session, loop_id)
        sequence = await session.get(LoopJournalSequence, loop_id)
        boundary = int(sequence.last_sequence if sequence else 0)
        before = min(before_sequence, boundary + 1) if before_sequence is not None else boundary + 1
        limit = max(1, min(limit, 200))
        retention = await session.get(LoopReplayRetention, loop_id)
        minimum = int(retention.minimum_sequence if retention else 1)
        rows = tuple(await session.scalars(select(LoopJournalEvent).where(
            LoopJournalEvent.loop_id == loop_id, LoopJournalEvent.sequence < before,
            LoopJournalEvent.sequence >= minimum,
        ).order_by(LoopJournalEvent.sequence.desc()).limit(limit + 1)))
        page = rows[:limit]
        policy = LiveEventAuthorizationPolicy()
        activities = [LoopLiveProjectionReducer._activity(policy.public_envelope(
            LoopEventJournal._envelope(row), access.permissions)).model_dump(mode="json") for row in reversed(page)]
        return {"loop_id": loop_id, "last_sequence": boundary, "authority_revision": access.authority_revision,
                "items": activities, "next_before_sequence": page[-1].sequence if len(rows) > limit else None,
                "minimum_sequence": minimum, "truncated": minimum > 1}
