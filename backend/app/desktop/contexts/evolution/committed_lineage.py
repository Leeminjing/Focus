"""本文件对外提供 CommittedLineageReader 的已发布当前来源图冻结端口。

输入为授权 workspace 内的当前或已发布任务输入根；输出为根、直接来源身份、拓扑 hash 与可裁剪快照。
具体工作流为检查发布资格和 workspace 范围，构造当前根及其直接来源元数据，不读取永久 Revision 祖先正文。
示例：await CommittedLineageReader().snapshot(session, roots, workspace_id=workspace_id)。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.persistence.models import DesktopThread
from backend.app.desktop.runs.evidence.identity import canonical_hash

from .models import ContextInputSnapshot, ContextRevision
from .repository import ContextRevisionRepository


class CommittedLineageReader:
    async def published_roots(
        self, session: AsyncSession, context_ids: tuple[str, ...]
    ) -> dict[str, str]:
        return dict(
            (
                await session.execute(
                    select(ContextRevision.context_id, ContextRevision.revision_id)
                    .join(
                        DesktopThread,
                        DesktopThread.task_id == ContextRevision.context_id,
                    )
                    .where(
                        ContextRevision.context_id.in_(context_ids),
                        ContextRevision.revision_id
                        == DesktopThread.current_revision_id,
                        ContextRevision.projection_status.in_(
                            ("valid", "repaired", "approved", "deleted")
                        ),
                    )
                )
            ).all()
        )

    async def snapshot(
        self,
        session: AsyncSession,
        roots: dict[str, str],
        *,
        workspace_id: str,
        max_nodes: int = 10000,
    ) -> dict:
        repository = ContextRevisionRepository()
        nodes, edges = {}, []
        for context_id, token in sorted(roots.items()):
            value = await repository.get_by_id(session, token)
            value = await repository.get_input(session, value.ref)
            task = await session.get(DesktopThread, context_id)
            if (
                task is None
                or task.workspace_id != workspace_id
                or value.ref.context_id != context_id
            ):
                raise ValueError("provenance root crosses workspace scope")
            if task.current_revision_id != token:
                owner = await session.scalar(
                    select(ContextInputSnapshot.snapshot_key)
                    .where(
                        ContextInputSnapshot.revision_id == token,
                        ContextInputSnapshot.is_published.is_(True),
                    )
                    .limit(1)
                )
                if owner is None:
                    raise ValueError(
                        "provenance root is neither current nor frozen execution input"
                    )
            if value.projection_status.value not in {
                "valid",
                "repaired",
                "approved",
                "deleted",
            }:
                raise ValueError("provenance root is not runnable or deleted")
            nodes[token] = {
                "revision_id": token,
                "context_id": context_id,
                "generation": value.ref.generation,
                "redacted": value.deleted_at is not None,
            }
            for edge in value.sources:
                source = edge.source
                if source.context_id == context_id:
                    continue
                source_task = await session.get(DesktopThread, source.context_id)
                if source_task is not None and source_task.workspace_id != workspace_id:
                    raise ValueError("provenance source crosses workspace scope")
                nodes.setdefault(
                    source.revision_id,
                    {
                        "revision_id": source.revision_id,
                        "context_id": source.context_id,
                        "generation": source.generation,
                        "redacted": source_task is None
                        or source_task.deleted_at is not None,
                    },
                )
                edges.append(
                    {
                        "source_context_id": source.context_id,
                        "source_revision_id": source.revision_id,
                        "target_context_id": context_id,
                        "target_revision_id": token,
                    }
                )
        if len(nodes) > max_nodes:
            raise ValueError(
                "lineage_history_budget_exceeded: current provenance exceeds capacity"
            )
        edges.sort(
            key=lambda item: (item["target_revision_id"], item["source_revision_id"])
        )
        return {
            "schema_version": 1,
            "complete": True,
            "roots": roots,
            "nodes": sorted(nodes.values(), key=lambda item: item["revision_id"]),
            "edges": edges,
            "topology_hash": canonical_hash(edges),
        }

    @staticmethod
    def scoped(snapshot: dict, context_ids: set[str]) -> dict:
        selected_contexts = set(context_ids)
        changed = True
        while changed:
            changed = False
            for edge in snapshot["edges"]:
                if (
                    edge["target_context_id"] in selected_contexts
                    and edge["source_context_id"] not in selected_contexts
                ):
                    selected_contexts.add(edge["source_context_id"])
                    changed = True
        edges = [
            edge
            for edge in snapshot["edges"]
            if edge["source_context_id"] in selected_contexts
            and edge["target_context_id"] in selected_contexts
        ]
        return {
            **snapshot,
            "nodes": [
                node
                for node in snapshot["nodes"]
                if node["context_id"] in selected_contexts
            ],
            "edges": edges,
            "topology_hash": canonical_hash(edges),
            "roots": {
                key: value
                for key, value in snapshot["roots"].items()
                if key in context_ids
            },
        }
