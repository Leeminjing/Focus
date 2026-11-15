"""本文件对外提供普通 Main 准入、最终 checkpoint 发布与 Patrol 所有权的行锁回归。

输入为隔离 PostgreSQL、真实准入/结算服务和两个独立事务；输出为无死锁、唯一派发、原子发布及所有权拒绝断言。
具体工作流为在结算持有 Run 锁时提交下一条输入或重放幂等输入，再验证 Context 锁等待后的 Patrol 所有权复检。
屏障只控制真实 SQL 的交错，不替换数据库读写或重试死锁。示例：pytest backend/tests/test_main_admission_lock_order.py。
"""

import asyncio
import os
import uuid

import pytest
from fastapi import HTTPException
from focus.runtime.runs.manager import RunRecord
from focus.runtime.runs.schemas import DisconnectMode, RunStatus
from langchain_core.messages import AIMessage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.desktop.contexts.evolution import ContextRevisionRepository
from backend.app.desktop.patrol.agent_loop.curation.curation_ownership import (
    CurationOwnershipRepository,
)
from backend.app.desktop.patrol.agent_loop.execution.execution_ownership import (
    RunOwnershipPolicy,
)
from backend.app.desktop.patrol.agent_loop.models import (
    AgentLoop,
    LoopContextMembership,
)
from backend.app.desktop.persistence.models import DesktopRun, DesktopThread
from backend.app.desktop.runs.orchestration.admission import RunAdmissionService
from backend.app.desktop.runs.orchestration.lifecycle import RunLifecycleFinalizer
from backend.app.desktop.runs.orchestration.models import RunDispatch, RunOutboxEvent
from backend.tests.test_run_dispatch_durability import _run
from backend.tests.test_run_dispatch_lock_order import _admit
from backend.tests.test_unified_run_orchestration import _Checkpointer

pytestmark = pytest.mark.usefixtures("runtime_postgres_database")


def _locks_run(statement):
    return (
        statement._for_update_arg is not None
        and statement.column_descriptions[0].get("entity") is DesktopRun
    )


