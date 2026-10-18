"""真实 Git/PostgreSQL 采用崩溃演练：子进程在 git apply 落盘后立即退出，再从持久事实恢复。"""

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.workspace_coordination import (
    GitWorkspaceResultApplier, WorkspaceAdopter, WorkspaceAdoption,
    WorkspaceAdoptionRequest, WorkspaceFingerprinter, WorkspaceSlot,
    WorkspaceAdoptionConflict, WorkspaceLeaseConflict, WorkspaceLeaseManager, WorkspaceLeaseRequest,
    WorkspaceSlotLifecycle,
    RunExecutionAnchor,
)
from backend.app.desktop.workspace_coordination.fingerprints import WorkspaceRevisionConflict
from backend.tests.test_git_workspace_adoption import _git, _repository


_CRASH = r'''
import asyncio, json, os, sys
from pathlib import Path
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from backend.app.desktop.workspace_coordination import GitWorkspaceResultApplier, WorkspaceAdopter, WorkspaceAdoptionRequest

class CrashAfterApply(GitWorkspaceResultApplier):
    async def apply(self, target, intent):
        if sys.argv[3] == "before_apply":
            os._exit(73)
        return await super().apply(target, intent)

    async def _git(self, root, *args, **kwargs):
        result = await super()._git(root, *args, **kwargs)
        if sys.argv[3] == "after_apply" and Path(root) == Path(sys.argv[2]) and args[:2] == ("apply", "--binary"):
            os._exit(73)
        return result

async def run():
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    await WorkspaceAdopter(async_sessionmaker(engine, expire_on_commit=False)).adopt(
        WorkspaceAdoptionRequest(**json.loads(sys.argv[1])), CrashAfterApply())
    if sys.argv[3] == "after_commit":
        os._exit(73)
asyncio.run(run())
'''


async def _seed(sessions, tmp_path):
    target, source = tmp_path / "target", tmp_path / "source"
    baseline = _repository(target)
    _git(target, "worktree", "add", "--detach", str(source), baseline)
    (source / "tracked.txt").write_text("adopted result\n", encoding="utf-8")
    (source / "new.bin").write_bytes(b"\0\xffbinary result")
    workspace_id, source_id, target_id, adoption_id = (uuid.uuid4().hex for _ in range(4))
    async with sessions.begin() as session:
        session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(target), display_name="crash drill"))
        await session.flush()
        for slot_id, root, kind in ((source_id, source, "isolated"), (target_id, target, "authoritative")):
            session.add(WorkspaceSlot(slot_id=slot_id, workspace_id=workspace_id, kind=kind,
                root_path=str(root), base_revision=baseline,
                current_fingerprint=WorkspaceFingerprinter().capture(root).digest))
    return WorkspaceAdoptionRequest(adoption_id=adoption_id, source_slot_id=source_id,
        target_slot_id=target_id, source_revision=1, expected_target_revision=1), source, target


async def _crash(request, target, phase="after_apply"):
    process = await asyncio.to_thread(subprocess.run,
        [sys.executable, "-c", _CRASH, request.model_dump_json(), str(target), phase],
        capture_output=True, timeout=30)
    assert process.returncode == 73, process.stderr.decode(errors="replace")


