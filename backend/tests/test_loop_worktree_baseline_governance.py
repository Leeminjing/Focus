"""本文件对外提供同轮 baseline 治理与动态配额串行回退的物理回归。

输入为隔离 PostgreSQL、真实临时 Git worktree、活动 Run 和配额释放；输出为共同 baseline 与
并行 Writer 隔离合同的断言。流程复用既有 FileWorker 和 Kernel 波次，基线漂移时保留待派发
任务且不消耗 attempt，配额恢复时仍完成当前串行回退。示例：pytest backend/tests/test_loop_worktree_baseline_governance.py。
"""

import asyncio
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop import ContextRunPool, LoopCoordinator
from backend.app.desktop.agent_loop import workspace_planning
from backend.app.desktop.agent_loop.dispatch import LoopWaveDispatcher
from backend.app.desktop.agent_loop.models import LoopDirective
from backend.app.desktop.workspace_coordination import WorkspaceFingerprinter, IsolationRequest
from backend.app.desktop.workspace_coordination.git_worktree import GitWorktreeIsolationProvider, WorkspaceIsolationCapacity
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot
from backend.tests.test_loop_runtime_pools import _create_wave
from backend.tests.test_loop_worktree_parallel_runs import _FileWorker
from backend.tests.test_git_workspace_adoption import _git

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_refill_preserves_active_sibling_commit_after_authority_advances(tmp_path, monkeypatch):
    async def run():
        task_home = tmp_path.parent / ("v" + uuid.uuid4().hex[:6])
        monkeypatch.setattr(workspace_planning, "global_home", lambda: task_home)
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, _, directives = await _create_wave(sessions, tmp_path, writing=True, git=True)
        worker = _FileWorker(sessions)
        pool = ContextRunPool(sessions, LoopCoordinator(sessions), LoopWaveDispatcher(sessions, worker), concurrency=2)
        try:
            await pool.drain(snapshot["loop_id"])
            await asyncio.gather(*(task for _, _, task in pool._tasks.values()))
            await asyncio.wait_for(asyncio.gather(worker.started[0].wait(), worker.started[1].wait()), 10)
            worker.release[1].set()
            await asyncio.wait_for(worker.done[1].wait(), 10)
            async with sessions() as session:
                authority = await session.scalar(select(WorkspaceSlot).where(
                    WorkspaceSlot.workspace_id == snapshot["workspace_id"], WorkspaceSlot.kind == "authoritative"))
            old_commit = _git(worker.roots[0], "rev-parse", "HEAD")
            root = Path(authority.root_path)
            (root / "changed-interface.txt").write_text("external new baseline", encoding="utf-8")
            _git(root, "add", "changed-interface.txt")
            _git(root, "commit", "-m", "advance authority while sibling runs")
            await WorkspaceFingerprinter(sessions).compare_and_advance(
                authority.slot_id, authority.revision, WorkspaceFingerprinter().capture(root))
            await pool.drain(snapshot["loop_id"])
            await asyncio.gather(*(task for _, _, task in pool._tasks.values()))
            assert not worker.done[0].is_set()
            assert len(worker.runs) == 2, "stale wave must not admit a refill from a different commit"
            assert _git(worker.roots[0], "rev-parse", "HEAD") == old_commit
            async with sessions() as session:
                waiting = await session.scalar(select(LoopDirective).where(
                    LoopDirective.directive_id.in_(directives), LoopDirective.status == "created"))
                assert waiting is not None and waiting.attempt == 0
                assert waiting.queued_reason == "workspace_baseline_changed"
        finally:
            for release in worker.release:
                release.set()
            await pool.close()
            await asyncio.gather(*worker.tasks, return_exceptions=True)
            await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()
    asyncio.run(run())


