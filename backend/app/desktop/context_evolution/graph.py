r"""本文件对外提供完整 Context Evolution Graph 与第一父链兼容树查询服务。

输入为 workspace identity、不可变 revision repository 与调用方 AsyncSession；输出为包含全部
revision/source 边的图，或每个 Context 当前 revision 的确定性树投影。具体工作流为先加载完整
workspace revision 集合，图视图原样保留多来源；树视图穿透同 Context 历史到外部来源，并对
identity 反馈环确定性抑制一条展示边而不删除图证据。示例：`await service.graph(session, workspace_id)`。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.context_evolution.repository import ContextRevisionRepository
from backend.app.desktop.context_evolution.schemas import (
    ContextEvolutionEdge,
    ContextEvolutionGraph,
    ContextEvolutionNode,
    ContextFirstParentNode,
    ContextFirstParentTree,
    ContextRevisionContract,
    ContextRevisionProjectionStatus,
    ContextRevisionRef,
)
from backend.app.desktop.models import DesktopThread


class ContextEvolutionQueryService:
    def __init__(self, repository: ContextRevisionRepository) -> None:
        self._repository = repository

    async def graph(
        self,
        session: AsyncSession,
        workspace_id: str,
    ) -> ContextEvolutionGraph:
        revisions = await self._repository.list_workspace(session, workspace_id)
        tasks = await self._tasks(session, workspace_id)
        current_ids = {task.current_revision_id for task in tasks if task.current_revision_id}
        nodes = tuple(
            ContextEvolutionNode(
                ref=revision.ref,
                projection_status=revision.projection_status,
                content_hash=revision.content_hash,
                origin_kind=revision.origin_kind,
                origin_id=revision.origin_id,
                current=revision.ref.revision_id in current_ids,
                deleted=(
                    revision.deleted_at is not None
                    or revision.projection_status is ContextRevisionProjectionStatus.DELETED
                ),
            )
            for revision in revisions
        )
        edges = tuple(
            ContextEvolutionEdge(
                target=revision.ref,
                source=source.source,
                position=source.position,
            )
            for revision in revisions
            for source in revision.sources
        )
        return ContextEvolutionGraph(workspace_id=workspace_id, nodes=nodes, edges=edges)

    async def first_parent_tree(
        self,
        session: AsyncSession,
        workspace_id: str,
    ) -> ContextFirstParentTree:
        revisions = await self._repository.list_workspace(session, workspace_id)
        by_revision = {revision.ref.revision_id: revision for revision in revisions}
        tasks = await self._tasks(session, workspace_id)
        current = {
            task.task_id: by_revision[task.current_revision_id]
            for task in tasks
            if task.current_revision_id in by_revision
        }
        external = {
            context_id: self._external_sources(revision, by_revision)
            for context_id, revision in current.items()
        }
        proposed = {
            context_id: sources[0].context_id
            for context_id, sources in external.items()
            if sources and sources[0].context_id != context_id
        }
        parents, suppressed = self._acyclic_parents(proposed)
        depths = {context_id: self._depth(context_id, parents) for context_id in current}
        nodes = tuple(
            ContextFirstParentNode(
                context_id=task.task_id,
                title=task.title,
                lifecycle=self._lifecycle(task),
                current_revision=current[task.task_id].ref if task.task_id in current else None,
                primary_source=(external[task.task_id][0] if external.get(task.task_id) else None),
                secondary_sources=tuple(external.get(task.task_id, ())[1:]),
                tree_parent_context_id=parents.get(task.task_id),
                depth=depths.get(task.task_id, 0),
                cycle_suppressed=task.task_id in suppressed,
            )
            for task in tasks
        )
        return ContextFirstParentTree(workspace_id=workspace_id, nodes=nodes)

    async def _tasks(
        self,
        session: AsyncSession,
        workspace_id: str,
    ) -> list[DesktopThread]:
        return list(
            (
                await session.scalars(
                    select(DesktopThread)
                    .where(DesktopThread.workspace_id == workspace_id)
                    .order_by(DesktopThread.created_at, DesktopThread.task_id)
                )
            ).all()
        )

    def _external_sources(
        self,
        revision: ContextRevisionContract,
        by_revision: dict[str, ContextRevisionContract],
    ) -> tuple[ContextRevisionRef, ...]:
        found: list[ContextRevisionRef] = []
        visited: set[str] = set()

        def visit(candidate: ContextRevisionContract) -> None:
            if candidate.ref.revision_id in visited:
                return
            visited.add(candidate.ref.revision_id)
            for edge in candidate.sources:
                if edge.source.context_id != revision.ref.context_id:
                    if edge.source.revision_id not in {item.revision_id for item in found}:
                        found.append(edge.source)
                    continue
                previous = by_revision.get(edge.source.revision_id)
                if previous is not None:
                    visit(previous)

        visit(revision)
        return tuple(found)

    @staticmethod
    def _acyclic_parents(
        proposed: dict[str, str],
    ) -> tuple[dict[str, str], set[str]]:
        accepted: dict[str, str] = {}
        suppressed: set[str] = set()
        for child in sorted(proposed):
            parent = proposed[child]
            cursor = parent
            seen = {child}
            while cursor in accepted and cursor not in seen:
                seen.add(cursor)
                cursor = accepted[cursor]
            if cursor in seen:
                suppressed.add(child)
            else:
                accepted[child] = parent
        return accepted, suppressed

    @staticmethod
    def _depth(context_id: str, parents: dict[str, str]) -> int:
        depth = 0
        cursor = context_id
        visited: set[str] = set()
        while cursor in parents and cursor not in visited:
            visited.add(cursor)
            cursor = parents[cursor]
            depth += 1
        return depth

    @staticmethod
    def _lifecycle(task: DesktopThread) -> str:
        if task.deleted_at is not None:
            return "deleted"
        if task.archived_at is not None:
            return "archived"
        return "active"
