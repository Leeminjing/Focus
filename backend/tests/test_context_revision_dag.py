r"""本文件验证 Context revision repository 的多来源归属与批量 DAG 原子性。

输入为跨代双向 Context 演化、同批循环、错误 checkpoint 和跨 workspace 来源；输出为合法反馈
链成功及所有非法 batch 零部分写入断言。具体工作流为在隔离 PostgreSQL 建立多个 Context identity，
按精确 revision ref 插入或拒绝来源图。示例：`pytest backend/tests/test_context_revision_dag.py`。
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
    ContextRevisionContract,
    ContextRevisionCycle,
    ContextRevisionIdentityMismatch,
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
        execution_thread_id=f"shadow:{context_id}:{label}",
        checkpoint_ns="context-revision-shadow",
        checkpoint_id=f"checkpoint-{context_id}-{label}",
        payload_mode=ContextRevisionPayloadMode.DEFINITION,
    )


def _contract(ref: ContextRevisionRef, *sources: ContextRevisionRef) -> ContextRevisionContract:
    return ContextRevisionContract(
        ref=ref,
        sources=tuple(
            ContextRevisionSourceContract(source=source, position=position)
            for position, source in enumerate(sources)
        ),
        authored_messages=({"id": f"m-{ref.revision_id}", "role": "human", "content": ref.revision_id},),
        execution_messages=({"id": f"m-{ref.revision_id}", "role": "human", "content": ref.revision_id},),
        definition_hash="d" * 64,
        projection_hash="p" * 64,
        content_hash=uuid.uuid5(uuid.NAMESPACE_OID, ref.revision_id).hex * 2,
        projection_status=ContextRevisionProjectionStatus.VALID,
        origin_kind=ContextRevisionOriginKind.CURATION,
        origin_id="dag-test",
        created_at=datetime(2026, 9, 14, tzinfo=UTC),
    )


def test_revision_dag_accepts_cross_generation_feedback_and_rejects_invalid_batches_atomically() -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        suffix = uuid.uuid4().hex[:8]
        ws_main = f"ws-dag-{suffix}"
        ws_other = f"ws-other-{suffix}"
        architecture = f"ctx-arch-{suffix}"
        implementation = f"ctx-impl-{suffix}"
        review = f"ctx-review-{suffix}"
        foreign = f"ctx-foreign-{suffix}"
        repository = ContextRevisionRepository()
        arch_r4 = _ref(architecture, "arch-r4", 4)
        impl_r7 = _ref(implementation, "impl-r7", 7)
        arch_r5 = _ref(architecture, "arch-r5", 5)
        try:
            async with sessions.begin() as session:
                session.add_all(
                    [
                        DesktopWorkspace(
                            workspace_id=ws_main,
                            path=f"/tmp/{ws_main}",
                            display_name="dag main",
                        ),
                        DesktopWorkspace(
                            workspace_id=ws_other,
                            path=f"/tmp/{ws_other}",
                            display_name="dag other",
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        DesktopThread(
                            task_id=context_id,
                            workspace_id=ws_main,
                            thread_id=f"thread-{context_id}",
                            title=context_id,
                        )
                        for context_id in (architecture, implementation, review)
                    ]
                    + [
                        DesktopThread(
                            task_id=foreign,
                            workspace_id=ws_other,
                            thread_id=f"thread-{foreign}",
                            title=foreign,
                        )
                    ]
                )

            async with sessions.begin() as session:
                await repository.insert(session, _contract(arch_r4))
                await repository.insert(session, _contract(impl_r7, arch_r4))
                await repository.insert(session, _contract(arch_r5, impl_r7))
                foreign_r1 = _ref(foreign, "foreign-r1", 1)
                await repository.insert(session, _contract(foreign_r1))

            async with sessions() as session:
                assert (await repository.get(session, impl_r7)).sources[0].source == arch_r4
                assert (await repository.get(session, arch_r5)).sources[0].source == impl_r7

            arch_r6 = _ref(architecture, "arch-r6", 6)
            impl_r8 = _ref(implementation, "impl-r8", 8)
            cycle = (
                _contract(arch_r6, impl_r8),
                _contract(impl_r8, arch_r6),
            )
            async with sessions.begin() as session:
                before = await session.scalar(select(func.count()).select_from(ContextRevision))
                with pytest.raises(ContextRevisionCycle):
                    await repository.insert_many(session, cycle)
                after = await session.scalar(select(func.count()).select_from(ContextRevision))
                assert after == before

            wrong_source = arch_r5.model_copy(update={"checkpoint_id": "checkpoint-from-another-revision"})
            review_r1 = _ref(review, "review-r1", 1)
            async with sessions.begin() as session:
                before = await session.scalar(select(func.count()).select_from(ContextRevision))
                with pytest.raises(ContextRevisionIdentityMismatch, match="执行身份不匹配"):
                    await repository.insert(session, _contract(review_r1, wrong_source))
                assert await session.scalar(select(func.count()).select_from(ContextRevision)) == before

            review_r2 = _ref(review, "review-r2", 2)
            async with sessions.begin() as session:
                before = await session.scalar(select(func.count()).select_from(ContextRevision))
                with pytest.raises(ContextRevisionIdentityMismatch, match="同一 workspace"):
                    await repository.insert(session, _contract(review_r2, foreign_r1))
                assert await session.scalar(select(func.count()).select_from(ContextRevision)) == before
        finally:
            async with sessions.begin() as session:
                await session.execute(
                    delete(ContextRevisionSource).where(
                        ContextRevisionSource.source_context_id.in_(
                            [architecture, implementation, review, foreign]
                        )
                    )
                )
                await session.execute(
                    delete(ContextRevision).where(
                        ContextRevision.context_id.in_([architecture, implementation, review, foreign])
                    )
                )
                await session.execute(
                    delete(DesktopThread).where(
                        DesktopThread.task_id.in_([architecture, implementation, review, foreign])
                    )
                )
                await session.execute(
                    delete(DesktopWorkspace).where(
                        DesktopWorkspace.workspace_id.in_([ws_main, ws_other])
                    )
                )
            await engine.dispose()

    asyncio.run(run())
