r"""本文件对外提供 WorkspaceAdopter、GitWorkspaceResultApplier、WorkspaceResultApplier 与冲突类型。

输入为隔离/权威 slot 版本和采用证据；输出为 adopted 或可审计 conflict 记录。
具体工作流为先独立提交包含补丁字节、SHA256、前后 fingerprint 的采用意图，再锁定两个 slot 执行真实 Git apply。
重试/启动恢复按目标 fingerprint 判定：后态只登记，前态应用持久补丁，其他状态保留冲突和 slot 预留。
示例：`result = await adopter.adopt(request, GitWorkspaceResultApplier())`。
"""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.workspaces.coordination.models import WorkspaceAdoption, WorkspaceLease, WorkspaceSlot
from backend.app.desktop.workspaces.coordination.reservations import reserved_adoption
from backend.app.desktop.workspaces.coordination.schemas import WorkspaceAdoptionRequest
from backend.app.desktop.workspaces.coordination.fingerprints import WorkspaceFingerprinter


class WorkspaceResultApplier(Protocol):
    async def prepare(self, source: WorkspaceSlot, target: WorkspaceSlot) -> dict: ...
    async def apply(self, target: WorkspaceSlot, intent: dict) -> str: ...


class WorkspaceAdoptionConflict(RuntimeError):
    def __init__(self, reason: str, *, kind: str = "execution_uncertain") -> None:
        super().__init__(reason)
        self.kind = kind


