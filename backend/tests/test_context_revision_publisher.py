r"""本文件验证单 Context publisher 的 shadow 准备、协议阻断、原子发布与陈旧候选回滚。

输入为隔离 PostgreSQL Context、authored messages 和可观测 checkpoint writer；输出为不可路由
identity、current pointer、无失败残留及审批阻断断言。具体工作流为发布 base revision，准备多个
候选并制造 writer failure、approval_required 和 CAS race。示例：`pytest test_context_revision_publisher.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
import uuid

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.context_evolution import (
    ContextRevisionContract,
    ContextRevisionNotFound,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionPreparationFailed,
    ContextRevisionPrepareRequest,
    ContextRevisionProjectionStatus,
    ContextRevisionPublicationBlocked,
    ContextRevisionPublisher,
    ContextRevisionRef,
    ContextRevisionRepository,
    ContextRevisionSourceContract,
    StaleContextRevision,
)
from backend.app.desktop.context_evolution.models import ContextRevision, ContextRevisionSource
from backend.app.desktop.models import DesktopThread, DesktopWorkspace


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


class _CheckpointWriter:
    def __init__(self) -> None:
        self.calls: list[ContextRevisionRef] = []
        self.fail = False

    async def write(
        self,
        shadow_ref: ContextRevisionRef,
        execution_messages: tuple[dict, ...],
    ) -> tuple[str, tuple[str, ...]]:
        self.calls.append(shadow_ref)
        if self.fail:
            raise RuntimeError("provider unavailable")
        return (
            f"checkpoint-{shadow_ref.revision_id}",
            tuple(message["id"] for message in execution_messages if message.get("id")),
        )


def _base(ref: ContextRevisionRef) -> ContextRevisionContract:
    return ContextRevisionContract(
        ref=ref,
        content_hash="b" * 64,
        projection_status=ContextRevisionProjectionStatus.VALID,
        origin_kind=ContextRevisionOriginKind.ROOT,
        origin_id=ref.context_id,
        created_at=datetime(2026, 9, 14, tzinfo=UTC),
    )


def test_publisher_only_routes_fully_prepared_candidates_and_rolls_back_stale_publish() -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        suffix = uuid.uuid4().hex[:8]
        workspace_id = f"ws-pub-{suffix}"
        context_id = f"ctx-pub-{suffix}"
        base_ref = ContextRevisionRef(
            context_id=context_id,
            revision_id=f"rev-base-{suffix}",
            generation=1,
            execution_thread_id=f"thread-{suffix}",
            checkpoint_ns="",
            checkpoint_id=f"checkpoint-base-{suffix}",
            payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
        )
        repository = ContextRevisionRepository()
        writer = _CheckpointWriter()
        publisher = ContextRevisionPublisher(repository, writer)
        try:
            async with sessions.begin() as session:
                session.add(
                    DesktopWorkspace(
                        workspace_id=workspace_id,
                        path=f"/tmp/{workspace_id}",
                        display_name="publisher test",
                    )
                )
                await session.flush()
                session.add(
                    DesktopThread(
                        task_id=context_id,
                        workspace_id=workspace_id,
                        thread_id=f"legacy-{suffix}",
                        title="publisher",
                    )
                )
            async with sessions.begin() as session:
                await repository.insert(session, _base(base_ref))
                await repository.switch_current(session, base_ref, None)

            request = ContextRevisionPrepareRequest(
                context_id=context_id,
                expected_base=base_ref,
                sources=(ContextRevisionSourceContract(source=base_ref, position=0),),
                authored_messages=({"id": "directive-1", "role": "human", "content": "运行测试"},),
                origin_kind=ContextRevisionOriginKind.MANUAL_DERIVE,
                origin_id="derive-request-1",
            )
            async with sessions() as session:
                first = await publisher.prepare(session, request)
                second = await publisher.prepare(session, request)

            assert first.publishable is True
            assert first.revision.ref.execution_thread_id.startswith(f"shadow:{context_id}:")
            assert first.revision.ref.checkpoint_ns == "context-revision-shadow"
            assert writer.calls[0].checkpoint_id is None

            async with sessions.begin() as session:
                result = await publisher.publish(session, second)
            assert result.previous == base_ref
            assert result.published == second.revision.ref

            async with sessions.begin() as session:
                with pytest.raises(StaleContextRevision):
                    await publisher.publish(session, first)

            async with sessions() as session:
                assert (await repository.current(session, context_id)).ref == second.revision.ref
                with pytest.raises(ContextRevisionNotFound):
                    await repository.get(session, first.revision.ref)

            invalid = ContextRevisionPrepareRequest(
                context_id=context_id,
                expected_base=second.revision.ref,
                authored_messages=({"id": "bad", "role": "alien", "content": "x"},),
                origin_kind=ContextRevisionOriginKind.DEFINITION_UPDATE,
            )
            calls_before_invalid = len(writer.calls)
            async with sessions() as session:
                blocked = await publisher.prepare(session, invalid)
            assert blocked.publishable is False
            assert blocked.revision.ref.checkpoint_id is None
            assert blocked.revision.projection_status is ContextRevisionProjectionStatus.APPROVAL_REQUIRED
            assert len(writer.calls) == calls_before_invalid
            async with sessions.begin() as session:
                with pytest.raises(ContextRevisionPublicationBlocked):
                    await publisher.publish(session, blocked)

            writer.fail = True
            count_before_failure = None
            async with sessions() as session:
                count_before_failure = await session.scalar(select(func.count()).select_from(ContextRevision))
                with pytest.raises(ContextRevisionPreparationFailed):
                    await publisher.prepare(
                        session,
                        ContextRevisionPrepareRequest(
                            context_id=context_id,
                            expected_base=second.revision.ref,
                            authored_messages=(
                                {"id": "directive-2", "role": "human", "content": "定位失败"},
                            ),
                            origin_kind=ContextRevisionOriginKind.DEFINITION_UPDATE,
                        ),
                    )
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(ContextRevision)) == count_before_failure
                assert (await repository.current(session, context_id)).ref == second.revision.ref
        finally:
            async with sessions.begin() as session:
                await session.execute(
                    delete(ContextRevisionSource).where(
                        ContextRevisionSource.source_context_id == context_id
                    )
                )
                await session.execute(
                    update(DesktopThread)
                    .where(DesktopThread.task_id == context_id)
                    .values(current_revision_id=None)
                )
                await session.execute(
                    delete(ContextRevision).where(ContextRevision.context_id == context_id)
                )
                await session.execute(
                    delete(DesktopThread).where(DesktopThread.task_id == context_id)
                )
                await session.execute(
                    delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace_id)
                )
            await engine.dispose()

    asyncio.run(run())
