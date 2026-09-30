"""本文件对外提供 CommittedLineageReader 的真实已发布 Revision 祖先读面。

输入为只读事务和授权 workspace 内的已发布根；输出为完整祖先 DAG、外部来源边和拓扑 hash。
具体工作流为验证根的 publication receipt/current proof，分批回溯全部来源，保留多父与外部祖先；
普通同 Context 来源参与回溯但不成为派生边，prepared 候选不能用作根。读取不更改拓扑。
示例：await CommittedLineageReader().snapshot(session, {"test": "r3"}, workspace_id="w")。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.context_evolution.models import (
    ContextPublicationReceipt,
    ContextRevision,
    ContextRevisionSource,
)
from backend.app.desktop.domain_evidence.identity import canonical_hash
from backend.app.desktop.models import DesktopThread


class CommittedLineageReader:
    async def published_roots(
        self, session: AsyncSession, context_ids: tuple[str, ...]
    ) -> dict[str, str]:
        eligible = ("valid", "repaired", "approved", "deleted")
        current = dict(
            (
                await session.execute(
                    select(DesktopThread.task_id, DesktopThread.current_revision_id)
                    .join(
                        ContextRevision,
                        ContextRevision.revision_id
                        == DesktopThread.current_revision_id,
                    )
                    .where(
                        DesktopThread.task_id.in_(context_ids),
                        ContextRevision.projection_status.in_(eligible),
                    )
                )
            ).all()
        )
        missing = set(context_ids) - set(current)
        if missing:
            history = (
                await session.execute(
                    select(
                        ContextPublicationReceipt.context_id,
                        ContextPublicationReceipt.revision_id,
                    )
                    .join(
                        ContextRevision,
                        ContextRevision.revision_id
                        == ContextPublicationReceipt.revision_id,
                    )
                    .where(
                        ContextPublicationReceipt.context_id.in_(missing),
                        ContextRevision.projection_status.in_(eligible),
                    )
                    .order_by(
                        ContextRevision.generation.desc(), ContextRevision.revision_id
                    )
                )
            ).all()
            for context_id, revision_id in history:
                current.setdefault(context_id, revision_id)
        return current

    async def snapshot(
        self,
        session: AsyncSession,
        roots: dict[str, str],
        *,
        workspace_id: str,
        max_nodes: int = 10000,
    ) -> dict:
        pending = set(roots.values())
        current = dict(
            (
                await session.execute(
                    select(
                        DesktopThread.task_id, DesktopThread.current_revision_id
                    ).where(DesktopThread.workspace_id == workspace_id)
                )
            ).all()
        )
        proven = (
            set(
                (
                    await session.scalars(
                        select(ContextPublicationReceipt.revision_id).where(
                            ContextPublicationReceipt.revision_id.in_(pending)
                        )
                    )
                ).all()
            )
            if pending
            else set()
        )
        for context_id, revision_id in roots.items():
            if context_id not in current or (
                current[context_id] != revision_id and revision_id not in proven
            ):
                raise ValueError("Lineage 根不是已发布 Context revision")
        nodes: dict[str, dict] = {}
        edges: dict[tuple[str, str], dict] = {}
        while pending:
            batch = tuple(sorted(pending)[:256])
            pending.difference_update(batch)
            rows = list(
                (
                    await session.execute(
                        select(ContextRevision, DesktopThread.workspace_id)
                        .join(
                            DesktopThread,
                            DesktopThread.task_id == ContextRevision.context_id,
                        )
                        .where(ContextRevision.revision_id.in_(batch))
                    )
                ).all()
            )
            if len(rows) != len(batch):
                raise ValueError("Lineage 祖先 revision 缺失")
            for row, workspace in rows:
                if workspace != workspace_id:
                    raise ValueError("Lineage 来源越过 workspace 授权边界")
                if row.revision_id in roots.values() and row.projection_status not in {
                    "valid",
                    "repaired",
                    "approved",
                    "deleted",
                }:
                    raise ValueError("Lineage 根尚未完成发布所需的 projection 审核")
                nodes[row.revision_id] = {
                    "revision_id": row.revision_id,
                    "context_id": row.context_id,
                    "generation": row.generation,
                    "redacted": row.deleted_at is not None,
                }
            if len(nodes) > max_nodes:
                raise ValueError("lineage_history_budget_exceeded: 祖先读取未完成")
            sources = tuple(
                (
                    await session.scalars(
                        select(ContextRevisionSource)
                        .where(ContextRevisionSource.target_revision_id.in_(batch))
                        .order_by(
                            ContextRevisionSource.target_revision_id,
                            ContextRevisionSource.position,
                        )
                    )
                ).all()
            )
            for source in sources:
                target = nodes[source.target_revision_id]
                if target["context_id"] != source.source_context_id:
                    edges[(source.source_revision_id, source.target_revision_id)] = {
                        "source_context_id": source.source_context_id,
                        "source_revision_id": source.source_revision_id,
                        "target_context_id": target["context_id"],
                        "target_revision_id": source.target_revision_id,
                    }
                if source.source_revision_id not in nodes:
                    pending.add(source.source_revision_id)
        ordered_edges = sorted(
            edges.values(),
            key=lambda item: (item["target_revision_id"], item["source_revision_id"]),
        )
        return {
            "schema_version": 1,
            "complete": True,
            "roots": roots,
            "nodes": sorted(nodes.values(), key=lambda item: item["revision_id"]),
            "edges": ordered_edges,
            "topology_hash": canonical_hash(ordered_edges),
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
