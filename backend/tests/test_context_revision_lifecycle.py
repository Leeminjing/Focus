r"""本文件验证 Context revision 血缘在归档、非级联删除与物理清理后的可解释性。

输入为同一 workspace 内的来源、后代与无引用 Context revision；输出为生命周期、最小墓碑、
精确来源 checkpoint、完整图边和物理删除断言。具体工作流为先归档并恢复后代，再删除仍被其
引用的来源和无引用叶子，最后经 reader 与 Evolution Graph 验证血缘。示例：
`pytest backend/tests/test_context_revision_lifecycle.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.context_evolution import (
    ContextEvolutionQueryService,
    ContextRevisionContract,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionReader,
    ContextRevisionRef,
    ContextRevisionRepository,
    ContextRevisionSourceContract,
)
from backend.app.desktop.context_evolution.models import ContextRevision, ContextRevisionSource
from backend.app.desktop.context_service import ContextService
from backend.app.desktop.models import DesktopThread, DesktopWorkspace


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


class _Checkpointer:
    def __init__(self) -> None:
        self.deleted_threads: list[str] = []

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted_threads.append(thread_id)

    async def aget_tuple(self, config: dict) -> None:
        return None


def _ref(context_id: str, generation: int = 1) -> ContextRevisionRef:
    token = uuid.uuid4().hex
    return ContextRevisionRef(
        context_id=context_id,
        revision_id=token,
        generation=generation,
        execution_thread_id=f"thread-{context_id}",
        checkpoint_ns="",
        checkpoint_id=f"checkpoint-{token}",
        payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
    )


def _contract(
    ref: ContextRevisionRef,
    *sources: ContextRevisionRef,
) -> ContextRevisionContract:
    return ContextRevisionContract(
        ref=ref,
        sources=tuple(
            ContextRevisionSourceContract(source=source, position=position)
            for position, source in enumerate(sources)
        ),
        content_hash=uuid.uuid5(uuid.NAMESPACE_OID, ref.revision_id).hex * 2,
        projection_status=ContextRevisionProjectionStatus.VALID,
        origin_kind=ContextRevisionOriginKind.CURATION,
        origin_id="lifecycle-test",
        created_at=datetime(2026, 9, 14, tzinfo=UTC),
    )


def test_lifecycle_retains_referenced_history_and_physically_cleans_unreferenced_context() -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        repository = ContextRevisionRepository()
        reader = ContextRevisionReader(repository, _Checkpointer())
        queries = ContextEvolutionQueryService(repository)
        checkpointer = _Checkpointer()
        contexts = ContextService(sessions, checkpointer, object())
        suffix = uuid.uuid4().hex[:8]
        workspace_id = f"ws-life-{suffix}"
        source_id = f"ctx-source-{suffix}"
        descendant_id = f"ctx-child-{suffix}"
        leaf_id = f"ctx-leaf-{suffix}"
        source = _ref(source_id)
        descendant = _ref(descendant_id)
        leaf = _ref(leaf_id)
        try:
            async with sessions.begin() as session:
                session.add(
                    DesktopWorkspace(
                        workspace_id=workspace_id,
                        path=f"/tmp/{workspace_id}",
                        display_name="revision lifecycle",
                    )
                )
                await session.flush()
                session.add_all(
                    [
                        DesktopThread(
                            task_id=context_id,
                            workspace_id=workspace_id,
                            thread_id=f"thread-{context_id}",
                            title=title,
                        )
                        for context_id, title in (
                            (source_id, "Source"),
                            (descendant_id, "Descendant"),
                            (leaf_id, "Leaf"),
                        )
                    ]
                )
            async with sessions.begin() as session:
                await repository.insert(session, _contract(source))
                await repository.insert(session, _contract(descendant, source))
                await repository.insert(session, _contract(leaf))
                await repository.switch_current(session, source, None)
                await repository.switch_current(session, descendant, None)
                await repository.switch_current(session, leaf, None)

            await contexts.archive(descendant_id)
            async with sessions() as session:
                archived_tree = await queries.first_parent_tree(session, workspace_id)
                archived_node = next(
                    node for node in archived_tree.nodes if node.context_id == descendant_id
                )
                assert archived_node.lifecycle == "archived"
                assert archived_node.current_revision == descendant
                assert await session.scalar(
                    select(func.count(ContextRevision.revision_id)).where(
                        ContextRevision.context_id == descendant_id
                    )
                ) == 1

            await contexts.unarchive(descendant_id)
            await contexts.delete(source_id)
            await contexts.delete(leaf_id)

            async with sessions() as session:
                source_identity = await session.get(DesktopThread, source_id)
                assert source_identity is not None and source_identity.deleted_at is not None
                assert await session.get(DesktopThread, leaf_id) is None
                source_current = await repository.current(session, source_id)
                assert source_current is not None
                assert source_current.projection_status is ContextRevisionProjectionStatus.DELETED
                assert source_current.ref.checkpoint_id is None
                assert (await repository.get(session, source)).ref.checkpoint_id == source.checkpoint_id

                deleted_sources = await reader.read(session, descendant, "deleted-source")
                assert deleted_sources.sources[0].source == source
                assert deleted_sources.sources[0].deleted is True
                assert deleted_sources.sources[0].deleted_at is not None

                graph = await queries.graph(session, workspace_id)
                assert any(edge.target == descendant and edge.source == source for edge in graph.edges)
                assert any(node.ref == source and not node.deleted for node in graph.nodes)
                assert any(
                    node.ref == source_current.ref and node.deleted and node.current
                    for node in graph.nodes
                )
                tree = await queries.first_parent_tree(session, workspace_id)
                by_context = {node.context_id: node for node in tree.nodes}
                assert by_context[source_id].lifecycle == "deleted"
                assert by_context[descendant_id].lifecycle == "active"
                assert by_context[descendant_id].primary_source == source

            assert set(checkpointer.deleted_threads) == {
                f"thread-{source_id}",
                f"thread-{leaf_id}",
            }
        finally:
            async with sessions.begin() as session:
                await session.execute(
                    delete(ContextRevisionSource).where(
                        ContextRevisionSource.source_context_id.in_([source_id, descendant_id])
                    )
                )
                await session.execute(
                    delete(ContextRevision).where(
                        ContextRevision.context_id.in_([source_id, descendant_id, leaf_id])
                    )
                )
                await session.execute(
                    delete(DesktopThread).where(
                        DesktopThread.task_id.in_([source_id, descendant_id, leaf_id])
                    )
                )
                await session.execute(
                    delete(DesktopWorkspace).where(
                        DesktopWorkspace.workspace_id == workspace_id
                    )
                )
            await engine.dispose()

    asyncio.run(run())