def test_process_exit_after_apply_recovers_exactly_once(isolated_postgres_database, tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            request, source, target = await _seed(sessions, tmp_path)
            await _crash(request, target)
            assert (target / "tracked.txt").read_text(encoding="utf-8") == "adopted result\n"
            assert (target / "new.bin").read_bytes() == b"\0\xffbinary result"
            async with sessions() as session:
                slot = await session.get(WorkspaceSlot, request.target_slot_id)
                assert slot.revision == 1
                assert slot.current_fingerprint != WorkspaceFingerprinter().capture(target).digest
                intent = (await session.get(WorkspaceAdoption, request.adoption_id)).application_intent
                assert intent["before_fingerprint"] == slot.current_fingerprint
                assert intent["after_fingerprint"] == WorkspaceFingerprinter().capture(target).digest
            adopter = WorkspaceAdopter(sessions)
            result, = await adopter.recover_pending()
            assert result.status == "adopted", result.conflict
            assert result.resulting_target_revision == 2
            assert (await adopter.adopt(request, GitWorkspaceResultApplier())).status == "adopted"
            async with sessions() as session:
                slot = await session.get(WorkspaceSlot, request.target_slot_id)
                assert slot.revision == 2
                assert slot.current_fingerprint == WorkspaceFingerprinter().capture(target).digest
                assert (await session.get(WorkspaceSlot, request.source_slot_id)).lifecycle == "retained"
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_exit_before_apply_reserves_slots_and_replays_saved_patch(isolated_postgres_database, tmp_path):
    class SavedPatchOnly(GitWorkspaceResultApplier):
        async def prepare(self, source, target):
            pytest.fail("恢复不能重新生成补丁")

    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            request, source, target = await _seed(sessions, tmp_path)
            await _crash(request, target, "before_apply")
            adopter = WorkspaceAdopter(sessions)
            results = await adopter.recover_pending()
            assert any(row.adoption_id == request.adoption_id and row.status == "adopting" for row in results)
            assert (target / "tracked.txt").read_text(encoding="utf-8") == "baseline\n"
            for slot_id, root in ((request.source_slot_id, source), (request.target_slot_id, target)):
                with pytest.raises(WorkspaceLeaseConflict, match="采用意图"):
                    await WorkspaceLeaseManager(sessions).acquire(WorkspaceLeaseRequest(
                        slot_id=slot_id, run_id=uuid.uuid4().hex, mode="write"))
                with pytest.raises(WorkspaceRevisionConflict, match="采用意图"):
                    await WorkspaceFingerprinter(sessions).compare_and_advance(
                        slot_id, 1, WorkspaceFingerprinter().capture(root))
            assert not await WorkspaceSlotLifecycle(sessions).cleanup(request.source_slot_id, "test", force=True)
            # Changed source files must not alter the already-persisted patch identity.
            (source / "tracked.txt").write_text("later source edit\n", encoding="utf-8")
            result = await adopter.adopt(request, SavedPatchOnly())
            assert result.status == "adopted", result.conflict
            assert (target / "tracked.txt").read_text(encoding="utf-8") == "adopted result\n"
            assert result.resulting_target_revision == 2
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("mutation", ("external_edit", "partial_apply", "bad_patch", "stale_revision"))
def test_recovery_preserves_unknown_state_as_conflict(isolated_postgres_database, tmp_path, mutation):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            request, source, target = await _seed(sessions, tmp_path)
            await _crash(request, target, "before_apply" if mutation == "partial_apply" else "after_apply")
            if mutation == "external_edit":
                (target / "tracked.txt").write_text("user edit\n", encoding="utf-8")
            elif mutation == "partial_apply":
                (target / "tracked.txt").write_text("adopted result\n", encoding="utf-8")
            else:
                async with sessions.begin() as session:
                    if mutation == "bad_patch":
                        row = await session.get(WorkspaceAdoption, request.adoption_id)
                        row.application_intent = {**row.application_intent, "patch": "invalid"}
                    else:
                        (await session.get(WorkspaceSlot, request.target_slot_id)).revision += 1
            before = WorkspaceFingerprinter().capture(target).digest
            result = await WorkspaceAdopter(sessions).recover(request.adoption_id)
            assert result.status == "conflict"
            assert result.conflict["kind"] == "execution_uncertain"
            assert result.resulting_target_revision is None
            assert WorkspaceFingerprinter().capture(target).digest == before
            with pytest.raises(WorkspaceLeaseConflict, match="采用意图"):
                await WorkspaceLeaseManager(sessions).acquire(WorkspaceLeaseRequest(
                    slot_id=request.target_slot_id, run_id=uuid.uuid4().hex, mode="write"))
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_concurrent_recovery_and_reused_identity(isolated_postgres_database, tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            request, source, target = await _seed(sessions, tmp_path)
            await _crash(request, target)
            adopter = WorkspaceAdopter(sessions)
            results = await asyncio.gather(*(adopter.adopt(request, GitWorkspaceResultApplier()) for _ in range(3)))
            assert all(row.status == "adopted" and row.resulting_target_revision == 2 for row in results)
            with pytest.raises(WorkspaceAdoptionConflict, match="不同采用请求"):
                await adopter.adopt(request.model_copy(update={"source_revision": 2}), GitWorkspaceResultApplier())
            async with sessions() as session:
                assert (await session.get(WorkspaceSlot, request.target_slot_id)).revision == 2
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_exit_after_database_commit_is_idempotent(isolated_postgres_database, tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            request, source, target = await _seed(sessions, tmp_path)
            await _crash(request, target, "after_commit")
            result = await WorkspaceAdopter(sessions).adopt(request, GitWorkspaceResultApplier())
            assert result.status == "adopted", result.conflict
            assert result.resulting_target_revision == 2
            async with sessions() as session:
                assert (await session.get(WorkspaceSlot, request.target_slot_id)).revision == 2
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("variant", ("dirty_target", "no_patch", "autocrlf", "hidden_untracked"))
def test_predicted_fingerprint_matches_real_git_state(isolated_postgres_database, tmp_path, variant):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            request, source, target = await _seed(sessions, tmp_path)
            if variant == "dirty_target":
                (target / "user.txt").write_text("staged user work\n", encoding="utf-8")
                _git(target, "add", "user.txt")
                (target / ".git" / "info" / "exclude").write_text("ignored.log\n", encoding="utf-8")
                (target / "ignored.log").write_bytes(b"preserve ignored file")
                _git(source, "mv", "tracked.txt", "renamed.txt")
            elif variant == "no_patch":
                (source / "tracked.txt").write_text("baseline\n", encoding="utf-8")
                (source / "new.bin").unlink()
            elif variant == "autocrlf":
                _git(target, "config", "core.autocrlf", "true")
            else:
                _git(target, "config", "status.showUntrackedFiles", "no")
                (source / "tracked.txt").write_text("baseline\n", encoding="utf-8")
            index_before = _git(target, "diff", "--cached", "--binary")
            async with sessions.begin() as session:
                (await session.get(WorkspaceSlot, request.source_slot_id)).current_fingerprint = WorkspaceFingerprinter().capture(source).digest
                (await session.get(WorkspaceSlot, request.target_slot_id)).current_fingerprint = WorkspaceFingerprinter().capture(target).digest
            result = await WorkspaceAdopter(sessions).adopt(request, GitWorkspaceResultApplier())
            assert result.status == "adopted", result.conflict
            assert result.application_intent["after_fingerprint"] == WorkspaceFingerprinter().capture(target).digest
            assert _git(target, "diff", "--cached", "--binary") == index_before
            if variant == "dirty_target":
                assert (target / "ignored.log").read_bytes() == b"preserve ignored file"
                assert (target / "user.txt").read_text(encoding="utf-8") == "staged user work\n"
                assert not (target / "tracked.txt").exists()
                assert (target / "renamed.txt").read_text(encoding="utf-8") == "adopted result\n"
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_intent_migration_preserves_existing_adoption(isolated_migration_database, tmp_path):
    from alembic import command
    from alembic.config import Config

    async def seed():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            request, source, target = await _seed(sessions, tmp_path)
            result = await WorkspaceAdopter(sessions).adopt(request, GitWorkspaceResultApplier())
            assert result.status == "adopted"
            return request.adoption_id
        finally:
            await engine.dispose()

    identity = asyncio.run(seed())
    config = Config(str(Path(__file__).parents[1] / "packages/harness/focus/persistence/migrations/alembic.ini"))
    command.downgrade(config, "f36d7e8f901a")
    command.upgrade(config, "head")

    async def verify():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        try:
            async with async_sessionmaker(engine)() as session:
                row = await session.get(WorkspaceAdoption, identity)
                assert row.status == "adopted"
                assert row.resulting_target_revision == 2
                assert row.application_intent == {}
        finally:
            await engine.dispose()
    asyncio.run(verify())


def test_late_run_settlement_cannot_consume_unregistered_adoption(isolated_postgres_database, tmp_path):
    from backend.app.desktop.run_orchestration.lifecycle import RunLifecycleFinalizer

    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            request, source, target = await _seed(sessions, tmp_path)
            run_id, task_id = uuid.uuid4().hex, uuid.uuid4().hex
            async with sessions.begin() as session:
                slot = await session.get(WorkspaceSlot, request.target_slot_id)
                before = slot.current_fingerprint
                session.add(DesktopThread(task_id=task_id, workspace_id=slot.workspace_id,
                    thread_id=uuid.uuid4().hex, title="late settlement"))
                await session.flush()
                session.add(DesktopRun(run_id=run_id, task_id=task_id, agent_id="late-writer",
                    kind="main", status="success", origin="loop", execution_thread_id=uuid.uuid4().hex,
                    workspace_anchor={"slot_id": slot.slot_id, "mode": "write"}))
                await session.flush()
                session.add(RunExecutionAnchor(run_id=run_id, slot_id=slot.slot_id,
                    observed_workspace_revision=1, observed_fingerprint=before))
            await _crash(request, target)
            async with sessions.begin() as session:
                run_row = await session.get(DesktopRun, run_id)
                result = await RunLifecycleFinalizer._settle_workspace(
                    session, run_row, None, WorkspaceFingerprinter().capture(target))
                assert result["workspace_status"] == "adoption_recovery_pending"
                slot = await session.get(WorkspaceSlot, request.target_slot_id)
                assert slot.revision == 1 and slot.current_fingerprint == before
                assert (await session.get(RunExecutionAnchor, run_id)).resulting_workspace_revision is None
            result = await WorkspaceAdopter(sessions).recover(request.adoption_id)
            assert result.status == "adopted", result.conflict
            assert result.resulting_target_revision == 2
        finally:
            await engine.dispose()
    asyncio.run(run())
