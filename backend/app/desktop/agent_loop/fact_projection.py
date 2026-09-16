r"""本文件对外提供 LoopFactProjectionService 的按 Run 游标事实查询端口。

输入为 Loop id、Context/类型/状态过滤、反向 Run 游标与页大小；输出为该 Run 页对应的可追溯
run、workspace、artifact、tool 与 test 事实。具体工作流为先用 SQL 限定 Run 页，再读取每个 Run
发布的不可变 revision 增量并交给纯事实构造器，避免扫描整个 Loop 历史。示例：
`await service.read(session, loop_id, context_id=None, before=None, limit=20)`。
"""

from __future__ import annotations

from collections import Counter
import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.fact_sources import LoopFactBuilder, TestResultParser
from backend.app.desktop.agent_loop.models import AgentLoop, LoopContextMembership
from backend.app.desktop.context_evolution import ContextRevisionReader, ContextRevisionRepository
from backend.app.desktop.context_evolution.models import ContextRevision
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor


class LoopFactProjectionService:
    def __init__(self, checkpointer) -> None:
        self._repository = ContextRevisionRepository()
        self._reader = ContextRevisionReader(self._repository, checkpointer)

    async def read(
        self,
        session: AsyncSession,
        loop_id: str,
        *,
        context_id: str | None,
        kind: str | None,
        status: str | None,
        before: int | None,
        limit: int,
    ) -> dict:
        loop = await session.get(AgentLoop, loop_id)
        if loop is None:
            raise HTTPException(404, "Agent Loop 不存在")
        context_ids = await self._context_ids(session, loop_id)
        if context_id and context_id not in context_ids:
            raise HTTPException(404, "Context 不属于当前 Loop")
        selected = [context_id] if context_id else sorted(context_ids)
        total_runs, start, end, runs = await self._run_page(
            session,
            loop_id,
            selected,
            before,
            limit,
        )
        facts = await self._facts_for_runs(session, runs)
        if kind:
            facts = [item for item in facts if item["kind"] == kind]
        if status:
            facts = [item for item in facts if item["status"] == status]
        facts.sort(key=lambda item: (item.get("occurred_at") or "", item["fact_id"]))
        return {
            "loop_id": loop_id,
            "cursor_unit": "run",
            "total": total_runs,
            "total_runs": total_runs,
            "total_facts_in_page": len(facts),
            "range": {"start": start, "end": end},
            "next_before": start if start > 0 else None,
            "has_more": start > 0,
            "facts": facts,
        }

    @staticmethod
    async def _context_ids(session: AsyncSession, loop_id: str) -> set[str]:
        rows = await session.scalars(
            select(LoopContextMembership.context_id).where(
                LoopContextMembership.loop_id == loop_id
            )
        )
        return set(rows.all())

    @staticmethod
    async def _run_page(
        session: AsyncSession,
        loop_id: str,
        context_ids: list[str],
        before: int | None,
        limit: int,
    ) -> tuple[int, int, int, list[DesktopRun]]:
        if not context_ids:
            return 0, 0, 0, []
        predicate = (
            DesktopRun.loop_id == loop_id,
            DesktopRun.task_id.in_(context_ids),
        )
        total = int(
            await session.scalar(
                select(func.count()).select_from(DesktopRun).where(*predicate)
            )
            or 0
        )
        end = total if before is None else min(max(before, 0), total)
        start = max(0, end - limit)
        runs = list(
            (
                await session.scalars(
                    select(DesktopRun)
                    .where(*predicate)
                    .order_by(DesktopRun.created_at, DesktopRun.run_id)
                    .offset(start)
                    .limit(end - start)
                )
            ).all()
        )
        return total, start, end, runs

    async def _facts_for_runs(
        self,
        session: AsyncSession,
        runs: list[DesktopRun],
    ) -> list[dict]:
        if not runs:
            return []
        run_ids = [row.run_id for row in runs]
        anchors = {
            row.run_id: row
            for row in (
                await session.scalars(
                    select(RunExecutionAnchor).where(RunExecutionAnchor.run_id.in_(run_ids))
                )
            ).all()
        }
        revision_rows = list(
            (
                await session.scalars(
                    select(ContextRevision).where(
                        ContextRevision.origin_kind == "run_settled",
                        ContextRevision.origin_id.in_(run_ids),
                    )
                )
            ).all()
        )
        revision_ids = {row.origin_id: row.revision_id for row in revision_rows}
        facts: list[dict] = []
        for run in runs:
            facts.append(LoopFactBuilder.run_fact(run))
            facts.extend(LoopFactBuilder.workspace_facts(run, anchors.get(run.run_id)))
            revision_id = revision_ids.get(run.run_id)
            if revision_id:
                facts.extend(await self._tool_facts(session, run, revision_id))
        return facts

    async def _tool_facts(
        self,
        session: AsyncSession,
        run: DesktopRun,
        revision_id: str,
    ) -> list[dict]:
        revision = await self._repository.get_by_id(session, revision_id)
        current = await self._reader.read(session, revision.ref, "display")
        previous_messages: tuple[dict[str, Any], ...] = ()
        if revision.sources:
            previous = await self._reader.read(session, revision.sources[0].source, "display")
            previous_messages = previous.messages
        delta = self._message_delta(previous_messages, current.messages)
        return LoopFactBuilder.tool_facts(run, revision_id, delta)

    @classmethod
    def _message_delta(
        cls,
        previous: tuple[dict[str, Any], ...],
        current: tuple[dict[str, Any], ...],
    ) -> list[dict[str, Any]]:
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

    @staticmethod
    def _test_fact(content: str, tool_name: str | None = "pytest") -> dict | None:
        return TestResultParser.parse(tool_name, content)