@pytest.mark.parametrize(
    ("replay", "keyed"), [(False, True), (False, False), (True, True)],
    ids=["next-input", "unkeyed-input", "idempotent-replay"],
)
def test_main_admission_and_checkpoint_publication_lock_run_before_context(tmp_path, replay, keyed):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        run_locked, admission_waiting = asyncio.Event(), asyncio.Event()
        run_lock_requests = []
        tasks = []

        class SettlementSession(AsyncSession):
            async def scalar(self, statement, **kwargs):
                value = await super().scalar(statement, **kwargs)
                if _locks_run(statement):
                    run_locked.set()
                    await asyncio.wait_for(admission_waiting.wait(), 5)
                return value

        class AdmissionSession(AsyncSession):
            async def scalar(self, statement, **kwargs):
                if _locks_run(statement):
                    run_lock_requests.append(statement)
                    # New keyed input first checks its absent idempotency row;
                    # release settlement only when admission requests the held Run.
                    if len(run_lock_requests) == (2 if keyed and not replay else 1):
                        admission_waiting.set()
                return await super().scalar(statement, **kwargs)

        settling = async_sessionmaker(engine, class_=SettlementSession, expire_on_commit=False)
        admitting = async_sessionmaker(engine, class_=AdmissionSession, expire_on_commit=False)
        try:
            context_id, previous_id, previous_dispatch = await _admit(sessions, tmp_path)
            async with sessions.begin() as session:
                (await session.get(DesktopRun, previous_id)).status = "running"
            checkpoint_id = uuid.uuid4().hex
            finalizer = RunLifecycleFinalizer(settling, _Checkpointer(
                checkpoint_id, messages=(AIMessage(content="完成", id="final-answer"),),
            ))
            record = RunRecord(
                run_id=previous_id, thread_id=f"thread-{context_id}", status=RunStatus.success,
                on_disconnect=DisconnectMode.continue_, model_call_count=1,
                prompt_input_tokens=7, prompt_output_tokens=3,
            )
            next_id = uuid.uuid4().hex

            async def admit():
                await asyncio.wait_for(run_locked.wait(), 5)
                async with admitting.begin() as session:
                    return await RunAdmissionService(RunOwnershipPolicy().admit).admit(
                        session, _run(context_id, next_id, previous_id if replay else next_id if keyed else None, "下一条输入"),
                    )

            tasks = [asyncio.create_task(finalizer.finalize(record)), asyncio.create_task(admit())]
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 15)
            assert not [result for result in results if isinstance(result, BaseException)], results
            settlement, accepted = results
            assert settlement.context_publication == "published"
            assert accepted.created is not replay
            assert accepted.run.run_id == (previous_id if replay else next_id)
            async with sessions() as session:
                previous = await session.get(DesktopRun, previous_id)
                assert previous.status == "success" and previous.settled_at is not None
                assert (previous.model_call_count, previous.prompt_input_tokens, previous.prompt_output_tokens) == (1, 7, 3)
                assert (await session.get(RunDispatch, previous_dispatch)).status == "settled"
                current = await ContextRevisionRepository().current(session, context_id)
                assert current.ref == settlement.context_revision
                assert current.ref.checkpoint_id == checkpoint_id
                assert await session.scalar(select(func.count()).select_from(RunOutboxEvent).where(
                    RunOutboxEvent.run_id == previous_id,
                )) == 1
                assert await session.scalar(select(func.count()).select_from(RunDispatch)) == (1 if replay else 2)
                if not replay:
                    assert (await session.get(RunDispatch, accepted.dispatch.dispatch_id)).status == "accepted"
            assert (await finalizer.finalize(record)).idempotent
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("replay", [False, True], ids=["new-input", "idempotent-replay"])
@pytest.mark.parametrize("membership_status", ["active", "paused"])
def test_main_admission_rechecks_patrol_owner_after_context_lock_wait(tmp_path, replay, membership_status):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        context_requested = asyncio.Event()
        pending = None

        class AdmissionSession(AsyncSession):
            async def get(self, entity, ident, **kwargs):
                if entity is DesktopThread and kwargs.get("with_for_update"):
                    context_requested.set()
                return await super().get(entity, ident, **kwargs)

        admitting = async_sessionmaker(engine, class_=AdmissionSession, expire_on_commit=False)
        try:
            context_id, previous_id, _ = await _admit(sessions, tmp_path)
            # A settled idempotent Run must also pass the ownership check on replay.
            await RunLifecycleFinalizer(sessions, None).abort_prepared(previous_id, "fixture settled")
            async with sessions() as session:
                workspace_id = (await session.get(DesktopThread, context_id)).workspace_id

            async def admit():
                async with admitting.begin() as session:
                    with pytest.raises(HTTPException) as rejected:
                        next_id = uuid.uuid4().hex
                        await RunAdmissionService(RunOwnershipPolicy().admit).admit(
                            session, _run(context_id, next_id, previous_id if replay else next_id, "竞争输入"),
                        )
                    assert rejected.value.status_code == 409 and "统一输入" in rejected.value.detail

            async with sessions.begin() as ownership:
                assert await CurationOwnershipRepository().lock_context(ownership, context_id) is not None
                pending = asyncio.create_task(admit())
                await asyncio.wait_for(context_requested.wait(), 5)
                loop_id = uuid.uuid4().hex
                ownership.add(AgentLoop(
                    loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id,
                    holder_id="patrol-owner", status="running", interaction_mode="workspace_patrol",
                ))
                await ownership.flush()
                ownership.add(LoopContextMembership(
                    membership_id=uuid.uuid4().hex, loop_id=loop_id,
                    context_id=context_id, status=membership_status, role="primary",
                ))
            await asyncio.wait_for(pending, 5)
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(DesktopRun)) == 1
                assert await session.scalar(select(func.count()).select_from(RunDispatch)) == 1
        finally:
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await engine.dispose()

    asyncio.run(run())