def test_quota_release_cannot_mix_authoritative_and_isolated_writers(tmp_path, monkeypatch):
    async def run():
        task_home = tmp_path.parent / ("v" + uuid.uuid4().hex[:6])
        monkeypatch.setattr(workspace_planning, "global_home", lambda: task_home)
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, _, _ = await _create_wave(sessions, tmp_path, count=2, writing=True, git=True)
        async with sessions() as session:
            authority = await session.scalar(select(WorkspaceSlot).where(
                WorkspaceSlot.workspace_id == snapshot["workspace_id"], WorkspaceSlot.kind == "authoritative"))
        managed = task_home / "loop-workspaces" / snapshot["workspace_id"]
        provider = GitWorktreeIsolationProvider(managed, quota=1)
        occupied = await provider.prepare(IsolationRequest(source_root=Path(authority.root_path),
            target_root=managed / "other-loop" / "completed-lane", baseline=_git(Path(authority.root_path), "rev-parse", "HEAD"),
            loop_id="other-loop", lane_id="completed-lane"))
        original = provider.prepare
        attempts = 0
        async def prepare(request):
            nonlocal attempts
            attempts += 1
            try:
                return await original(request)
            except WorkspaceIsolationCapacity:
                if attempts == 1:
                    await provider.remove(occupied)
                raise
        provider.prepare = prepare
        monkeypatch.setattr(workspace_planning, "GitWorktreeIsolationProvider", lambda root: provider)
        worker = _FileWorker(sessions)
        pool = ContextRunPool(sessions, LoopCoordinator(sessions), LoopWaveDispatcher(sessions, worker), concurrency=2)
        try:
            await pool.drain(snapshot["loop_id"])
            await asyncio.gather(*(task for _, _, task in pool._tasks.values()))
            assert len(worker.runs) == 1, "authoritative fallback must finish before another Writer starts"
            await asyncio.wait_for(worker.started[0].wait(), 10)
            assert worker.roots[0] == Path(authority.root_path)
            assert attempts == 1
            worker.release[0].set()
            await asyncio.wait_for(worker.done[0].wait(), 10)
            assert await pool.drain(snapshot["loop_id"]) == 1
            await asyncio.gather(*(task for _, _, task in pool._tasks.values()))
            await asyncio.wait_for(worker.started[1].wait(), 10)
            assert len(worker.runs) == 2
        finally:
            for release in worker.release:
                release.set()
            await pool.close()
            await asyncio.gather(*worker.tasks, return_exceptions=True)
            await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()
    asyncio.run(run())


def test_authority_edit_during_preparation_keeps_unused_worktrees_and_releases_claims(tmp_path, monkeypatch):
    async def run():
        task_home = tmp_path.parent / ("v" + uuid.uuid4().hex[:6])
        monkeypatch.setattr(workspace_planning, "global_home", lambda: task_home)
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, _, directives = await _create_wave(sessions, tmp_path, writing=True, git=True)
        original = GitWorktreeIsolationProvider.prepare
        prepared = []
        async def prepare(provider, request):
            result = await original(provider, request)
            prepared.append(result)
            (request.source_root / "tracked.txt").write_text("keep user edit", encoding="utf-8")
            return result
        monkeypatch.setattr(GitWorktreeIsolationProvider, "prepare", prepare)
        async def forbidden_launch(*_):
            pytest.fail("changed authority was delivered after worktree preparation")
        try:
            assert await LoopWaveDispatcher(sessions, forbidden_launch).dispatch(
                snapshot["loop_id"], snapshot["current_round_id"], 3) == ()
            async with sessions() as session:
                rows = (await session.scalars(select(LoopDirective).where(LoopDirective.directive_id.in_(directives)))).all()
                assert len(rows) == 3 and all(row.status == "created" and row.attempt == 0 for row in rows)
                assert {row.queued_reason for row in rows} == {"workspace_baseline_changed"}
                slots = (await session.scalars(select(WorkspaceSlot).where(WorkspaceSlot.owner_loop_id == snapshot["loop_id"]))).all()
                assert len(slots) == 3 and all(Path(slot.root_path).is_dir() for slot in slots)
            assert len(prepared) == 3
        finally:
            await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()
    asyncio.run(run())
