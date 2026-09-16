r"""本文件验证完整 Context Evolution Graph 与第一父链兼容树的多来源及反馈环语义。

输入为 root、派生、更新、多来源和跨代反馈 revision fixture；输出为无损图边、稳定 primary/
secondary source、深度与 identity 环抑制断言。具体工作流为经 repository 插入不可变 revision 并
切换 current pointer，再分别查询完整图和兼容树。示例：`pytest test_context_evolution_graph.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.context_evolution import (
    ContextEvolutionQueryService,
    ContextRevisionContract,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionRef,
    ContextRevisionRepository,
    ContextRevisionSourceContract,
)
from backend.app.desktop.context_evolution.models import ContextRevision, ContextRevisionSource
from backend.app.desktop.models import DesktopThread, DesktopWorkspace


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def _ref(context_id: str, label: str, generation: int) -> ContextRevisionRef:
    return ContextRevisionRef(
        context_id=context_id,
        revision_id=f"{label}-{uuid.uuid5(uuid.NAMESPACE_DNS, context_id + label).hex[:16]}",
        generation=generation,
        execution_thread_id=f"thread-{context_id}",
        checkpoint_ns="",
        checkpoint_id=f"checkpoint-{context_id}-{label}",
        payload_mode=ContextRevisionPayloadMode.DEFINITION,
    )


def _contract(ref: ContextRevisionRef, *sources: ContextRevisionRef) -> ContextRevisionContract:
    return ContextRevisionContract(
        ref=ref,
        sources=tuple(
            ContextRevisionSourceContract(source=source, position=index)
            for index, source in enumerate(sources)
        ),
        authored_messages=({"id": ref.revision_id, "role": "human", "content": ref.revision_id},),
        execution_messages=({"id": ref.revision_id, "role": "human", "content": ref.revision_id},),
        definition_hash="d" * 64,
        projection_hash="p" * 64,
        content_hash=uuid.uuid5(uuid.NAMESPACE_OID, ref.revision_id).hex * 2,
        projection_status=ContextRevisionProjectionStatus.VALID,
        origin_kind=ContextRevisionOriginKind.CURATION,
        origin_id="graph-test",
        created_at=datetime(2026, 9, 14, tzinfo=UTC),
    )


def test_graph_keeps_all_sources_while_tree_projects_deterministically() -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        suffix = uuid.uuid4().hex[:8]
        workspace_id = f"ws-graph-{suffix}"
        context_a = f"ctx-a-{suffix}"
        context_b = f"ctx-b-{suffix}"
        context_c = f"ctx-c-{suffix}"
        repository = ContextRevisionRepository()
        queries = ContextEvolutionQueryService(repository)
        a1 = _ref(context_a, "a1", 1)
        b1 = _ref(context_b, "b1", 1)
        c1 = _ref(context_c, "c1", 1)
        c2 = _ref(context_c, "c2", 2)
        a2 = _ref(context_a, "a2", 2)
        try:
            async with sessions.begin() as session:
                session.add(
                    DesktopWorkspace(
                        workspace_id=workspace_id,
                        path=f"/tmp/{workspace_id}",
                        display_name="evolution graph",
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
                            (context_a, "Architecture"),
                            (context_b, "Implementation"),
                            (context_c, "Release"),
                        )
                    ]
                )
            async with sessions.begin() as session:
                await repository.insert(session, _contract(a1))
                await repository.insert(session, _contract(b1, a1))
                await repository.insert(session, _contract(c1, a1, b1))
                await repository.insert(session, _contract(c2, c1))
                await repository.insert(session, _contract(a2, b1))
                await repository.switch_current(session, a1, None)
                await repository.switch_current(session, b1, None)
                await repository.switch_current(session, c1, None)
                await repository.switch_current(session, c2, c1)
                await repository.switch_current(session, a2, a1)

            async with sessions() as session:
                graph = await queries.graph(session, workspace_id)
                tree = await queries.first_parent_tree(session, workspace_id)

            assert len(graph.nodes) == 5
            assert len(graph.edges) == 5
            c1_edges = [edge for edge in graph.edges if edge.target == c1]
            assert [(edge.source, edge.position) for edge in c1_edges] == [(a1, 0), (b1, 1)]
            by_context = {node.context_id: node for node in tree.nodes}
            assert by_context[context_c].current_revision == c2
            assert by_context[context_c].primary_source == a1
            assert by_context[context_c].secondary_sources == (b1,)
            assert by_context[context_c].depth >= 1
            assert sum(node.cycle_suppressed for node in tree.nodes) == 1
            suppressed = next(node for node in tree.nodes if node.cycle_suppressed)
            assert suppressed.primary_source is not None
            assert suppressed.tree_parent_context_id is None
        finally:
            async with sessions.begin() as session:
                await session.execute(
                    delete(ContextRevisionSource).where(
                        ContextRevisionSource.source_context_id.in_([context_a, context_b, context_c])
                    )
                )
                await session.execute(
                    delete(ContextRevision).where(
                        ContextRevision.context_id.in_([context_a, context_b, context_c])
                    )
                )
                await session.execute(
                    delete(DesktopThread).where(
                        DesktopThread.task_id.in_([context_a, context_b, context_c])
                    )
                )
                await session.execute(
                    delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace_id)
                )
            await engine.dispose()

    asyncio.run(run())
