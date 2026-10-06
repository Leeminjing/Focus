"""本文件对外提供 RoundStateEventRecorder，发布事务轮次的规范状态事件。

输入为调用方事务和已锁定 LoopRound；输出为同事务可重放、幂等的 round 状态信封。
具体工作流为按该 Round identity 比较完整状态，相同事实不重复发布，屏障变化递增本地事件版本，保存冻结 Observation、唯一 Decision、屏障和单调轮号；
事件版本只属于该实体，首个事件从初始快照版本一递增，不代替 Loop 控制版本或冻结历史，也不启动执行。
示例：await RoundStateEventRecorder().record(session, round_row)，由观察冻结、收口或取消服务调用。
"""

import json

from sqlalchemy import select

from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent


class RoundStateEventRecorder:
    async def record(self, session, round_row):
        await session.flush()
        payload = {"round_id": round_row.round_id, "number": round_row.number, "status": round_row.status,
            "observation_id": round_row.observation_id, "decision_id": round_row.decision_id,
            "barrier": json.loads(json.dumps(round_row.barrier or {}))}
        latest = await session.scalar(select(LoopJournalEvent).where(
            LoopJournalEvent.loop_id == round_row.loop_id, LoopJournalEvent.entity_type == "round",
            LoopJournalEvent.entity_id == round_row.round_id).order_by(LoopJournalEvent.entity_revision.desc()).limit(1))
        if latest is not None and latest.payload == payload:
            return
        revision = max(1, latest.entity_revision if latest else 1) + 1
        await LoopEventJournal().append(session, round_row.loop_id, CanonicalEventDraft(
            kind="loop.round.state_changed", entity_type="round", entity_id=round_row.round_id,
            entity_revision=revision, correlation_id=round_row.round_id,
            payload=payload,
            idempotency_key=f"round-state:{round_row.round_id}:entity:{revision}"))
        from backend.app.desktop.agent_loop.accounting_events import LoopAccountingEventRecorder

        await LoopAccountingEventRecorder().record(session, round_row.loop_id,
            source_kind="round", source_id=round_row.round_id, transition=round_row.status)
