r"""本文件验证 Git 隔离 workspace 的多文件采用与冲突前置检查。

输入为临时 Git 权威工作区、共同 baseline、隔离 worktree 和持久 slot fingerprint；输出为完整 binary
patch 应用、新文件保留与目标漂移拒绝。具体工作流为真实创建 worktree、修改隔离结果、调用公开
GitWorkspaceResultApplier，再断言主工作区没有部分或遗漏结果。示例：`pytest test_git_workspace_adoption.py`。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import uuid

import pytest

from backend.app.desktop.workspace_coordination import (
    GitWorkspaceResultApplier,
    GitWorktreeIsolationProvider,
    IsolationRequest,
    WorkspaceAdoptionConflict,
    WorkspaceFingerprinter,
    WorkspaceSlot,
)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(root: Path) -> str:
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "focus-tests@example.invalid")
    _git(root, "config", "user.name", "Focus Tests")
    (root / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "baseline")
    return _git(root, "rev-parse", "HEAD")


def test_git_workspace_result_applies_tracked_and_untracked_files(tmp_path: Path) -> None:
    async def run() -> None:
        target = tmp_path / "authoritative"
        managed = tmp_path / "managed"
        source_path = managed / "loop" / "lane"
        baseline = _repository(target)
        provider = GitWorktreeIsolationProvider(managed)
        isolation = await provider.prepare(
            IsolationRequest(
                source_root=target,
                target_root=source_path,
                baseline=baseline,
                loop_id="loop",
                lane_id="lane",
            )
        )
        (source_path / "tracked.txt").write_text("isolated result\n", encoding="utf-8")
        (source_path / "new.txt").write_text("new result\n", encoding="utf-8")
        source_fingerprint = WorkspaceFingerprinter().capture(source_path)
        target_fingerprint = WorkspaceFingerprinter().capture(target)
        source = WorkspaceSlot(
            slot_id=uuid.uuid4().hex,
            workspace_id="workspace",
            kind="isolated",
            root_path=str(source_path),
            provider=isolation.provider,
            base_revision=baseline,
            current_fingerprint=source_fingerprint.digest,
            owner_loop_id="loop",
            owner_lane_id="lane",
        )
        destination = WorkspaceSlot(
            slot_id=uuid.uuid4().hex,
            workspace_id="workspace",
            kind="authoritative",
            root_path=str(target),
            provider="git",
            base_revision=baseline,
            current_fingerprint=target_fingerprint.digest,
        )
        resulting = await GitWorkspaceResultApplier()(source, destination)
        assert (target / "tracked.txt").read_text(encoding="utf-8") == "isolated result\n"
        assert (target / "new.txt").read_text(encoding="utf-8") == "new result\n"
        assert resulting == WorkspaceFingerprinter().capture(target).digest
        await provider.remove(isolation)
        assert not source_path.exists()

    asyncio.run(run())


def test_git_workspace_result_rejects_changed_authoritative_target(tmp_path: Path) -> None:
    async def run() -> None:
        target = tmp_path / "authoritative"
        managed = tmp_path / "managed"
        source_path = managed / "loop" / "lane"
        baseline = _repository(target)
        isolation = await GitWorktreeIsolationProvider(managed).prepare(
            IsolationRequest(
                source_root=target,
                target_root=source_path,
                baseline=baseline,
                loop_id="loop",
                lane_id="lane",
            )
        )
        (source_path / "tracked.txt").write_text("candidate\n", encoding="utf-8")
        source = WorkspaceSlot(
            slot_id=uuid.uuid4().hex,
            workspace_id="workspace",
            kind="isolated",
            root_path=str(source_path),
            provider=isolation.provider,
            base_revision=baseline,
            current_fingerprint=WorkspaceFingerprinter().capture(source_path).digest,
        )
        destination = WorkspaceSlot(
            slot_id=uuid.uuid4().hex,
            workspace_id="workspace",
            kind="authoritative",
            root_path=str(target),
            provider="git",
            base_revision=baseline,
            current_fingerprint=WorkspaceFingerprinter().capture(target).digest,
        )
        (target / "tracked.txt").write_text("user edit\n", encoding="utf-8")
        with pytest.raises(WorkspaceAdoptionConflict, match="权威 workspace fingerprint 已变化"):
            await GitWorkspaceResultApplier()(source, destination)
        assert (target / "tracked.txt").read_text(encoding="utf-8") == "user edit\n"

    asyncio.run(run())
