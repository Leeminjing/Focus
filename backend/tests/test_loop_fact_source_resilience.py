r"""本文件验证 RunFactSourceReader 面对已清理历史 checkpoint 时的降级边界。

输入为存在 revision 元数据但读取 checkpoint 抛出 ContextRevisionNotFound 的 Run；输出为不抛异常且不伪造 tool/test
候选的断言。具体工作流为替换 reader 的两个只读依赖并直接执行受保护的消息派生 helper，证明仅明确的历史缺失被吸收。
示例：`pytest backend/tests/test_loop_fact_source_resilience.py`。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.app.desktop.agent_loop.materialized_fact_sources import RunFactSourceReader
from backend.app.desktop.context_evolution import ContextRevisionNotFound


class _UnusedCheckpointer:
    pass


def test_missing_historical_checkpoint_skips_only_message_derived_facts() -> None:
    async def run() -> None:
        source = RunFactSourceReader(_UnusedCheckpointer())

        async def get_by_id(_session, _revision_id):
            return SimpleNamespace(ref=SimpleNamespace(), sources=())

        async def read(_session, _ref, _purpose):
            raise ContextRevisionNotFound("revision checkpoint 不存在")

        source._repository.get_by_id = get_by_id
        source._reader.read = read

        facts = await source._tool_facts(None, SimpleNamespace(), "revision-1")

        assert facts == []

    asyncio.run(run())
