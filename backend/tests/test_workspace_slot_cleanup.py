"""Exercise automatic retention with real PostgreSQL and managed Git worktrees."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.models import AgentLoop
from backend.app.desktop.agent_loop.workspace_cleanup import LoopWorkspaceCleanup
from backend.app.desktop.domain_evidence.models import DesktopDomainResult
from backend.app.desktop.models import DesktopRun, DesktopWorkspace
from backend.app.desktop.run_orchestration.lifecycle import RunLifecycleFinalizer
from backend.app.desktop.workspace_coordination import (
    GitWorktreeIsolationProvider, IsolationRequest, RunExecutionAnchor,
    WorkspaceAdoption, WorkspaceFingerprinter, WorkspaceLease, WorkspaceSlot,
    WorkspaceSlotLifecycle, WorkspaceSlotTombstone,
)
from backend.tests.test_git_workspace_adoption import _git, _repository
from backend.tests.test_loop_event_journal import _seed_loop
from focus.runtime.runs.manager import RunRecord
from focus.runtime.runs.schemas import DisconnectMode, RunStatus


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


@asynccontextmanager
async def _environment(tmp_path, monkeypatch):
    import backend.app.desktop.workspace_coordination.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "global_home", lambda: tmp_path / "home")
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    loop = None
    try:
        loop_id = await _seed_loop(sessions, tmp_path)
        async with sessions() as session:
            loop = await session.get(AgentLoop, loop_id)
        root = tmp_path / "repository"
        baseline = _repository(root)
        slot_id, run_id = uuid.uuid4().hex, uuid.uuid4().hex
        managed = tmp_path / "home" / "loop-workspaces" / loop.workspace_id
        isolated = await GitWorktreeIsolationProvider(managed).prepare(IsolationRequest(
            source_root=root, target_root=managed / "loop" / slot_id[:8],
            baseline=baseline, loop_id=loop_id, lane_id="lane"))
        fingerprint = WorkspaceFingerprinter().capture(isolated.root_path).digest
        old = datetime.now(UTC) - timedelta(days=2)
        async with sessions.begin() as session:
            slot = WorkspaceSlot(slot_id=slot_id, workspace_id=loop.workspace_id,
                kind="isolated", root_path=str(isolated.root_path), provider="git_worktree",
                base_revision=baseline, current_fingerprint=fingerprint, owner_loop_id=loop_id,
                metadata_json={"cleanup_token": isolated.cleanup_token})
            run = DesktopRun(run_id=run_id, task_id=loop.initial_context_id,
                agent_id=run_id, kind="main", status="success", settled_at=old,
                workspace_anchor={"slot_id": slot_id, "mode": "write"})
            session.add_all([slot, run])
            await session.flush()
            session.add(RunExecutionAnchor(run_id=run_id, slot_id=slot_id,
                observed_workspace_revision=1, observed_fingerprint=fingerprint,
                resulting_workspace_revision=1, resulting_fingerprint=fingerprint,
                settled_at=old, adoption_state="not_required"))
        yield sessions, loop, slot, run, isolated.root_path
    finally:
        if loop is not None:
            async with sessions.begin() as session:
                slot_ids = select(WorkspaceSlot.slot_id).where(WorkspaceSlot.workspace_id == loop.workspace_id)
                await session.execute(delete(WorkspaceAdoption).where(or_(
                    WorkspaceAdoption.source_slot_id.in_(slot_ids), WorkspaceAdoption.target_slot_id.in_(slot_ids))))
                await session.execute(delete(DesktopDomainResult).where(
                    DesktopDomainResult.context_id == loop.initial_context_id))
                await session.execute(delete(DesktopRun).where(DesktopRun.task_id == loop.initial_context_id))
                await session.execute(delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == loop.workspace_id))
        await engine.dispose()


def test_background_cleanup_removes_settled_worktree_once(tmp_path, monkeypatch):
    async def exercise():
        async with _environment(tmp_path, monkeypatch) as (sessions, _, slot, _, root):
            cleanup = LoopWorkspaceCleanup(sessions)
            assert sum(await asyncio.gather(cleanup.drain(), cleanup.drain())) == 1
            assert not root.exists()
            assert str(root) not in _git(Path(slot.metadata_json["cleanup_token"]), "worktree", "list", "--porcelain")
            assert await cleanup.drain() == 0
            assert not await WorkspaceSlotLifecycle(sessions).cleanup(slot.slot_id, "duplicate")
            async with sessions() as session:
                assert (await session.get(WorkspaceSlot, slot.slot_id)).lifecycle == "deleted"
                assert await session.scalar(select(func.count()).select_from(WorkspaceSlotTombstone).where(
                    WorkspaceSlotTombstone.slot_id == slot.slot_id)) == 1
    asyncio.run(exercise())


@pytest.mark.parametrize("guard", ["pending", "conflict", "uncertain", "read_changed", "active_lease",
    "unsettled_expired_lease", "shell_cleanup", "reserved_adoption", "retention", "external_edit",
    "outside_managed_root"])
def test_background_cleanup_preserves_protected_worktrees(tmp_path, monkeypatch, guard):
    async def exercise():
        async with _environment(tmp_path, monkeypatch) as (sessions, loop, slot, run, root):
            async with sessions.begin() as session:
                anchor = await session.get(RunExecutionAnchor, run.run_id)
                persisted = await session.get(WorkspaceSlot, slot.slot_id)
                current = await session.get(DesktopRun, run.run_id)
                if guard in {"pending", "conflict"}:
                    anchor.adoption_state = guard
                elif guard == "uncertain":
                    anchor.resulting_fingerprint = None
                elif guard == "read_changed":
                    anchor.observed_fingerprint = "b" * 64
                elif guard in {"active_lease", "unsettled_expired_lease"}:
                    session.add(WorkspaceLease(lease_id=uuid.uuid4().hex, slot_id=slot.slot_id,
                        run_id=run.run_id, mode="write", fencing_token=1, status="active",
                        expires_at=datetime.now(UTC) + timedelta(seconds=60 if guard == "active_lease" else -60)))
                    if guard == "unsettled_expired_lease":
                        current.settled_at, current.status = None, "running"
                elif guard == "shell_cleanup":
                    current.settled_at = None
                    current.workspace_result = {"shell_cleanup": {"attempt": "unknown"}}
                elif guard == "reserved_adoption":
                    target_id = uuid.uuid4().hex
                    session.add(WorkspaceSlot(slot_id=target_id, workspace_id=loop.workspace_id,
                        kind="authoritative", root_path=slot.metadata_json["cleanup_token"],
                        current_fingerprint=slot.current_fingerprint))
                    await session.flush()
                    session.add(WorkspaceAdoption(adoption_id=uuid.uuid4().hex,
                        source_slot_id=slot.slot_id, target_slot_id=target_id, source_revision=1,
                        expected_target_revision=1, status="adopting", application_intent={"pending": True}))
                elif guard == "retention":
                    persisted.retention_until = datetime.now(UTC) + timedelta(days=5)
                elif guard == "external_edit":
                    (root / "valuable.txt").write_text("keep user edits", encoding="utf-8")
                elif guard == "outside_managed_root":
                    monkeypatch.setattr("backend.app.desktop.workspace_coordination.lifecycle.global_home",
                        lambda: tmp_path / "different-home")
            assert await LoopWorkspaceCleanup(sessions).drain() == 0
            assert root.is_dir()
            async with sessions() as session:
                assert (await session.get(WorkspaceSlot, slot.slot_id)).lifecycle != "deleted"
                assert not await session.scalar(select(WorkspaceSlotTombstone).where(
                    WorkspaceSlotTombstone.slot_id == slot.slot_id))
    asyncio.run(exercise())


def test_unused_slot_waits_for_loop_terminal_and_retention(tmp_path, monkeypatch):
    async def exercise():
        async with _environment(tmp_path, monkeypatch) as (sessions, loop, slot, run, root):
            async with sessions.begin() as session:
                await session.delete(await session.get(RunExecutionAnchor, run.run_id))
            cleanup = LoopWorkspaceCleanup(sessions)
            assert await cleanup.drain() == 0
            async with sessions.begin() as session:
                owner = await session.get(AgentLoop, loop.loop_id)
                owner.status, owner.completed_at = "stopped", datetime.now(UTC)
            assert await cleanup.drain() == 0
            async with sessions.begin() as session:
                retained = await session.get(WorkspaceSlot, slot.slot_id)
                deadline = retained.retention_until
                assert deadline > datetime.now(UTC) + timedelta(hours=23)
                owner = await session.get(AgentLoop, loop.loop_id)
                owner.completed_at -= timedelta(days=2)
                retained.retention_until = datetime.now(UTC) - timedelta(seconds=1)
            # Recreate the worker: retention is durable and no new Loop event is required.
            assert await LoopWorkspaceCleanup(sessions).drain() == 1
            assert not root.exists()
    asyncio.run(exercise())


def test_run_finalization_is_seen_by_cleanup_after_commit(tmp_path, monkeypatch):
    async def exercise():
        async with _environment(tmp_path, monkeypatch) as (sessions, _, slot, run, root):
            async with sessions.begin() as session:
                current = await session.get(DesktopRun, run.run_id)
                current.settled_at, current.status = None, "running"
                anchor = await session.get(RunExecutionAnchor, run.run_id)
                anchor.settled_at, anchor.resulting_fingerprint = None, None
            cleanup = LoopWorkspaceCleanup(sessions, retention=timedelta(0))
            assert await cleanup.drain() == 0
            class Checkpointer:
                async def aget_tuple(self, config):
                    return None
            record = RunRecord(run_id=run.run_id, thread_id=run.run_id,
                status=RunStatus.success, on_disconnect=DisconnectMode.cancel)
            await RunLifecycleFinalizer(sessions, Checkpointer()).finalize(record)
            assert await cleanup.drain() == 1
            assert not root.exists()
    asyncio.run(exercise())


def test_adopted_result_retention_starts_at_adoption(tmp_path, monkeypatch):
    async def exercise():
        async with _environment(tmp_path, monkeypatch) as (sessions, loop, slot, run, root):
            adoption_id, target_id = uuid.uuid4().hex, uuid.uuid4().hex
            async with sessions.begin() as session:
                anchor = await session.get(RunExecutionAnchor, run.run_id)
                anchor.observed_fingerprint, anchor.adoption_state = "b" * 64, "adopted"
                session.add(WorkspaceSlot(slot_id=target_id, workspace_id=loop.workspace_id,
                    kind="authoritative", root_path=slot.metadata_json["cleanup_token"],
                    current_fingerprint=slot.current_fingerprint))
                await session.flush()
                session.add(WorkspaceAdoption(adoption_id=adoption_id, source_slot_id=slot.slot_id,
                    target_slot_id=target_id, source_revision=1, expected_target_revision=1,
                    status="adopted", completed_at=datetime.now(UTC)))
            assert await LoopWorkspaceCleanup(sessions).drain() == 0
            assert root.exists()
            async with sessions.begin() as session:
                adoption = await session.get(WorkspaceAdoption, adoption_id)
                adoption.completed_at -= timedelta(days=2)
                persisted = await session.get(WorkspaceSlot, slot.slot_id)
                persisted.retention_until = datetime.now(UTC) - timedelta(seconds=1)
            assert await LoopWorkspaceCleanup(sessions).drain() == 1
    asyncio.run(exercise())


def test_cleanup_waits_for_removal_and_commits_before_cancellation(tmp_path, monkeypatch):
    async def exercise():
        async with _environment(tmp_path, monkeypatch) as (sessions, _, slot, _, root):
            entered, release = asyncio.Event(), asyncio.Event()
            original = WorkspaceSlotLifecycle._remove_physical
            async def blocked(snapshot):
                entered.set()
                await release.wait()
                await original(snapshot)
            monkeypatch.setattr(WorkspaceSlotLifecycle, "_remove_physical", staticmethod(blocked))
            task = asyncio.create_task(WorkspaceSlotLifecycle(sessions).cleanup(slot.slot_id, "cancel"))
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and root.exists()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with sessions() as session:
                assert (await session.get(WorkspaceSlot, slot.slot_id)).lifecycle == "deleted"
            assert not root.exists()
    asyncio.run(exercise())


def test_failed_removal_keeps_identity_and_retries(tmp_path, monkeypatch):
    async def exercise():
        async with _environment(tmp_path, monkeypatch) as (sessions, _, slot, _, root):
            original = WorkspaceSlotLifecycle._remove_physical
            async def fail(snapshot):
                raise OSError("directory busy")
            monkeypatch.setattr(WorkspaceSlotLifecycle, "_remove_physical", staticmethod(fail))
            assert await LoopWorkspaceCleanup(sessions).drain() == 0
            async with sessions() as session:
                retained = await session.get(WorkspaceSlot, slot.slot_id)
                assert retained.lifecycle == "retained" and "directory busy" in retained.metadata_json["cleanup_error"]
            assert root.exists()
            monkeypatch.setattr(WorkspaceSlotLifecycle, "_remove_physical", staticmethod(original))
            assert await LoopWorkspaceCleanup(sessions).drain() == 1
    asyncio.run(exercise())
