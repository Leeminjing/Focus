"""本文件对外提供提交权威与完整 Revision DAG 的派生边界验收测试。

输入为隔离数据库中的多父来源、Context 回流、候选和回滚事务；输出为可重建图及提交后才变化的断言。
具体工作流为创建 Architecture R4→Implementation R7→Architecture R5，加入独立父源，验证 shadow 与回滚不改变图。
另外验证实际成员退出、超过 256 个祖先的完整分页，以及只有结构读取权限时不得取得祖先正文。
示例：python -m pytest backend/tests/test_committed_derivation_memory.py -q。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_agent_loop_round_liveness import _seed_loop, _stop
from test_atomic_portfolio_publication import _contract
from test_round_memory_boundaries import _prepare_portfolio, _world
from test_round_task_progress import _Checkpointer

from backend.app.desktop.agent_loop.models import LoopContextMembership
from backend.app.desktop.agent_loop.observation_capture import (
    LoopObservationService,
    PatrolReadRequest,
)
from backend.app.desktop.context_evolution import ContextRevisionRepository
from backend.app.desktop.context_evolution.committed_lineage import (
    CommittedLineageReader,
)
from backend.app.desktop.context_evolution.lineage import ContextLineageResolver
from backend.app.desktop.context_evolution.lineage_contracts import LineageSnapshot
from backend.app.desktop.context_evolution.schemas import (
    ContextRevisionContract,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionRef,
    ContextRevisionSourceContract,
)
from backend.app.desktop.models import DesktopThread


@pytest.mark.usefixtures("isolated_postgres_database")
def test_full_multi_parent_history_context_return_and_commit_boundary(tmp_path):
    async def exercise():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(
            sessions, tmp_path, label="lineage", started_at=datetime.now(UTC)
        )
        repository = ContextRevisionRepository()
        reader = CommittedLineageReader()
        suffix = uuid.uuid4().hex[:8]
        arch, impl, e2e = (f"{kind}-{suffix}" for kind in ("arch", "impl", "e2e"))

        async def revision(
            session,
            context,
            generation,
            sources=(),
            *,
            status=ContextRevisionProjectionStatus.VALID,
        ):
            ref = ContextRevisionRef(
                context_id=context,
                revision_id=uuid.uuid4().hex,
                generation=generation,
                execution_thread_id=f"thread-{context}",
                checkpoint_id=uuid.uuid4().hex,
                payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
            )
            await repository.insert(
                session,
                ContextRevisionContract(
                    ref=ref,
                    sources=tuple(
                        ContextRevisionSourceContract(source=source, position=index)
                        for index, source in enumerate(sources)
                    ),
                    content_hash="a" * 64,
                    projection_status=status,
                    origin_kind=ContextRevisionOriginKind.MANUAL_DERIVE,
                    created_at=datetime.now(UTC),
                ),
            )
            return ref

        try:
            async with sessions.begin() as session:
                root = await session.get(DesktopThread, fixture["context_id"])
                primary = (
                    await repository.get_by_id(session, root.current_revision_id)
                ).ref
                for context in (arch, impl, e2e):
                    session.add(
                        DesktopThread(
                            task_id=context,
                            workspace_id=root.workspace_id,
                            thread_id=f"thread-{context}",
                            title=context,
                        )
                    )
                await session.flush()
                workspace_id = root.workspace_id
                a4 = await revision(session, arch, 4)
                await repository.switch_current(session, a4, None)
                b7 = await revision(session, impl, 7, (a4,))
                await repository.switch_current(session, b7, None)
                a5 = await revision(session, arch, 5, (b7, primary, a4))
                await repository.switch_current(session, a5, a4)
                shadow = await revision(session, e2e, 1, (a5,))
                implementation = await session.get(DesktopThread, impl)
                implementation.archived_at = datetime.now(UTC)
            async with sessions() as session:
                before = await reader.snapshot(
                    session, {arch: a5.revision_id}, workspace_id=workspace_id
                )
                LineageSnapshot.model_validate(before)
                assert len(before["edges"]) == 3
                assert {
                    a4.revision_id,
                    b7.revision_id,
                    a5.revision_id,
                    primary.revision_id,
                } == {node["revision_id"] for node in before["nodes"]}
                with pytest.raises(ValueError, match="不是已发布"):
                    await reader.snapshot(
                        session, {e2e: shadow.revision_id}, workspace_id=workspace_id
                    )
                scoped = reader.scoped(before, {arch})
                LineageSnapshot.model_validate(scoped)
                assert len(scoped["edges"]) == 3
                assert (
                    await ContextLineageResolver().resolve(
                        session, {arch: a5.revision_id}
                    )
                    == ()
                )
                with pytest.raises(ValueError, match="未完成"):
                    await reader.snapshot(
                        session,
                        {arch: a5.revision_id},
                        workspace_id=workspace_id,
                        max_nodes=1,
                    )
            with pytest.raises(RuntimeError, match="rollback"):
                async with sessions.begin() as session:
                    await repository.switch_current(session, shadow, None)
                    raise RuntimeError("rollback publication")
            async with sessions() as session:
                assert (
                    await reader.snapshot(
                        session, {arch: a5.revision_id}, workspace_id=workspace_id
                    )
                    == before
                )
                with pytest.raises(ValueError, match="不是已发布"):
                    await reader.snapshot(
                        session, {e2e: shadow.revision_id}, workspace_id=workspace_id
                    )
            async with sessions.begin() as session:
                await repository.switch_current(session, shadow, None)
            async with sessions() as session:
                after = await reader.snapshot(
                    session, {e2e: shadow.revision_id}, workspace_id=workspace_id
                )
                assert len(after["edges"]) == 4
                assert after["topology_hash"] != before["topology_hash"]
                assert (
                    await reader.snapshot(
                        session, {arch: a4.revision_id}, workspace_id=workspace_id
                    )
                )["edges"] == []
            async with sessions.begin() as session:
                pending = await revision(
                    session,
                    arch,
                    6,
                    (a5,),
                    status=ContextRevisionProjectionStatus.APPROVAL_REQUIRED,
                )
                await repository.switch_current(session, pending, a5)
            async with sessions() as session:
                published = await reader.published_roots(session, (arch,))
                assert published == {arch: a5.revision_id}
                assert (
                    await reader.snapshot(session, published, workspace_id=workspace_id)
                    == before
                )
                with pytest.raises(ValueError, match="projection 审核"):
                    await reader.snapshot(
                        session, {arch: pending.revision_id}, workspace_id=workspace_id
                    )
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_lineage_pages_all_300_ancestors_and_rejects_truncation(tmp_path):
    async def exercise():
        async with _world(tmp_path) as (sessions, fixture):
            repository = ContextRevisionRepository()
            reader = CommittedLineageReader()
            async with sessions.begin() as session:
                source_context = await session.get(DesktopThread, fixture["context_id"])
                parents = tuple(
                    _contract(source_context.task_id, number, "private ancestor")
                    for number in range(2, 302)
                )
                child_id = uuid.uuid4().hex
                session.add(
                    DesktopThread(
                        task_id=child_id,
                        workspace_id=source_context.workspace_id,
                        thread_id=f"thread-{child_id}",
                        title="E2E",
                    )
                )
                await session.flush()
                child = _contract(child_id, 1, "E2E").model_copy(
                    update={
                        "sources": tuple(
                            ContextRevisionSourceContract(
                                source=parent.ref, position=index
                            )
                            for index, parent in enumerate(parents)
                        )
                    }
                )
                await repository.insert_many(session, (*parents, child))
                await repository.switch_current(session, child.ref, None)
                workspace_id = source_context.workspace_id
            async with sessions() as session:
                snapshot = await reader.snapshot(
                    session,
                    {child_id: child.ref.revision_id},
                    workspace_id=workspace_id,
                )
                assert len(snapshot["nodes"]) == 301 and len(snapshot["edges"]) == 300
                assert {parent.ref.revision_id for parent in parents} <= {
                    node["revision_id"] for node in snapshot["nodes"]
                }
                assert reader.scoped(snapshot, {child_id}) == snapshot
                LineageSnapshot.model_validate(snapshot)
                with pytest.raises(ValueError, match="lineage_history_budget_exceeded"):
                    await reader.snapshot(
                        session,
                        {child_id: child.ref.revision_id},
                        workspace_id=workspace_id,
                        max_nodes=256,
                    )
                assert "private ancestor" not in json.dumps(snapshot)

    asyncio.run(exercise())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_departed_membership_keeps_ancestry_without_granting_content_access(tmp_path):
    async def exercise():
        async with _world(tmp_path) as (sessions, fixture):
            frozen, publisher = await _prepare_portfolio(
                sessions, fixture, action="create"
            )
            publication = await publisher.publish(
                frozen.portfolio_revision_id, frozen.controls
            )
            child = publication.context_revisions[0]
            async with sessions.begin() as session:
                membership = await session.scalar(
                    select(LoopContextMembership).where(
                        LoopContextMembership.loop_id == fixture["loop_id"],
                        LoopContextMembership.context_id == fixture["context_id"],
                    )
                )
                membership.status = "discarded"
                session.add(
                    LoopContextMembership(
                        membership_id=uuid.uuid4().hex,
                        loop_id=fixture["loop_id"],
                        context_id=child.context_id,
                        role="derived",
                    )
                )
            capture = LoopObservationService(sessions, _Checkpointer())
            observation = await capture.capture(fixture["loop_id"], fixture["round_id"])
            assert {
                entry["context_id"] for entry in observation.portfolio_frontier
            } == {child.context_id}
            scoped = CommittedLineageReader.scoped(
                observation.committed_lineage, {child.context_id}
            )
            assert {node["context_id"] for node in scoped["nodes"]} == {
                fixture["context_id"],
                child.context_id,
            }
            assert len(scoped["edges"]) == 1
            assert all(
                set(node) == {"context_id", "revision_id", "generation", "redacted"}
                for node in scoped["nodes"]
            )
            with pytest.raises(ValueError, match="handles"):
                await capture.selective_read(
                    observation,
                    (
                        PatrolReadRequest(
                            context_id=fixture["context_id"],
                            revision_id=fixture["revision_id"],
                        ),
                    ),
                )
            async with sessions() as session:
                root = await session.get(DesktopThread, fixture["context_id"])
                with pytest.raises(ValueError, match="不是已发布"):
                    await CommittedLineageReader().snapshot(
                        session,
                        {child.context_id: child.revision_id},
                        workspace_id=root.workspace_id + "-unauthorized",
                    )

    asyncio.run(exercise())
