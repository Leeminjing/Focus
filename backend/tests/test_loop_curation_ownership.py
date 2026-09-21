r"""本文件对外提供 Loop 策展所有权与终态生命周期的 PostgreSQL 行为测试。

输入为 free、终态 stale、非终态 owner、关系不一致的 Context 数据及重复释放调用；输出为锁定分类、失败关闭、幂等退休、事件与唯一索引保持的断言。
具体工作流为在隔离数据库构造真实 Program/Lane/Loop 关系，经 CurationOwnershipRepository 执行查找、后继准备和释放，再读取持久状态验证。
示例：`pytest backend/tests/test_loop_curation_ownership.py`。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from backend.app.desktop.agent_loop.curation_ownership import (
    CurationOwnershipConflict,
    CurationOwnershipRecovery,
    CurationOwnershipRepository,
    CurationOwnershipState,
)
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.agent_loop.models import AgentLoop
from backend.app.desktop.agent_loop.models import LoopDelegationGrant
from backend.app.desktop.agent_loop.terminal_lifecycle import LoopTerminalLifecycle
from backend.app.desktop.context_curation.models import CurationLane, CurationProgram
from backend.app.desktop.models import DesktopThread, DesktopWorkspace


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_ownership_state_matrix_and_successor_reconciliation(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        terminal = {"completed", "stopped", "failed"}
        nonterminal = {"running", "paused", "waiting_user", "stopping"}
        try:
            async with sessions.begin() as session:
                workspace_id = uuid.uuid4().hex
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="ownership-matrix"))
                await session.flush()
                rows: dict[str, tuple[str, str, str]] = {}
                for status in sorted(terminal | nonterminal):
                    context_id = uuid.uuid4().hex
                    program_id = uuid.uuid4().hex
                    loop_id = uuid.uuid4().hex
                    lane_id = uuid.uuid4().hex
                    session.add_all([
                        DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{context_id}", title=status),
                        CurationProgram(program_id=program_id, workspace_id=workspace_id, policy={"owner_loop_id": loop_id}, revision=1),
                    ])
                    await session.flush()
                    session.add_all([
                        AgentLoop(loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id, program_id=program_id, holder_id="patrol", status=status, health="idle"),
                        CurationLane(lane_id=lane_id, program_id=program_id, managed_context_id=context_id, purpose=status, normalized_purpose=status, lane_policy={}),
                    ])
                    rows[status] = (context_id, loop_id, lane_id)
                free_context_id = uuid.uuid4().hex
                inconsistent_context_id = uuid.uuid4().hex
                inconsistent_program_id = uuid.uuid4().hex
                inconsistent_lane_id = uuid.uuid4().hex
                session.add_all([
                    DesktopThread(task_id=free_context_id, workspace_id=workspace_id, thread_id=f"thread-{free_context_id}", title="free"),
                    DesktopThread(task_id=inconsistent_context_id, workspace_id=workspace_id, thread_id=f"thread-{inconsistent_context_id}", title="inconsistent"),
                    CurationProgram(program_id=inconsistent_program_id, workspace_id=workspace_id, policy={}, revision=1),
                ])
                await session.flush()
                session.add(CurationLane(lane_id=inconsistent_lane_id, program_id=inconsistent_program_id, managed_context_id=inconsistent_context_id, purpose="orphan", normalized_purpose="orphan", lane_policy={}))
            repository = CurationOwnershipRepository(LoopEventJournal(enabled=True))
            async with sessions.begin() as session:
                assert (await repository.live_owner(session, free_context_id)).state == CurationOwnershipState.FREE
                inconsistent = await repository.live_owner(session, inconsistent_context_id)
                assert inconsistent.state == CurationOwnershipState.INCONSISTENT
                with pytest.raises(CurationOwnershipConflict):
                    await repository.prepare_successor(session, inconsistent_context_id, reason="test")
            for status, (context_id, _loop_id, lane_id) in rows.items():
                async with sessions.begin() as session:
                    ownership = await repository.live_owner(session, context_id)
                    expected = CurationOwnershipState.TERMINAL_STALE if status in terminal else CurationOwnershipState.OWNED
                    assert ownership.state == expected
                    if status in terminal:
                        await repository.prepare_successor(session, context_id, reason="matrix_repair")
                    else:
                        with pytest.raises(CurationOwnershipConflict):
                            await repository.prepare_successor(session, context_id, reason="matrix_conflict")
                async with sessions() as session:
                    lane = await session.get(CurationLane, lane_id)
                    assert lane.lifecycle == ("retired" if status in terminal else "active")
            async with sessions() as session:
                index_name = await session.scalar(text("SELECT indexname FROM pg_indexes WHERE indexname = 'uq_curation_lane_managed_publisher'"))
                assert index_name == "uq_curation_lane_managed_publisher"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_terminal_release_is_idempotent_and_audited_once(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workspace_id = uuid.uuid4().hex
        context_id = uuid.uuid4().hex
        program_id = uuid.uuid4().hex
        loop_id = uuid.uuid4().hex
        lane_id = uuid.uuid4().hex
        repository = CurationOwnershipRepository(LoopEventJournal(enabled=True))
        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="ownership-release"))
                await session.flush()
                session.add_all([
                    DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{context_id}", title="release"),
                    CurationProgram(program_id=program_id, workspace_id=workspace_id, policy={"owner_loop_id": loop_id}, revision=1),
                ])
                await session.flush()
                session.add_all([
                    AgentLoop(loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id, program_id=program_id, holder_id="patrol", status="stopped", health="idle"),
                    CurationLane(lane_id=lane_id, program_id=program_id, managed_context_id=context_id, purpose="release", normalized_purpose="release", lane_policy={}),
                ])
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, loop_id, with_for_update=True)
                first = await repository.release_terminal(session, loop, reason="user_stop")
                second = await repository.release_terminal(session, loop, reason="user_stop")
                assert first == (lane_id,)
                assert second == ()
            async with sessions() as session:
                lane = await session.get(CurationLane, lane_id)
                events = tuple((await session.scalars(select(LoopJournalEvent).where(LoopJournalEvent.loop_id == loop_id, LoopJournalEvent.kind == "curation.ownership.released"))).all())
                assert lane.lane_id == lane_id
                assert lane.program_id == program_id
                assert lane.lifecycle == "retired"
                assert lane.publisher_epoch == 2
                assert len(events) == 1
                assert events[0].payload["context_id"] == context_id
                assert events[0].payload["reason"] == "user_stop"
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("failure_stage", ["convergence", "ownership"])
def test_terminal_finalization_rolls_back_every_domain_on_failure(tmp_path: Path, failure_stage: str) -> None:
    class Convergence:
        async def converge(self, _session, _loop, _reason):
            if failure_stage == "convergence":
                raise RuntimeError("forced convergence failure")
            return ()

    class Ownership(CurationOwnershipRepository):
        async def release_terminal(self, session, loop, *, reason, event_kind="curation.ownership.released"):
            if failure_stage == "ownership":
                raise RuntimeError("forced ownership failure")
            return await super().release_terminal(session, loop, reason=reason, event_kind=event_kind)

    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workspace_id = uuid.uuid4().hex
        context_id = uuid.uuid4().hex
        program_id = uuid.uuid4().hex
        loop_id = uuid.uuid4().hex
        lane_id = uuid.uuid4().hex
        grant_id = uuid.uuid4().hex
        lifecycle = LoopTerminalLifecycle(Convergence(), Ownership(LoopEventJournal(enabled=True)), LoopEventJournal(enabled=True))
        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name=f"rollback-{failure_stage}"))
                await session.flush()
                session.add_all([
                    DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{context_id}", title="rollback"),
                    CurationProgram(program_id=program_id, workspace_id=workspace_id, policy={"owner_loop_id": loop_id}, revision=1),
                ])
                await session.flush()
                session.add_all([
                    AgentLoop(loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id, program_id=program_id, holder_id="patrol", status="running", health="observing"),
                    CurationLane(lane_id=lane_id, program_id=program_id, managed_context_id=context_id, purpose="rollback", normalized_purpose="rollback", lane_policy={}),
                    LoopDelegationGrant(grant_id=grant_id, loop_id=loop_id, revision=1, holder_id="patrol", capabilities=[], context_scope=[context_id], permission_scope=["read"], budgets={}, delegable_gates=[], compression_policy={}),
                ])
            with pytest.raises(RuntimeError, match="forced"):
                async with sessions.begin() as session:
                    loop = await session.get(AgentLoop, loop_id, with_for_update=True)
                    await lifecycle.finalize(session, loop, "stopped", "rollback_test")
            async with sessions() as session:
                loop = await session.get(AgentLoop, loop_id)
                lane = await session.get(CurationLane, lane_id)
                grant = await session.get(LoopDelegationGrant, grant_id)
                events = tuple((await session.scalars(select(LoopJournalEvent).where(LoopJournalEvent.loop_id == loop_id))).all())
                assert loop.status == "running"
                assert loop.completed_at is None
                assert lane.lifecycle == "active"
                assert grant.status == "active"
                assert events == ()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_startup_recovery_is_idempotent_and_completes_migration_audit(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workspace_id = uuid.uuid4().hex
        context_id = uuid.uuid4().hex
        program_id = uuid.uuid4().hex
        loop_id = uuid.uuid4().hex
        lane_id = uuid.uuid4().hex
        orphan_context_id = uuid.uuid4().hex
        orphan_program_id = uuid.uuid4().hex
        orphan_lane_id = uuid.uuid4().hex
        recovery = CurationOwnershipRecovery(sessions, CurationOwnershipRepository(LoopEventJournal(enabled=True)))
        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="ownership-recovery"))
                await session.flush()
                session.add_all([
                    DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{context_id}", title="recovery"),
                    DesktopThread(task_id=orphan_context_id, workspace_id=workspace_id, thread_id=f"thread-{orphan_context_id}", title="orphan"),
                    CurationProgram(program_id=program_id, workspace_id=workspace_id, control_state="stopped", policy={"owner_loop_id": loop_id}, revision=2),
                    CurationProgram(program_id=orphan_program_id, workspace_id=workspace_id, policy={}, revision=1),
                ])
                await session.flush()
                session.add_all([
                    AgentLoop(loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id, program_id=program_id, holder_id="patrol", status="stopped", health="idle"),
                    CurationLane(lane_id=lane_id, program_id=program_id, managed_context_id=context_id, purpose="recovery", normalized_purpose="recovery", lane_policy={}, lifecycle="retired", publisher_epoch=2),
                    CurationLane(lane_id=orphan_lane_id, program_id=orphan_program_id, managed_context_id=orphan_context_id, purpose="orphan", normalized_purpose="orphan", lane_policy={}),
                ])
            first = await recovery.reconcile()
            second = await recovery.reconcile()
            assert first.retired_lane_ids == second.retired_lane_ids == ()
            assert orphan_context_id in {item.context_id for item in first.diagnostics}
            assert first.diagnostics == second.diagnostics
            async with sessions() as session:
                events = tuple((await session.scalars(select(LoopJournalEvent).where(LoopJournalEvent.loop_id == loop_id, LoopJournalEvent.kind == "curation.ownership.repaired"))).all())
                assert len(events) == 1
                assert events[0].payload["lane_id"] == lane_id
                assert events[0].payload["reason"] == "startup_reconciliation"
                assert (await session.get(CurationLane, orphan_lane_id)).lifecycle == "active"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_migration_retires_only_proven_terminal_owners_and_downgrade_keeps_them_safe(tmp_path: Path) -> None:
    migrations = Path(__file__).parents[1] / "packages" / "harness" / "focus" / "persistence" / "migrations" / "alembic.ini"
    config = Config(str(migrations))
    engine = create_engine(make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg"))
    workspace_id = uuid.uuid4().hex
    terminal_context_id = uuid.uuid4().hex
    running_context_id = uuid.uuid4().hex
    terminal_program_id = uuid.uuid4().hex
    running_program_id = uuid.uuid4().hex
    terminal_loop_id = uuid.uuid4().hex
    running_loop_id = uuid.uuid4().hex
    terminal_lane_id = uuid.uuid4().hex
    running_lane_id = uuid.uuid4().hex
    try:
        command.downgrade(config, "1b2c3d4e5f6a")
        with Session(engine) as session, session.begin():
            session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="ownership-migration"))
            session.flush()
            session.add_all([
                DesktopThread(task_id=terminal_context_id, workspace_id=workspace_id, thread_id=f"thread-{terminal_context_id}", title="terminal"),
                DesktopThread(task_id=running_context_id, workspace_id=workspace_id, thread_id=f"thread-{running_context_id}", title="running"),
                CurationProgram(program_id=terminal_program_id, workspace_id=workspace_id, policy={"owner_loop_id": terminal_loop_id}, revision=1),
                CurationProgram(program_id=running_program_id, workspace_id=workspace_id, policy={"owner_loop_id": running_loop_id}, revision=1),
            ])
            session.flush()
            session.add_all([
                AgentLoop(loop_id=terminal_loop_id, workspace_id=workspace_id, initial_context_id=terminal_context_id, program_id=terminal_program_id, holder_id="patrol", status="stopped", health="idle"),
                AgentLoop(loop_id=running_loop_id, workspace_id=workspace_id, initial_context_id=running_context_id, program_id=running_program_id, holder_id="patrol", status="running", health="observing"),
                CurationLane(lane_id=terminal_lane_id, program_id=terminal_program_id, managed_context_id=terminal_context_id, purpose="terminal", normalized_purpose="terminal", lane_policy={}),
                CurationLane(lane_id=running_lane_id, program_id=running_program_id, managed_context_id=running_context_id, purpose="running", normalized_purpose="running", lane_policy={}),
            ])
        command.upgrade(config, "head")
        with Session(engine) as session:
            terminal_lane = session.get(CurationLane, terminal_lane_id)
            running_lane = session.get(CurationLane, running_lane_id)
            assert terminal_lane.lifecycle == "retired"
            assert terminal_lane.publisher_epoch == 2
            assert running_lane.lifecycle == "active"
            assert session.get(AgentLoop, terminal_loop_id).program_id == terminal_program_id
        command.downgrade(config, "1b2c3d4e5f6a")
        with Session(engine) as session:
            assert session.get(CurationLane, terminal_lane_id).lifecycle == "retired"
        command.upgrade(config, "head")
        with Session(engine) as session:
            assert session.get(CurationLane, terminal_lane_id).publisher_epoch == 2
    finally:
        command.upgrade(config, "head")
        engine.dispose()
