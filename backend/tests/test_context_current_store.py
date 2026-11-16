"""Current-only storage preserves active inputs, then releases them without archives."""

import asyncio
import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from focus.history import HistoryPayload, legacy_to_items
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.contexts.evolution import (
    ContextEvolutionService,
    ContextRevisionAlreadyExists,
    ContextRevisionContract,
    ContextRevisionCycle,
    ContextRevisionIdentityMismatch,
    ContextRevisionNotFound,
    ContextRevisionReader,
    ContextRevisionRef,
    ContextRevisionRepository,
    ContextRevisionSourceContract,
    StaleContextRevision,
)
from backend.app.desktop.contexts.evolution.models import (
    ContextInputSnapshot,
    ContextRevision,
)
from backend.app.desktop.persistence.models import DesktopThread, DesktopWorkspace

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def contract(context_id, generation=1, sources=(), initial=()):
    token = uuid.uuid4().hex
    return ContextRevisionContract(
        ref=ContextRevisionRef(
            context_id=context_id,
            revision_id=token,
            generation=generation,
            execution_thread_id="execution-" + context_id,
            checkpoint_id="checkpoint-" + token,
            payload_mode="checkpoint",
        ),
        sources=tuple(
            ContextRevisionSourceContract(source=source, position=i)
            for i, source in enumerate(sources)
        ),
        history_payload=HistoryPayload(
            execution_items=legacy_to_items(
                [
                    {
                        "id": token,
                        "role": "user",
                        "content": f"frozen generation {generation}",
                    }
                ]
            )
        ),
        initial_message_ids=initial,
        content_hash=token * 2,
        projection_status="valid",
        origin_kind="run_settled",
        created_at=datetime.now(UTC),
    )


async def workspace(sessions, *contexts):
    identity = uuid.uuid4().hex
    async with sessions.begin() as session:
        session.add(
            DesktopWorkspace(
                workspace_id=identity,
                path="/current-store/" + identity,
                display_name="current store",
            )
        )
        await session.flush()
        session.add_all(
            DesktopThread(
                task_id=context,
                workspace_id=identity,
                thread_id="thread-" + context,
                title=context,
            )
            for context in contexts
        )


