r"""本文件对外提供 RunFactSourceReader，将已提交 Run 证据转换为确定性候选事实字典。

输入为 DesktopRun、RunExecutionAnchor、Run 产生的 Context revision 与可用 checkpoint 消息；输出为 run、workspace、artifact、
tool 与 test 候选。具体工作流为批量读取 anchor/revision，再计算消息增量并复用纯 LoopFactBuilder；历史 checkpoint 已清理时
保留无需 checkpoint 的事实并跳过消息派生候选；本模块不持久化事实，也不决定验证权力。示例：
`facts = await reader.read(session, (run,))`。
"""

from __future__ import annotations

from collections import Counter
import json
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.fact_sources import LoopFactBuilder
from backend.app.desktop.context_evolution import ContextRevisionNotFound, ContextRevisionReader, ContextRevisionRepository
from backend.app.desktop.context_evolution.models import ContextRevision
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor


class RunFactSourceReader:
    def __init__(self, checkpointer) -> None:
        self._repository = ContextRevisionRepository()
        self._reader = ContextRevisionReader(self._repository, checkpointer)

    async def read(self, session: AsyncSession, runs: Iterable[DesktopRun]) -> list[dict]:
        selected = tuple(runs)
        if not selected:
            return []
        run_ids = [row.run_id for row in selected]
        anchors = {row.run_id: row for row in (await session.scalars(select(RunExecutionAnchor).where(RunExecutionAnchor.run_id.in_(run_ids)))).all()}
        revisions = list((await session.scalars(select(ContextRevision).where(ContextRevision.origin_kind == "run_settled", ContextRevision.origin_id.in_(run_ids)))).all())
        revision_ids = {row.origin_id: row.revision_id for row in revisions}
        facts: list[dict] = []
        for run in selected:
            facts.append(LoopFactBuilder.run_fact(run))
            facts.extend(LoopFactBuilder.workspace_facts(run, anchors.get(run.run_id)))
            revision_id = revision_ids.get(run.run_id)
            if revision_id:
                facts.extend(await self._tool_facts(session, run, revision_id))
        return facts

    async def _tool_facts(self, session: AsyncSession, run: DesktopRun, revision_id: str) -> list[dict]:
        try:
            revision = await self._repository.get_by_id(session, revision_id)
            current = await self._reader.read(session, revision.ref, "display")
            previous_messages: tuple[dict[str, Any], ...] = ()
            if revision.sources:
                previous = await self._reader.read(session, revision.sources[0].source, "display")
                previous_messages = previous.messages
            return LoopFactBuilder.tool_facts(run, revision_id, self._message_delta(previous_messages, current.messages))
        except ContextRevisionNotFound:
            return []

    @classmethod
    def _message_delta(cls, previous: tuple[dict[str, Any], ...], current: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
        remaining = Counter(cls._message_key(message) for message in previous)
        delta: list[dict[str, Any]] = []
        for message in current:
            key = cls._message_key(message)
            if remaining[key] > 0:
                remaining[key] -= 1
            else:
                delta.append(message)
        return delta

    @staticmethod
    def _message_key(message: dict[str, Any]) -> str:
        identity = message.get("id")
        if identity:
            return f"id:{identity}"
        return "payload:" + json.dumps(message, ensure_ascii=False, sort_keys=True, default=str)
