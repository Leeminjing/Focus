r"""本文件对外提供 WorkspaceFingerprinter 与 WorkspaceRevisionConflict。

输入为已验证的 workspace 根路径或持久 slot；输出为稳定内容 fingerprint、文件统计和可选 Git revision。
具体工作流为按相对路径排序散列文件类型、路径、大小与内容，忽略 Git 管理数据，并以 compare_and_advance
锁定 slot 后推进 revision。示例：`result = fingerprinter.capture(Path(root))`。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.workspace_coordination.models import WorkspaceSlot
from backend.app.desktop.workspace_coordination.schemas import WorkspaceFingerprint


class WorkspaceRevisionConflict(RuntimeError):
    pass


class WorkspaceFingerprinter:
    def __init__(self, sessions: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._sessions = sessions

    def capture(self, root: Path) -> WorkspaceFingerprint:
        resolved = root.resolve(strict=True)
        digest = hashlib.sha256()
        file_count = 0
        byte_count = 0
        for path in self._files(resolved):
            relative = path.relative_to(resolved).as_posix()
            size = path.stat().st_size
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(size).encode("ascii"))
            digest.update(b"\0")
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            file_count += 1
            byte_count += size
        vcs_revision, dirty = self._git_state(resolved)
        digest.update((vcs_revision or "").encode("ascii"))
        digest.update(b"1" if dirty else b"0")
        return WorkspaceFingerprint(
            digest=digest.hexdigest(),
            file_count=file_count,
            byte_count=byte_count,
            vcs_revision=vcs_revision,
            dirty=dirty,
        )

    async def compare_and_advance(
        self, slot_id: str, expected_revision: int, fingerprint: WorkspaceFingerprint
    ) -> int:
        if self._sessions is None:
            raise RuntimeError("compare_and_advance 需要 session factory")
        async with self._sessions.begin() as session:
            slot = await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.slot_id == slot_id).with_for_update())
            if slot is None or slot.revision != expected_revision:
                raise WorkspaceRevisionConflict("workspace revision 已变化")
            if slot.current_fingerprint == fingerprint.digest:
                return slot.revision
            slot.revision += 1
            slot.current_fingerprint = fingerprint.digest
            return slot.revision

    @staticmethod
    def _files(root: Path) -> list[Path]:
        return sorted(
            (path for path in root.rglob("*") if path.is_file() and ".git" not in path.relative_to(root).parts),
            key=lambda path: path.relative_to(root).as_posix(),
        )

    @staticmethod
    def _git_state(root: Path) -> tuple[str | None, bool]:
        try:
            revision = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            status = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
            return revision, bool(status.strip())
        except (OSError, subprocess.SubprocessError):
            return None, False