def test_current_replacement_keeps_only_owned_inputs_and_rejects_late_publication():
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        repo = ContextRevisionRepository()
        context = uuid.uuid4().hex
        await workspace(sessions, context)
        first, second, third = (
            contract(context),
            contract(context, 2),
            contract(context, 3),
        )
        try:
            async with sessions.begin() as session:
                await repo.insert(session, first)
                assert await repo.current(session, context) is None
                await repo.freeze(
                    session,
                    first.ref,
                    owner_kind="curation_portfolio",
                    owner_id="pending-portfolio",
                )
                with pytest.raises(ContextRevisionNotFound):
                    await repo.get_input(session, first.ref)
                await repo.switch_current(session, first.ref, None)
                with pytest.raises(ContextRevisionAlreadyExists):
                    await repo.insert(
                        session, first.model_copy(update={"content_hash": "f" * 64})
                    )
                assert (await repo.get_input(session, first.ref)).ref == first.ref
                await repo.release(session, "curation_portfolio", "pending-portfolio")
                await repo.freeze(
                    session, first.ref, owner_kind="run", owner_id="old-run"
                )
                await repo.freeze(
                    session, first.ref, owner_kind="round", owner_id="old-round"
                )
            async with sessions.begin() as session:
                await repo.insert(session, second)
                forged = first.ref.model_copy(update={"execution_thread_id": "forged"})
                with pytest.raises(StaleContextRevision):
                    await repo.switch_current(session, second.ref, forged)
                await repo.switch_current(session, second.ref, first.ref)
            async with sessions.begin() as session:
                assert (
                    await repo.get(session, first.ref)
                ).execution_messages == first.execution_messages
                frozen_view = await ContextRevisionReader(repo, None).read(
                    session, first.ref, "execution"
                )
                assert frozen_view.messages == first.execution_messages
                assert (await repo.current(session, context)).ref == second.ref
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(ContextRevision)
                        .where(ContextRevision.context_id == context)
                    )
                    == 1
                )
                await repo.release(session, "run", "old-run")
                assert (await repo.get(session, first.ref)).ref == first.ref
                wrong = first.ref.model_copy(
                    update={"checkpoint_id": "not-the-frozen-checkpoint"}
                )
                with pytest.raises(ContextRevisionIdentityMismatch):
                    await repo.freeze(
                        session, wrong, owner_kind="run", owner_id="forged-run"
                    )
                with pytest.raises(StaleContextRevision):
                    await repo.switch_current(session, first.ref, first.ref)
                await repo.release(session, "round", "old-round")
                await repo.release(session, "round", "old-round")
                with pytest.raises(ContextRevisionNotFound):
                    await repo.get(session, first.ref)
                await repo.insert(session, third)
                await repo.discard_candidate(session, third.ref.revision_id)
                with pytest.raises(ContextRevisionNotFound):
                    await repo.get(session, third.ref)
                assert (await repo.current(session, context)).ref == second.ref
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(ContextInputSnapshot)
                        .where(ContextInputSnapshot.context_id == context)
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_continuation_carries_direct_provenance_without_parent_chain_and_rejects_candidate_cycles():
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        repo = ContextRevisionRepository()
        source_id, target_id, cycle_a_id, cycle_b_id = [
            uuid.uuid4().hex for _ in range(4)
        ]
        await workspace(sessions, source_id, target_id, cycle_a_id, cycle_b_id)
        source = contract(source_id)
        target = contract(
            target_id, sources=(source.ref,), initial=("authored-prefix",)
        )
        continuation = contract(target_id, 2, sources=(target.ref,))
        try:
            async with sessions.begin() as session:
                await repo.insert_many(session, (target, source))
                await repo.switch_current(session, source.ref, None)
                await repo.switch_current(session, target.ref, None)
            async with sessions.begin() as session:
                prepared = await repo.insert(session, continuation)
                assert tuple(edge.source for edge in prepared.sources) == (source.ref,)
                assert prepared.initial_message_ids == ("authored-prefix",)
                await repo.switch_current(session, continuation.ref, target.ref)
                with pytest.raises(ContextRevisionNotFound):
                    await repo.get(session, target.ref)
                a, b = contract(cycle_a_id), contract(cycle_b_id)
                a = a.model_copy(
                    update={
                        "sources": (
                            ContextRevisionSourceContract(source=b.ref, position=0),
                        )
                    }
                )
                b = b.model_copy(
                    update={
                        "sources": (
                            ContextRevisionSourceContract(source=a.ref, position=0),
                        )
                    }
                )
                with pytest.raises(ContextRevisionCycle):
                    await repo.insert_many(session, (a, b))
                assert await repo.current(session, cycle_a_id) is None
                assert await repo.current(session, cycle_b_id) is None
                bad = contract(
                    cycle_b_id,
                    sources=(
                        source.ref.model_copy(
                            update={"checkpoint_id": "forged-source"}
                        ),
                    ),
                )
                with pytest.raises(ContextRevisionIdentityMismatch):
                    await repo.insert_many(
                        session, (a.model_copy(update={"sources": ()}), bad)
                    )
                with pytest.raises(ContextRevisionNotFound):
                    await repo.get(session, a.ref)
                assert await repo.current(session, cycle_a_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_checkpoint_import_only_publishes_actual_head_and_keeps_pinned_reads_exact():
    async def run():
        from langchain_core.messages import HumanMessage

        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        repo = ContextRevisionRepository()
        context_id = uuid.uuid4().hex
        await workspace(sessions, context_id)
        first = contract(context_id)

        class Checkpointer:
            async def aget_tuple(self, config):
                request = config["configurable"]
                checkpoint_id = request.get("checkpoint_id", "settled-new-head")
                return SimpleNamespace(
                    config={
                        "configurable": {**request, "checkpoint_id": checkpoint_id}
                    },
                    checkpoint={
                        "channel_values": {
                            "messages": [
                                HumanMessage(
                                    id="new-message", content="new stable head"
                                )
                            ]
                        }
                    },
                )

        service = ContextEvolutionService(sessions, Checkpointer(), None)
        try:
            async with sessions.begin() as session:
                await repo.insert(session, first)
                await repo.switch_current(session, first.ref, None)
                await repo.freeze(
                    session,
                    first.ref,
                    owner_kind="round",
                    owner_id="checkpoint-import-round",
                )
            async with sessions.begin() as session:
                latest = await service.ensure_checkpoint_revision(
                    session, context_id, "settled-new-head"
                )
                assert latest.generation == 2
                assert (await repo.current(session, context_id)).ref == latest
                assert (
                    await service.ensure_checkpoint_revision(
                        session, context_id, first.ref.checkpoint_id
                    )
                    == first.ref
                )
                assert (await repo.current(session, context_id)).ref == latest
                await repo.release(session, "round", "checkpoint-import-round")
                with pytest.raises(ValueError, match="neither current execution head"):
                    await service.ensure_checkpoint_revision(
                        session, context_id, first.ref.checkpoint_id
                    )
                assert (await repo.current(session, context_id)).ref == latest
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(ContextInputSnapshot)
                        .where(ContextInputSnapshot.context_id == context_id)
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("has_stable_current", [True, False])
def test_curation_observation_cannot_publish_a_running_mains_intermediate_head(
    has_stable_current,
):
    """Main admission owns publication until finalization, including a first Run."""

    async def run():
        from focus.runtime.runs.manager import RunRecord
        from focus.runtime.runs.schemas import DisconnectMode, RunStatus
        from langchain_core.messages import HumanMessage

        from backend.app.desktop.contexts.evolution import ContextCheckpointBusy
        from backend.app.desktop.persistence.models import DesktopRun
        from backend.app.desktop.runs.orchestration.admission import RunAdmissionService
        from backend.app.desktop.runs.orchestration.lifecycle import (
            RunLifecycleFinalizer,
        )

        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        repo = ContextRevisionRepository()
        context_id, run_id = uuid.uuid4().hex, uuid.uuid4().hex
        await workspace(sessions, context_id)
        first = contract(context_id) if has_stable_current else None

        class Checkpointer:
            head = "in-flight-main-checkpoint"

            async def aget_tuple(self, config):
                request = config["configurable"]
                token = request.get("checkpoint_id", self.head)
                return SimpleNamespace(
                    config={"configurable": {**request, "checkpoint_id": token}},
                    checkpoint={
                        "channel_values": {
                            "messages": [HumanMessage(id=token, content=token)]
                        }
                    },
                )

        checkpointer = Checkpointer()
        evolution = ContextEvolutionService(sessions, checkpointer, None)
        try:
            async with sessions.begin() as session:
                if first is not None:
                    await repo.insert(session, first)
                    await repo.switch_current(session, first.ref, None)
                await RunAdmissionService().admit(
                    session,
                    DesktopRun(
                        run_id=run_id,
                        task_id=context_id,
                        agent_id="main:" + context_id,
                        kind="main",
                        status="running",
                        origin="direct_user",
                        execution_thread_id=first.ref.execution_thread_id
                        if first
                        else "thread-" + context_id,
                        checkpoint_ns=first.ref.checkpoint_ns if first else "",
                        context_revision_id=first.ref.revision_id if first else None,
                        context_checkpoint_id=first.ref.checkpoint_id
                        if first
                        else None,
                        idempotency_key="curation-race:" + run_id,
                        equipment={"model_name": "test-model"},
                        input_messages=[],
                    ),
                )
            async with sessions.begin() as session:
                with pytest.raises(ContextCheckpointBusy, match="Main is active"):
                    await evolution.ensure_checkpoint_revision(
                        session, context_id, checkpointer.head
                    )
                current = await repo.current(session, context_id)
                assert (current.ref if current else None) == (
                    first.ref if first else None
                )
                if first is not None:
                    stable = await evolution.ensure_checkpoint_revision(
                        session, context_id, first.ref.checkpoint_id
                    )
                    assert stable == first.ref
                    await repo.freeze(
                        session,
                        stable,
                        owner_kind="context_curator",
                        owner_id="main-observer-" + run_id,
                    )
            checkpointer.head = "successful-main-final-checkpoint"
            settled = await RunLifecycleFinalizer(sessions, checkpointer).finalize(
                RunRecord(
                    run_id=run_id,
                    thread_id=first.ref.execution_thread_id
                    if first
                    else "thread-" + context_id,
                    status=RunStatus.success,
                    on_disconnect=DisconnectMode.cancel,
                )
            )
            assert settled.context_publication == "published"
            async with sessions.begin() as session:
                current = await repo.current(session, context_id)
                assert current.ref.checkpoint_id == "successful-main-final-checkpoint"
                assert current.ref.generation == (2 if first else 1)
                assert (await session.get(DesktopRun, run_id)).status == "success"
                if first is not None:
                    frozen = await repo.get_input(session, first.ref)
                    assert frozen.ref.checkpoint_id == first.ref.checkpoint_id
                    await repo.release(
                        session, "context_curator", "main-observer-" + run_id
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())
