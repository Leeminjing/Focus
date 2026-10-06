"""本文件对外提供 LoopAccountingEventRecorder，发布既有账本的消费读模型。

输入为调用方短事务、Loop identity、receipt 来源及状态转移；输出为同事务幂等的 accounting 规范事件。
具体工作流为调用方先锁既有用量行，flush receipt，再由共享 AccountingQuery 读取实际、预留、未知及事务计数；
事件版本独立于 Loop 控制版本，不改变授权、冻结 Observation 或余额，也不创建第二套账本。
示例：await recorder.record(session, loop_id, source_kind="run_attempt", source_id=audit.attempt_id, transition="actual")。
"""

from sqlalchemy import func, select

from backend.app.desktop.agent_loop.accounting_query import LoopAccountingQuery
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.agent_loop.models import LoopBudgetUsage


class LoopAccountingEventRecorder:
    async def record(self, session, loop_id, *, source_kind, source_id, transition):
        await session.flush()
        usage = await session.get(LoopBudgetUsage, loop_id, with_for_update=True)
        if usage is None:
            return
        view = await LoopAccountingQuery().read(session, loop_id)
        revision = max(1, int(await session.scalar(select(func.max(LoopJournalEvent.entity_revision)).where(
            LoopJournalEvent.loop_id == loop_id, LoopJournalEvent.entity_type == "accounting",
            LoopJournalEvent.entity_id == loop_id)) or 0)) + 1
        await LoopEventJournal().append(session, loop_id, CanonicalEventDraft(
            kind="loop.accounting.updated", entity_type="accounting", entity_id=loop_id, entity_revision=revision,
            payload={"accounting": view, "usage": {**{name: getattr(usage, name) for name in
                ("model_calls", "input_tokens", "output_tokens", "retries")}, "rounds": view["completed_rounds"]},
                "source_kind": source_kind, "source_id": source_id, "transition": transition},
            idempotency_key=f"accounting:{source_kind}:{source_id}:{transition}"))
