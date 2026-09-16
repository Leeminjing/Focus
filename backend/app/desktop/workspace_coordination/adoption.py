r"""本文件对外提供 WorkspaceAdopter、GitWorkspaceResultApplier、WorkspaceResultApplier 与冲突类型。

输入为隔离/权威 slot 版本、采用证据和原子 result applier；输出为 adopted 或 conflict 记录。
具体工作流为稳定锁定两个 slot、重验 source/target revision 与物理 fingerprint，以临时 Git index
把隔离结果构造成 binary patch，通过工作线程中的非 shell Git 先完整 check 后一次 apply，成功后推进目标 revision并封存来源；
任何校验或 patch 冲突都留下可审计 conflict。示例：`result = await adopter.adopt(request, applier)`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.workspace_coordination.models import WorkspaceAdoption, WorkspaceSlot
from backend.app.desktop.workspace_coordination.schemas import WorkspaceAdoptionRequest
from backend.app.desktop.workspace_coordination.fingerprints import WorkspaceFingerprinter


WorkspaceResultApplier = Callable[[WorkspaceSlot, WorkspaceSlot], Awaitable[str]]


class WorkspaceAdoptionConflict(RuntimeError):
    pass


class GitWorkspaceResultApplier:
    async def __call__(self, source: WorkspaceSlot, target: WorkspaceSlot) -> str:
        source_root = Path(source.root_path).resolve(strict=True)
        target_root = Path(target.root_path).resolve(strict=True)
        source_actual, target_actual = await asyncio.gather(
            asyncio.to_thread(WorkspaceFingerprinter().capture, source_root),
            asyncio.to_thread(WorkspaceFingerprinter().capture, target_root),
        )
        if source_actual.digest != source.current_fingerprint:
            raise WorkspaceAdoptionConflict("隔离 workspace fingerprint 已变化")
        if target_actual.digest != target.current_fingerprint:
            raise WorkspaceAdoptionConflict("权威 workspace fingerprint 已变化")
        if not source.base_revision:
            raise WorkspaceAdoptionConflict("隔离 workspace 缺少共同 Git baseline")
        patch = await self._patch(source_root, source.base_revision)
        if patch:
            await self._git(target_root, "apply", "--check", "--binary", "-", stdin=patch)
            await self._git(target_root, "apply", "--binary", "-", stdin=patch)
        captured = await asyncio.to_thread(WorkspaceFingerprinter().capture, target_root)
        return captured.digest

    async def _patch(self, root: Path, baseline: str) -> bytes:
        descriptor, index_name = tempfile.mkstemp(prefix="focus-adoption-index-")
        os.close(descriptor)
        Path(index_name).unlink(missing_ok=True)
        environment = {**os.environ, "GIT_INDEX_FILE": index_name}
        try:
            await self._git(root, "read-tree", baseline, environment=environment)
            await self._git(root, "add", "-A", "--", ".", environment=environment)
            tree = (await self._git(root, "write-tree", environment=environment)).decode().strip()
            return await self._git(
                root,
                "diff-tree",
                "-p",
                "--binary",
                "--full-index",
                baseline,
                tree,
                environment=environment,
            )
        finally:
            Path(index_name).unlink(missing_ok=True)

    @staticmethod
    async def _git(
        root: Path,
        *arguments: str,
        stdin: bytes | None = None,
        environment: dict[str, str] | None = None,
    ) -> bytes:
        process = await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", str(root), *arguments],
            input=stdin,
            capture_output=True,
            check=False,
            env=environment,
        )
        if process.returncode:
            raise WorkspaceAdoptionConflict(
                process.stderr.decode(errors="replace").strip() or "Git workspace adoption 失败"
            )
        return process.stdout


class WorkspaceAdopter:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def adopt(
        self,
        request: WorkspaceAdoptionRequest,
        applier: WorkspaceResultApplier,
    ) -> WorkspaceAdoption:
        async with self._sessions.begin() as session:
            existing = await session.get(WorkspaceAdoption, request.adoption_id)
            if existing is not None:
                return existing
            slots = await self._lock_slots(session, request.source_slot_id, request.target_slot_id)
            source = slots[request.source_slot_id]
            target = slots[request.target_slot_id]
            adoption = WorkspaceAdoption(
                adoption_id=request.adoption_id,
                source_slot_id=source.slot_id,
                target_slot_id=target.slot_id,
                source_revision=request.source_revision,
                expected_target_revision=request.expected_target_revision,
                evidence=request.evidence,
                status="adopting",
            )
            session.add(adoption)
            try:
                self._validate(source, target, request)
                resulting_fingerprint = await applier(source, target)
            except WorkspaceAdoptionConflict as exc:
                adoption.status = "conflict"
                adoption.conflict = {"reason": str(exc)}
                return adoption
            target.revision += 1
            target.current_fingerprint = resulting_fingerprint
            source.lifecycle = "retained"
            adoption.status = "adopted"
            adoption.resulting_target_revision = target.revision
            adoption.completed_at = datetime.now(UTC)
            return adoption

    @staticmethod
    async def _lock_slots(session: AsyncSession, *slot_ids: str) -> dict[str, WorkspaceSlot]:
        rows = list(
            (
                await session.scalars(
                    select(WorkspaceSlot)
                    .where(WorkspaceSlot.slot_id.in_(sorted(slot_ids)))
                    .order_by(WorkspaceSlot.slot_id)
                    .with_for_update()
                )
            ).all()
        )
        if len(rows) != len(set(slot_ids)):
            raise WorkspaceAdoptionConflict("workspace slot 不存在")
        return {row.slot_id: row for row in rows}

    @staticmethod
    def _validate(
        source: WorkspaceSlot,
        target: WorkspaceSlot,
        request: WorkspaceAdoptionRequest,
    ) -> None:
        if source.kind != "isolated" or target.kind != "authoritative":
            raise WorkspaceAdoptionConflict("只能从 isolated slot 采用到 authoritative slot")
        if source.workspace_id != target.workspace_id:
            raise WorkspaceAdoptionConflict("采用不能跨 workspace")
        if source.revision != request.source_revision:
            raise WorkspaceAdoptionConflict("隔离结果 revision 已变化")
        if target.revision != request.expected_target_revision:
            raise WorkspaceAdoptionConflict("权威 workspace revision 已变化")
