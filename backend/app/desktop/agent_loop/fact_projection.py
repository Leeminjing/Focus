r"""本文件对外提供 LoopFactProjectionService 的旧版按 Run 游标事实查询端口。

输入为 Loop id、Context/类型/状态过滤、反向 Run 游标与页大小；输出为该 Run 页对应的可追溯
run、workspace、artifact、tool 与 test 事实。具体工作流为先用 SQL 限定 Run 页，再委托共享 RunFactSourceReader
读取不可变 revision 增量；该端口保留用于物化切换前的 parity。示例：
`await service.read(session, loop_id, context_id=None, before=None, limit=20)`。
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.fact_sources import TestResultParser
from backend.app.desktop.agent_loop.materialized_fact_sources import RunFactSourceReader
from backend.app.desktop.agent_loop.models import AgentLoop, LoopContextMembership
from backend.app.desktop.models import DesktopRun


class LoopFactProjectionService:
    def __init__(self, checkpointer) -> None:
        self._sources = RunFactSourceReader(checkpointer)

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
        return await self._sources.read(session, runs)

    @staticmethod
    def _test_fact(content: str, tool_name: str | None = "pytest") -> dict | None:
        return TestResultParser.parse(tool_name, content)
