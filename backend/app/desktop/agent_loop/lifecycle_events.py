"""本文件对外提供 LoopLifecycleEventRecorder，发布用户控制及当前事务指针。

输入为调用方事务中已锁定并完成状态转移的 AgentLoop；输出为携带持久控制版本、具有独立单调实体版本的幂等规范事件。
具体工作流为先发布当前 Round 的既有事实，再读取当前授权，发布 status、health、Round 指针、装备与授权快照；新轮次在冻结准备期间也有明确身份，相同状态不重复发布，运行阶段变化不推进控制版本；等待、暂停、撤权及后继轮共用同一入口。
本模块只记录既有事实，不推进控制版本、不启动执行；示例：await recorder.record(session, loop)。
"""

from sqlalchemy import select

from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.agent_loop.models import LoopDelegationGrant, LoopRound
from backend.app.desktop.agent_loop.round_events import RoundStateEventRecorder


class LoopLifecycleEventRecorder:
    async def record(self, session, loop, *, cause_id=None):
        await session.flush()
        current = await session.get(LoopRound, loop.current_round_id) if loop.current_round_id else None
        if current is not None:
            await RoundStateEventRecorder().record(session, current)
        grant = await session.scalar(select(LoopDelegationGrant).where(
            LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.revision == loop.authority_revision))
        grant_state = None if grant is None else {key: getattr(grant, key) for key in
            ("grant_id", "status", "capabilities", "context_scope", "permission_scope", "budgets", "delegable_gates", "compression_policy")}
        if grant_state is not None:
            grant_state["expires_at"] = grant.expires_at.isoformat() if grant.expires_at else None
        payload = {"status": loop.status, "health": loop.health, "waiting_reason": loop.waiting_reason,
                "current_round_id": loop.current_round_id, "current_portfolio_revision_id": loop.current_portfolio_revision_id,
                "revision": loop.revision, "authority_revision": loop.authority_revision, "goal_revision": loop.goal_revision,
                "equipment": loop.equipment, "grant": grant_state, "final_result": loop.final_result}
        latest = await session.scalar(select(LoopJournalEvent).where(
            LoopJournalEvent.loop_id == loop.loop_id, LoopJournalEvent.entity_type == "loop",
            LoopJournalEvent.entity_id == loop.loop_id).order_by(LoopJournalEvent.entity_revision.desc()).limit(1))
        if latest is not None and latest.payload == payload:
            return
        revision = max(1, latest.entity_revision if latest else 1) + 1
        await LoopEventJournal().append(session, loop.loop_id, CanonicalEventDraft(
            kind="loop.lifecycle.changed", entity_type="loop", entity_id=loop.loop_id, entity_revision=revision,
            causation_id=cause_id, payload=payload,
            idempotency_key=f"loop-lifecycle:{loop.loop_id}:entity:{revision}"))
