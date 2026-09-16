r"""本文件对外提供 LoopUsageDelta 与 LoopUsageLedger。

输入为 Main Run、Portfolio Patrol、Lane Curator 或 Completion Verifier 的标准模型用量及显式 retry 数；
输出为同一 LoopBudgetUsage 行上的原子累加。具体工作流为锁定 Loop 账本，统一累计调用、输入、输出
和 retry，调用方不得各自解释预算字段。示例：`await ledger.record(loop_id, delta)`。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.models import LoopBudgetUsage
from focus.runtime.runs.usage import ModelUsage


@dataclass(frozen=True, slots=True)
class LoopUsageDelta:
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    retries: int = 0

    @classmethod
    def from_model_usage(cls, usage: ModelUsage, *, retries: int = 0) -> "LoopUsageDelta":
        return cls(
            model_calls=usage.model_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            retries=retries,
        )


class LoopUsageLedger:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record(self, loop_id: str, delta: LoopUsageDelta) -> None:
        async with self._sessions.begin() as session:
            usage = await session.get(LoopBudgetUsage, loop_id, with_for_update=True)
            if usage is not None:
                self.apply(usage, delta)

    @staticmethod
    def apply(usage: LoopBudgetUsage, delta: LoopUsageDelta) -> None:
        usage.model_calls += max(0, delta.model_calls)
        usage.input_tokens += max(0, delta.input_tokens)
        usage.output_tokens += max(0, delta.output_tokens)
        usage.retries += max(0, delta.retries)
