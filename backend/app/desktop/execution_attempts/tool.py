"""本文件对外提供 ToolExecutionLedger 的工具执行意图与结果保存端口。

输入为稳定调用 identity、Run 与参数、注入的实时控制校验及 canonical ToolMessage；输出为新 claim、可靠缓存结果或不确定诊断。
具体工作流为同一短事务先锁控制版本并校验 Run，再提交唯一执行 claim；暂停后的新工具意图拒绝，已开始的调用仍保存结果。
已有意图但无结果时拒绝恢复重复副作用。本模块通过组合根注入 Loop 政策，不向下引用 Loop 领域。
示例：cached = await ledger.claim(identity, run_id, call)；await ledger.complete(identity, result)。
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from backend.app.desktop.models import DesktopRun, ToolExecutionAttempt
from focus.history import content_hash, deserialize_history_message, serialize_history_message
from focus.runtime.tool_attempts import ToolExecutionUncertain


class ToolExecutionLedger:
    def __init__(self, sessions, *, authority_validator=None):
        self._sessions = sessions
        self._authority_validator = authority_validator

    async def claim(self, identity, run_id, call):
        digest = content_hash(call)
        async with self._sessions() as session:
            run = await session.get(DesktopRun, run_id)
            if run is None:
                raise ToolExecutionUncertain("工具调用缺少持久 Run")
            if run.loop_id and run.round_id:
                if self._authority_validator is None:
                    raise ToolExecutionUncertain("Loop 工具调用缺少实时控制校验")
                await self._authority_validator(session, run)
            created = await session.scalar(insert(ToolExecutionAttempt).values(
                attempt_id=identity, run_id=run_id, call_id=call["id"], call_hash=digest, status="claimed"
            ).on_conflict_do_nothing().returning(ToolExecutionAttempt.attempt_id))
            if created:
                await session.commit()
                return None
            row = await session.scalar(select(ToolExecutionAttempt).where(ToolExecutionAttempt.attempt_id == identity).with_for_update())
            if row.call_hash != digest:
                raise ToolExecutionUncertain("工具 identity 参数冲突，禁止重试")
            if row.status != "completed" or row.result is None:
                raise ToolExecutionUncertain(f"工具 {call['name']} 已有执行意图但结果不确定；请核对副作用后通过新调用明确重试")
            return deserialize_history_message(row.result)

    async def complete(self, identity, result):
        async with self._sessions() as session:
            row = await session.scalar(select(ToolExecutionAttempt).where(ToolExecutionAttempt.attempt_id == identity).with_for_update())
            if row is None or row.status != "claimed":
                raise RuntimeError("工具结果缺少唯一执行 claim")
            row.status, row.result = "completed", serialize_history_message(result)
            await session.commit()
