r"""本文件验证 Context Portfolio 的冻结、shadow 准备、CAS 原子发布、恢复与 outbox 去重。

输入为两个更新 Lane、一个 keep Lane、精确 source frontier 和控制 revision；输出为可复现冻结记录、
失败时零指针切换、成功时整代指针切换、陈旧发布拒绝和单次消费断言。具体工作流为在隔离 PostgreSQL
中建立 revision/Program，使用内存 checkpoint writer 准备 shadow，再验证事务边界和历史事实。
示例：`pytest backend/tests/test_atomic_portfolio_publication.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.context_curation import (
    AtomicPortfolioPublisher,
    CompiledLaneCandidate,
    CurationLane,
    CurationOutboxDelivery,
    CurationOutboxEvent,
    CurationOutboxRepository,
    CurationProgram,
    CurationProgramRepository,
    FrozenPortfolio,
    PortfolioCandidatePreparer,
    PortfolioControlRevisions,
    PortfolioCurrentReader,
    PortfolioFreezeRequest,
    PortfolioFreezer,
    PortfolioLaneAction,
    PortfolioLaneCandidate,
    PortfolioLaneIntent,
    PortfolioPreparationError,
    PortfolioPublicationAttempt,
    PortfolioRevision,
    PortfolioSuperseded,
)
from backend.app.desktop.context_evolution import (
    ContextRevisionContract,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionPublisher,
    ContextRevisionRef,
    ContextRevisionRepository,
)
from backend.app.desktop.models import DesktopThread, DesktopWorkspace


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


class _CheckpointWriter:
    async def write(self, shadow_ref, execution_messages):
        return f"checkpoint-{shadow_ref.revision_id}", tuple(
            str(message["id"]) for message in execution_messages
        )


def _contract(context_id: str, generation: int, content: str) -> ContextRevisionContract:
    revision_id = uuid.uuid4().hex
    ref = ContextRevisionRef(
        context_id=context_id,
        revision_id=revision_id,
        generation=generation,
        execution_thread_id=f"thread-{context_id}",
        checkpoint_ns="",
        checkpoint_id=f"checkpoint-{revision_id}",
        payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
    )
    message = {"id": f"message-{revision_id}", "role": "human", "content": content}
    return ContextRevisionContract(
        ref=ref,
        authored_messages=(message,),
        execution_messages=(message,),
        initial_message_ids=(message["id"],),
        content_hash="c" * 64,
        projection_status=ContextRevisionProjectionStatus.VALID,
        origin_kind=ContextRevisionOriginKind.ROOT,
        created_at=datetime.now(UTC),
    )


def _compiled(
    lane_id: str,
    purpose: str,
    source: ContextRevisionRef,
    content: str,
) -> CompiledLaneCandidate:
    message = {"id": f"curated-{lane_id}", "role": "human", "content": content}
    return CompiledLaneCandidate(
        action="update",
        lane_id=lane_id,
        purpose=purpose,
        source_frontier=(source,),
        authored_messages=(message,),
        execution_messages=(message,),
        message_lineage=(),
        source_dispositions=(),
        definition_hash="d" * 64,
        projection_hash="p" * 64,
        content_hash="h" * 64,
        semantic_fingerprint=(lane_id[0] * 64),
        projection_status="valid",
    )


async def _seed(sessions, suffix: str):
    revisions = ContextRevisionRepository()
    programs = CurationProgramRepository()
    workspace_id = f"ws-atomic-{suffix}"
    program_id = uuid.uuid4().hex
    source_id = f"source-{suffix}"
    target_ids = [f"target-a-{suffix}", f"target-b-{suffix}", f"target-c-{suffix}"]
    source = _contract(source_id, 1, "source")
    bases = [_contract(context_id, 1, f"base-{index}") for index, context_id in enumerate(target_ids)]
    async with sessions.begin() as session:
        session.add(
            DesktopWorkspace(
                workspace_id=workspace_id,
                path=f"/tmp/{workspace_id}",
                display_name="Atomic Portfolio",
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
                for context_id in [source_id, *target_ids]
            ]
        )
        await session.flush()
        for contract in [source, *bases]:
            await revisions.insert(session, contract)
            await revisions.switch_current(session, contract.ref, None)
        program = await programs.create(session, workspace_id, program_id=program_id)
        lanes = [
            await programs.add_lane(
                session,
                program.program_id,
                purpose,
                managed_context_id=context_id,
                lane_id=f"lane-{letter}-{suffix}",
            )
            for letter, purpose, context_id in zip(
                ("a", "b", "c"),
                ("Implementation", "Testing", "Requirements"),
                target_ids,
            )
        ]
    return workspace_id, program_id, source, bases, lanes


def _freeze_request(program_id, source, lanes, workspace_revision="workspace-r1"):
    return PortfolioFreezeRequest(
        program_id=program_id,
        source_frontier=(source.ref,),
        lane_intents=(
            PortfolioLaneIntent(
                lane_id=lanes[0].lane_id,
                action=PortfolioLaneAction.UPDATE,
                purpose=lanes[0].purpose,
                source_allocation=(source.ref,),
                semantic_fingerprint="a" * 64,
            ),
            PortfolioLaneIntent(
                lane_id=lanes[1].lane_id,
                action=PortfolioLaneAction.UPDATE,
                purpose=lanes[1].purpose,
                source_allocation=(source.ref,),
                semantic_fingerprint="b" * 64,
            ),
            PortfolioLaneIntent(
                lane_id=lanes[2].lane_id,
                action=PortfolioLaneAction.KEEP,
                purpose=lanes[2].purpose,
                source_allocation=(source.ref,),
                semantic_fingerprint="c" * 64,
            ),
        ),
        workspace_revision=workspace_revision,
        loop_revision=4,
        grant_revision=2,
    )


async def _cleanup(sessions, workspace_id: str):
    async with sessions.begin() as session:
        await session.execute(
            delete(CurationProgram).where(CurationProgram.workspace_id == workspace_id)
        )
        await session.flush()
        await session.execute(
            delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace_id)
        )


def test_failed_candidate_keeps_previous_portfolio_and_context_pointers() -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        revisions = ContextRevisionRepository()
        freezer = PortfolioFreezer(sessions, revisions)
        preparer = PortfolioCandidatePreparer(
            sessions,
            ContextRevisionPublisher(revisions, _CheckpointWriter()),
            revisions,
        )
        workspace_id, program_id, source, bases, lanes = await _seed(
            sessions, uuid.uuid4().hex[:8]
        )
        try:
            frozen = await freezer.freeze(_freeze_request(program_id, source, lanes))
            async with sessions() as session:
                portfolio = await session.get(PortfolioRevision, frozen.portfolio_revision_id)
                assert portfolio.source_frontier == [source.ref.model_dump(mode="json")]
                assert portfolio.control_revisions["workspace_revision"] == "workspace-r1"
                assert [item["publisher_epoch"] for item in portfolio.target_lanes] == [1, 1, 1]
            compiled = {
                frozen.candidate_ids[0]: _compiled(
                    lanes[0].lane_id, lanes[0].purpose, source.ref, "next implementation"
                )
            }
            with pytest.raises(PortfolioPreparationError):
                await preparer.prepare(frozen.portfolio_revision_id, compiled)
            async with sessions() as session:
                current = [
                    (await revisions.current(session, base.ref.context_id)).ref.revision_id
                    for base in bases
                ]
                assert current == [base.ref.revision_id for base in bases]
                program = await session.get(CurationProgram, program_id)
                assert program.current_portfolio_revision_id is None
                staged = list(
                    (
                        await session.scalars(
                            select(PortfolioLaneCandidate).where(
                                PortfolioLaneCandidate.portfolio_revision_id
                                == frozen.portfolio_revision_id
                            )
                        )
                    ).all()
                )
                assert any(item.candidate_context_revision_id for item in staged)
        finally:
            await _cleanup(sessions, workspace_id)
            await engine.dispose()

    asyncio.run(run())


def test_atomic_publish_is_idempotent_and_outbox_consumes_once() -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        revisions = ContextRevisionRepository()
        outbox = CurationOutboxRepository()
        freezer = PortfolioFreezer(sessions, revisions)
        preparer = PortfolioCandidatePreparer(
            sessions,
            ContextRevisionPublisher(revisions, _CheckpointWriter()),
            revisions,
        )
        publisher = AtomicPortfolioPublisher(sessions, revisions, outbox)
        reader = PortfolioCurrentReader(sessions, revisions)
        workspace_id, program_id, source, bases, lanes = await _seed(
            sessions, uuid.uuid4().hex[:8]
        )
        try:
            frozen: FrozenPortfolio = await freezer.freeze(
                _freeze_request(program_id, source, lanes)
            )
            await preparer.prepare(
                frozen.portfolio_revision_id,
                {
                    frozen.candidate_ids[0]: _compiled(
                        lanes[0].lane_id, lanes[0].purpose, source.ref, "implementation r2"
                    ),
                    frozen.candidate_ids[1]: _compiled(
                        lanes[1].lane_id, lanes[1].purpose, source.ref, "testing r2"
                    ),
                },
            )
            result = await publisher.publish(frozen.portfolio_revision_id, frozen.controls)
            again = await publisher.publish(frozen.portfolio_revision_id, frozen.controls)
            assert result.context_revisions[0].revision_id != bases[0].ref.revision_id
            assert result.context_revisions[1].revision_id != bases[1].ref.revision_id
            assert result.context_revisions[2].revision_id == bases[2].ref.revision_id
            assert again.idempotent is True
            assert again.event_id == result.event_id
            visible = await reader.current(program_id)
            assert visible.portfolio_revision_id == frozen.portfolio_revision_id
            assert {lane.context_revision.revision_id for lane in visible.lanes} == {
                revision.revision_id for revision in result.context_revisions
            }
            applied = 0

            async def consume(event):
                nonlocal applied
                applied += 1

            async with sessions.begin() as session:
                assert await outbox.deliver_once(session, result.event_id, "loop-kernel", consume)
            async with sessions.begin() as session:
                assert not await outbox.deliver_once(
                    session, result.event_id, "loop-kernel", consume
                )
            assert applied == 1
            async with sessions() as session:
                program = await session.get(CurationProgram, program_id)
                event = await session.get(CurationOutboxEvent, result.event_id)
                receipts = list((await session.scalars(select(CurationOutboxDelivery))).all())
                attempt = await session.scalar(
                    select(PortfolioPublicationAttempt).where(
                        PortfolioPublicationAttempt.portfolio_revision_id
                        == frozen.portfolio_revision_id
                    )
                )
                assert program.current_portfolio_revision_id == frozen.portfolio_revision_id
                assert program.revision == 1
                assert event.status == "delivered"
                assert len(receipts) == 1
                assert attempt.status == "published"
        finally:
            await _cleanup(sessions, workspace_id)
            await engine.dispose()

    asyncio.run(run())


def test_restart_recovery_distinguishes_precommit_from_committed_state() -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        revisions = ContextRevisionRepository()
        freezer = PortfolioFreezer(sessions, revisions)
        preparer = PortfolioCandidatePreparer(
            sessions,
            ContextRevisionPublisher(revisions, _CheckpointWriter()),
            revisions,
        )
        publisher = AtomicPortfolioPublisher(sessions, revisions)
        workspace_id, program_id, source, bases, lanes = await _seed(
            sessions, uuid.uuid4().hex[:8]
        )
        try:
            frozen = await freezer.freeze(_freeze_request(program_id, source, lanes))
            await preparer.prepare(
                frozen.portfolio_revision_id,
                {
                    frozen.candidate_ids[0]: _compiled(
                        lanes[0].lane_id, lanes[0].purpose, source.ref, "implementation r2"
                    ),
                    frozen.candidate_ids[1]: _compiled(
                        lanes[1].lane_id, lanes[1].purpose, source.ref, "testing r2"
                    ),
                },
            )
            async with sessions.begin() as session:
                portfolio = await session.get(PortfolioRevision, frozen.portfolio_revision_id)
                portfolio.status = "publishing"
            assert await publisher.recover(frozen.portfolio_revision_id) == "retryable"
            async with sessions.begin() as session:
                program = await session.get(CurationProgram, program_id)
                program.current_portfolio_revision_id = frozen.portfolio_revision_id
                program.revision += 1
                candidates = list(
                    (
                        await session.scalars(
                            select(PortfolioLaneCandidate).where(
                                PortfolioLaneCandidate.portfolio_revision_id
                                == frozen.portfolio_revision_id
                            )
                        )
                    ).all()
                )
                for candidate in candidates:
                    if candidate.target_context_id is not None:
                        context = await session.get(
                            DesktopThread, candidate.target_context_id
                        )
                        context.current_revision_id = candidate.candidate_context_revision_id
                portfolio = await session.get(PortfolioRevision, frozen.portfolio_revision_id)
                portfolio.status = "publishing"
            assert await publisher.recover(frozen.portfolio_revision_id) == "published"
            async with sessions() as session:
                event = await session.scalar(
                    select(CurationOutboxEvent).where(
                        CurationOutboxEvent.aggregate_id == frozen.portfolio_revision_id,
                        CurationOutboxEvent.event_type == "PortfolioPublished",
                    )
                )
                assert event is not None
        finally:
            await _cleanup(sessions, workspace_id)
            await engine.dispose()

    asyncio.run(run())


def test_stale_controls_supersede_without_partial_switch() -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        revisions = ContextRevisionRepository()
        freezer = PortfolioFreezer(sessions, revisions)
        preparer = PortfolioCandidatePreparer(
            sessions,
            ContextRevisionPublisher(revisions, _CheckpointWriter()),
            revisions,
        )
        publisher = AtomicPortfolioPublisher(sessions, revisions)
        workspace_id, program_id, source, bases, lanes = await _seed(
            sessions, uuid.uuid4().hex[:8]
        )
        try:
            frozen = await freezer.freeze(_freeze_request(program_id, source, lanes))
            await preparer.prepare(
                frozen.portfolio_revision_id,
                {
                    frozen.candidate_ids[0]: _compiled(
                        lanes[0].lane_id, lanes[0].purpose, source.ref, "implementation r2"
                    ),
                    frozen.candidate_ids[1]: _compiled(
                        lanes[1].lane_id, lanes[1].purpose, source.ref, "testing r2"
                    ),
                },
            )
            stale = frozen.controls.model_copy(update={"workspace_revision": "workspace-r2"})
            with pytest.raises(PortfolioSuperseded):
                await publisher.publish(frozen.portfolio_revision_id, stale)
            async with sessions() as session:
                assert [
                    (await revisions.current(session, base.ref.context_id)).ref.revision_id
                    for base in bases
                ] == [base.ref.revision_id for base in bases]
                portfolio = await session.get(PortfolioRevision, frozen.portfolio_revision_id)
                program = await session.get(CurationProgram, program_id)
                assert portfolio.status == "superseded"
                assert program.current_portfolio_revision_id is None
                failure = await session.scalar(
                    select(CurationOutboxEvent).where(
                        CurationOutboxEvent.aggregate_id == frozen.portfolio_revision_id,
                        CurationOutboxEvent.event_type == "PortfolioPublicationFailed",
                    )
                )
                assert failure.payload["superseded"] is True
        finally:
            await _cleanup(sessions, workspace_id)
            await engine.dispose()

    asyncio.run(run())
