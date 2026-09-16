r"""本文件验证 Context revision repository 的不可变插入、有序来源、CAS 与墓碑保留语义。

输入为隔离 PostgreSQL 中的 workspace、Context identity 与冻结 revision 合同；输出为精确读取、
并发冲突和无历史删除断言。具体工作流为创建两条来源 revision，再插入多来源目标、切换 current
pointer，并尝试重复写与陈旧 CAS。示例：`pytest backend/tests/test_context_revision_repository.py`。
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
    ContextRevisionAlreadyExists,
    ContextRevisionContract,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionRef,
    ContextRevisionRepository,
    ContextRevisionSourceContract,
    StaleContextRevision,
)
from backend.app.desktop.context_evolution.models import ContextRevision, ContextRevisionSource
from backend.app.desktop.models import DesktopThread, DesktopWorkspace


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def _ref(context_id: str, revision_id: str, generation: int) -> ContextRevisionRef:
    return ContextRevisionRef(
        context_id=context_id,
        revision_id=revision_id,
        generation=generation,
        execution_thread_id=f"execution-{revision_id}",
        checkpoint_ns="context-revision",
        checkpoint_id=f"checkpoint-{revision_id}",
        payload_mode=(
            ContextRevisionPayloadMode.CHECKPOINT
            if generation == 1
            else ContextRevisionPayloadMode.DEFINITION
        ),
    )


def _contract(
    ref: ContextRevisionRef,
    *sources: ContextRevisionRef,
    status: ContextRevisionProjectionStatus = ContextRevisionProjectionStatus.VALID,
) -> ContextRevisionContract:
    return ContextRevisionContract(
        ref=ref,
        sources=tuple(
            ContextRevisionSourceContract(source=source, position=position)
            for position, source in enumerate(sources)
        ),
        authored_messages=({"id": f"message-{ref.revision_id}", "role": "human", "content": "继续"},),
        execution_messages=({"id": f"message-{ref.revision_id}", "role": "human", "content": "继续"},),
        definition_hash="d" * 64 if ref.payload_mode is ContextRevisionPayloadMode.DEFINITION else None,
        projection_hash="p" * 64 if ref.payload_mode is ContextRevisionPayloadMode.DEFINITION else None,
        content_hash=(ref.revision_id[-1] * 64),
        projection_status=status,
        origin_kind=ContextRevisionOriginKind.CURATION,
        origin_id=f"origin-{ref.revision_id}",
        created_at=datetime(2026, 9, 14, tzinfo=UTC),
        deleted_at=datetime(2026, 9, 14, tzinfo=UTC)
        if status is ContextRevisionProjectionStatus.DELETED
        else None,
    )


def test_repository_preserves_immutable_ordered_history_and_rejects_stale_cas() -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        suffix = uuid.uuid4().hex[:8]
        workspace_id = f"ws-repo-{suffix}"
        source_a_id = f"ctx-a-{suffix}"
        source_b_id = f"ctx-b-{suffix}"
        target_id = f"ctx-t-{suffix}"
        source_a = _ref(source_a_id, f"rev-a-{suffix}", 1)
        source_b = _ref(source_b_id, f"rev-b-{suffix}", 1)
        target_v1 = _ref(target_id, f"rev-t1-{suffix}", 1)
        target_v2 = _ref(target_id, f"rev-t2-{suffix}", 2)
        tombstone = _ref(target_id, f"rev-t3-{suffix}", 3)
        repository = ContextRevisionRepository()
        try:
            async with sessions.begin() as session:
                session.add(
                    DesktopWorkspace(
                        workspace_id=workspace_id,
                        path=f"/tmp/{workspace_id}",
                        display_name="revision repository",
                    )
                )
                await session.flush()
                session.add_all(
                    [
                        DesktopThread(
                            task_id=context_id,
                            workspace_id=workspace_id,
                            thread_id=f"thread-{context_id}",
                            title=context_id,
                        )
                        for context_id in (source_a_id, source_b_id, target_id)
                    ]
                )

            async with sessions.begin() as session:
                await repository.insert(session, _contract(source_a))
                await repository.insert(session, _contract(source_b))
                await repository.insert(session, _contract(target_v1, source_b, source_a))
                await repository.switch_current(session, target_v1, None)

            async with sessions() as session:
                loaded = await repository.get(session, target_v1)
                current = await repository.current(session, target_id)
                assert loaded.sources == (
                    ContextRevisionSourceContract(source=source_b, position=0),
                    ContextRevisionSourceContract(source=source_a, position=1),
                )
                assert current == loaded

            async with sessions.begin() as session:
                with pytest.raises(ContextRevisionAlreadyExists):
                    await repository.insert(
                        session,
                        _contract(target_v1, source_a, source_b).model_copy(
                            update={"content_hash": "f" * 64}
                        ),
                    )

            async with sessions.begin() as session:
                await repository.insert(session, _contract(target_v2, target_v1))
                await repository.switch_current(session, target_v2, target_v1)

            async with sessions.begin() as session:
                with pytest.raises(StaleContextRevision):
                    await repository.switch_current(session, target_v1, None)

            async with sessions.begin() as session:
                await repository.insert(
                    session,
                    _contract(
                        tombstone,
                        target_v2,
                        status=ContextRevisionProjectionStatus.DELETED,
                    ),
                )
                await repository.switch_current(session, tombstone, target_v2)

            async with sessions() as session:
                assert (await repository.get(session, target_v1)).ref == target_v1
                assert (await repository.get(session, target_v2)).ref == target_v2
                assert (await repository.current(session, target_id)).ref == tombstone
        finally:
            async with sessions.begin() as session:
                await session.execute(
                    delete(ContextRevisionSource).where(
                        ContextRevisionSource.target_revision_id.in_(
                            [target_v1.revision_id, target_v2.revision_id, tombstone.revision_id]
                        )
                    )
                )
                await session.execute(
                    delete(DesktopThread).where(
                        DesktopThread.task_id.in_([source_a_id, source_b_id, target_id])
                    )
                )
                await session.execute(
                    delete(ContextRevision).where(
                        ContextRevision.revision_id.in_(
                            [
                                source_a.revision_id,
                                source_b.revision_id,
                                target_v1.revision_id,
                                target_v2.revision_id,
                                tombstone.revision_id,
                            ]
                        )
                    )
                )
                await session.execute(
                    delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace_id)
                )
            await engine.dispose()

    asyncio.run(run())