class GitWorkspaceResultApplier:
    async def __call__(self, source: WorkspaceSlot, target: WorkspaceSlot) -> str:
        return await self.apply(target, await self.prepare(source, target))

    async def prepare(self, source: WorkspaceSlot, target: WorkspaceSlot) -> dict:
        source_root = Path(source.root_path).resolve(strict=True)
        target_root = Path(target.root_path).resolve(strict=True)
        source_actual, target_actual = await asyncio.gather(
            asyncio.to_thread(WorkspaceFingerprinter().capture, source_root),
            asyncio.to_thread(WorkspaceFingerprinter().capture, target_root),
        )
        if source_actual.digest != source.current_fingerprint:
            raise WorkspaceAdoptionConflict("隔离 workspace fingerprint 已变化", kind="user_changes")
        if target_actual.digest != target.current_fingerprint:
            raise WorkspaceAdoptionConflict("权威 workspace fingerprint 已变化", kind="user_changes")
        if not source.base_revision:
            raise WorkspaceAdoptionConflict("隔离 workspace 缺少共同 Git baseline")
        patch = await self._patch(source_root, source.base_revision)
        if patch:
            # Only a patch-check overlap before a durable write intent is a repair candidate.
            # Eligibility still requires provenance and fresh fingerprints in the Loop port.
            await self._git(target_root, "apply", "--check", "--binary", "-", stdin=patch)
        # Simulate against a complete file snapshot, retaining ignored/untracked files and the
        # target's HEAD/index semantics. Git metadata stays in place and is never copied.
        with tempfile.TemporaryDirectory(prefix="focus-adoption-preview-") as directory:
            preview = Path(directory) / "tree"
            await asyncio.to_thread(shutil.copytree, target_root, preview, symlinks=True,
                ignore=lambda root, names: [".git"] if Path(root) == target_root else [])
            git_dir = (await self._git(target_root, "rev-parse", "--absolute-git-dir")).decode().strip()
            preview_git = ("--git-dir", git_dir, "--work-tree", str(preview))
            if patch:
                await self._git(preview, *preview_git, "apply", "--binary", "-", stdin=patch)
            revision, _ = WorkspaceFingerprinter.git_state(target_root)
            status = await self._git(preview, *preview_git, "status", "--porcelain",
                environment={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
            after = await asyncio.to_thread(WorkspaceFingerprinter().capture, preview, git_state=(revision, bool(status)))
        for root, expected in ((source_root, source.current_fingerprint), (target_root, target.current_fingerprint)):
            if (await asyncio.to_thread(WorkspaceFingerprinter().capture, root)).digest != expected:
                raise WorkspaceAdoptionConflict("准备采用意图时 workspace fingerprint 已变化")
        return {"version": 1, "patch": base64.b64encode(patch).decode("ascii"),
            "patch_sha256": hashlib.sha256(patch).hexdigest(),
            "source_root": str(source_root), "target_root": str(target_root),
            "source_fingerprint": source.current_fingerprint,
            "before_fingerprint": target.current_fingerprint, "after_fingerprint": after.digest}

    async def apply(self, target: WorkspaceSlot, intent: dict) -> str:
        target_root = Path(target.root_path).resolve(strict=True)
        patch = self.patch_bytes(intent)
        if (await asyncio.to_thread(WorkspaceFingerprinter().capture, target_root)).digest != intent["before_fingerprint"]:
            raise WorkspaceAdoptionConflict("权威 workspace fingerprint 已变化")
        if patch:
            await self._git(target_root, "apply", "--check", "--binary", "-", stdin=patch)
            await self._git(target_root, "apply", "--binary", "-", stdin=patch)
        captured = await asyncio.to_thread(WorkspaceFingerprinter().capture, target_root)
        if captured.digest != intent["after_fingerprint"]:
            raise WorkspaceAdoptionConflict("采用后 fingerprint 不符合持久意图，需恢复检查")
        return captured.digest

    @staticmethod
    def patch_bytes(intent: dict) -> bytes:
        try:
            patch = base64.b64decode(intent["patch"], validate=True)
            valid = intent["version"] == 1 and hashlib.sha256(patch).hexdigest() == intent["patch_sha256"]
            valid = valid and all(isinstance(intent[key], str) and intent[key] for key in (
                "source_root", "target_root", "source_fingerprint", "before_fingerprint", "after_fingerprint"))
        except (KeyError, ValueError, TypeError) as exc:
            raise WorkspaceAdoptionConflict("采用补丁身份无效") from exc
        if not valid:
            raise WorkspaceAdoptionConflict("采用补丁身份无效")
        return patch

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
            reason = process.stderr.decode(errors="replace").strip() or "Git workspace adoption 失败"
            check_overlap = (arguments[:3] == ("apply", "--check", "--binary") and process.returncode == 1
                and any(marker in reason for marker in ("patch does not apply", "already exists in working directory")))
            raise WorkspaceAdoptionConflict(
                reason, kind="code_overlap" if check_overlap else "execution_uncertain"
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
        # This transaction must commit before ANY writes to the authoritative tree.
        async with self._sessions.begin() as session:
            slots = await self._lock_slots(session, request.source_slot_id, request.target_slot_id)
            existing = await session.get(WorkspaceAdoption, request.adoption_id)
            if existing is not None:
                self._require_identity(existing, request)
                if existing.status != "adopting":
                    return existing
            else:
                duplicate = await session.scalar(select(WorkspaceAdoption.adoption_id).where(
                    WorkspaceAdoption.source_slot_id == request.source_slot_id,
                    WorkspaceAdoption.source_revision == request.source_revision))
                if duplicate is not None:
                    raise WorkspaceAdoptionConflict("该来源 revision 已绑定其他采用请求")
                if await reserved_adoption(session, *slots):
                    raise WorkspaceAdoptionConflict("workspace slot 存在未恢复的采用意图")
                source, target = slots[request.source_slot_id], slots[request.target_slot_id]
                adoption = WorkspaceAdoption(
                    adoption_id=request.adoption_id, source_slot_id=source.slot_id, target_slot_id=target.slot_id,
                    source_revision=request.source_revision, expected_target_revision=request.expected_target_revision,
                    evidence=request.evidence, status="adopting")
                session.add(adoption)
                try:
                    self._validate(source, target, request)
                    active = await session.scalar(select(WorkspaceLease.lease_id).where(
                        WorkspaceLease.slot_id.in_(slots), WorkspaceLease.status == "active",
                        WorkspaceLease.expires_at > datetime.now(UTC)).limit(1))
                    if active:
                        raise WorkspaceAdoptionConflict("采用 slot 存在活动 lease")
                    adoption.application_intent = await applier.prepare(source, target)
                except (WorkspaceAdoptionConflict, OSError) as exc:
                    self._conflict(adoption, str(exc), kind=getattr(exc, "kind", "execution_uncertain"))
                    return adoption
        return await self.recover(request.adoption_id, applier)

    async def recover(self, adoption_id: str, applier: WorkspaceResultApplier | None = None,
                      *, apply_if_before: bool = True, reject_if_before: bool = False) -> WorkspaceAdoption:
        applier = applier or GitWorkspaceResultApplier()
        async with self._sessions.begin() as session:
            probe = await session.get(WorkspaceAdoption, adoption_id)
            if probe is None:
                raise WorkspaceAdoptionConflict("采用记录不存在")
            slots = await self._lock_slots(session, probe.source_slot_id, probe.target_slot_id)
            adoption = await session.get(WorkspaceAdoption, adoption_id, populate_existing=True)
            if adoption.status != "adopting":
                return adoption
            source, target = slots[adoption.source_slot_id], slots[adoption.target_slot_id]
            intent = adoption.application_intent
            try:
                GitWorkspaceResultApplier.patch_bytes(intent)
                if (str(Path(source.root_path).resolve()) != intent["source_root"]
                    or str(Path(target.root_path).resolve()) != intent["target_root"]
                    or source.revision != adoption.source_revision
                    or target.revision != adoption.expected_target_revision
                    or source.current_fingerprint != intent["source_fingerprint"]
                    or target.current_fingerprint != intent["before_fingerprint"]):
                    raise WorkspaceAdoptionConflict("采用意图的 slot 身份或版本已变化")
                actual = (await asyncio.to_thread(WorkspaceFingerprinter().capture, Path(target.root_path))).digest
                if actual == intent["after_fingerprint"]:
                    # The filesystem effect already happened. Never apply it twice.
                    resulting = actual
                elif actual == intent["before_fingerprint"]:
                    if reject_if_before:
                        adoption.status = "rejected"
                        adoption.conflict = {"reason": "采用 owner 已失效，补丁尚未应用"}
                        adoption.completed_at = datetime.now(UTC)
                        return adoption
                    if not apply_if_before:
                        return adoption
                    resulting = await applier.apply(target, intent)
                else:
                    raise WorkspaceAdoptionConflict("采用恢复 fingerprint 既非前态也非后态，保留文件等待人工处理")
            except (WorkspaceAdoptionConflict, OSError) as exc:
                self._conflict(adoption, str(exc))
                return adoption
            target.revision += 1
            target.current_fingerprint = resulting
            source.lifecycle = "retained"
            adoption.status = "adopted"
            adoption.resulting_target_revision = target.revision
            adoption.completed_at = datetime.now(UTC)
            return adoption

    async def recover_pending(self) -> list[WorkspaceAdoption]:
        """启动时只收口已发生的效果；尚未写入的意图由授权 owner 决定是否继续。"""
        async with self._sessions() as session:
            ids = list((await session.scalars(select(WorkspaceAdoption.adoption_id).where(
                WorkspaceAdoption.status == "adopting"))).all())
        return [await self.recover(identity, apply_if_before=False) for identity in ids]

    @staticmethod
    def _conflict(adoption: WorkspaceAdoption, reason: str, *, kind: str = "execution_uncertain") -> None:
        adoption.status = "conflict"
        adoption.conflict = {"reason": reason, "kind": kind}
        adoption.completed_at = datetime.now(UTC)

    @staticmethod
    def _require_identity(adoption: WorkspaceAdoption, request: WorkspaceAdoptionRequest) -> None:
        if any(getattr(adoption, key) != getattr(request, key) for key in (
            "source_slot_id", "target_slot_id", "source_revision", "expected_target_revision")):
            raise WorkspaceAdoptionConflict("adoption_id 已绑定不同采用请求")

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
        if source.lifecycle not in ("active", "retained") or target.lifecycle != "active":
            raise WorkspaceAdoptionConflict("采用 slot 已不可用")
        if source.revision != request.source_revision:
            raise WorkspaceAdoptionConflict("隔离结果 revision 已变化", kind="user_changes")
        if target.revision != request.expected_target_revision:
            raise WorkspaceAdoptionConflict("权威 workspace revision 已变化", kind="target_version_changed")
