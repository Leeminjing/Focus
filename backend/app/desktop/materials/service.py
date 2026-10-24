"""MaterialService 的业务实现与显式依赖端口。"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

from fastapi import HTTPException, UploadFile
from focus.images import IMAGE_MIME_BY_SUFFIX, is_image_name
from focus.models.telemetry import observe_operation
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.api.schemas import MaterialCreate, MaterialUpdate
from backend.app.desktop.materials.content import ResolvedMaterialContent
from backend.app.desktop.materials.events import MaterialChangeFeed, path_key
from backend.app.desktop.materials.files import resolve_material_path
from backend.app.desktop.materials.kinds import MaterialKindClassifier
from backend.app.desktop.materials.ownership import (
    ensure_material_mutable,
    load_material,
    lock_material_upload,
)
from backend.app.desktop.materials.policy import _material_policy_line
from backend.app.desktop.materials.snapshots import MaterialSnapshotVerifier
from backend.app.desktop.materials.watch import (
    MaterialFileMonitor,
    restore_material_file,
)
from backend.app.desktop.persistence.ids import new_id
from backend.app.desktop.persistence.models import (
    DesktopMaterial,
    DesktopThread,
    DesktopWorkspace,
    MaterialVersion,
    RunMaterialBinding,
)

logger = logging.getLogger(__name__)


class MaterialServicePorts(Protocol):
    async def _get_task_entities(
        self, session: AsyncSession, task_id: str
    ) -> tuple[DesktopThread, DesktopWorkspace]: ...

    _material_watch_failures: Any

    material_contents: Any
    material_groups: Any
    material_snapshots: Any
    material_uploads: Any
    run_images: Any
    run_material_history: Any
    session_factory: Any
    state_changes: Any


class MaterialService:
    def __init__(self, ports: MaterialServicePorts) -> None:
        self._ports = ports

    async def enroll_material(
        self, task_id: str, body: MaterialCreate
    ) -> dict[str, Any]:
        ports = self._ports
        async with ports.session_factory() as session:
            task, workspace = await ports._get_task_entities(session, task_id)
            path = self._resolve_workspace_path(workspace.path, body.path)
            if not path.is_file():
                raise HTTPException(422, "材料必须是工作区内已存在的文件")
            relative = path.relative_to(Path(workspace.path)).as_posix()
            existing = await session.scalar(
                select(DesktopMaterial).where(
                    DesktopMaterial.task_id == task_id,
                    DesktopMaterial.relative_path == relative,
                )
            )
            if existing:
                return self._material_payload(existing, workspace.path)
            if body.retention == "irreplaceable":
                await self._ensure_git(Path(workspace.path), body.confirm_git_init)
            material = DesktopMaterial(
                material_id=new_id(),
                task_id=task.task_id,
                relative_path=relative,
                reading_mode=body.reading_mode,
                instruction_mode=body.instruction_mode,
                retention=body.retention,
                digest=self._digest(path),
                git_ref=f"refs/focus/materials/{new_id()}",
            )
            session.add(material)
            await session.flush()
            if material.retention == "irreplaceable":
                await self._save_material_version(
                    session, material, workspace.path, "enroll"
                )
            await session.commit()
            return self._material_payload(material, workspace.path)

    async def list_materials(self, task_id: str) -> list[dict[str, Any]]:
        ports = self._ports
        async with ports.session_factory() as session:
            _, workspace = await ports._get_task_entities(session, task_id)
            materials = (
                await session.scalars(
                    select(DesktopMaterial)
                    .where(DesktopMaterial.task_id == task_id)
                    .order_by(DesktopMaterial.created_at)
                )
            ).all()
            payloads = [
                self._material_payload(item, workspace.path) for item in materials
            ]
        groups = await ports.material_groups.list(task_id)
        memberships = {
            membership["material_id"]: membership
            for group in groups
            for membership in group["memberships"]
        }
        history = await self.list_material_history(task_id)
        first_use: dict[str, dict[str, Any]] = {}
        for item in history:
            first_use.setdefault(item["material_id"], item)
        for payload in payloads:
            membership = memberships.get(payload["material_id"])
            source = first_use.get(payload["material_id"])
            payload["custom_group_id"] = membership["group_id"] if membership else None
            payload["custom_position"] = membership["position"] if membership else None
            payload["first_run_id"] = source["run_id"] if source else None
            payload["first_run_created_at"] = source["created_at"] if source else None
        return payloads

    async def list_material_history(
        self, task_id: str, material_id: str | None = None
    ) -> list[dict[str, Any]]:
        ports = self._ports
        async with ports.session_factory() as session:
            await ports._get_task_entities(session, task_id)
            if material_id is not None:
                material = await session.get(DesktopMaterial, material_id)
                if material is not None and material.task_id != task_id:
                    binding = await session.scalar(
                        select(RunMaterialBinding.binding_id)
                        .where(
                            RunMaterialBinding.task_id == task_id,
                            RunMaterialBinding.material_id_snapshot == material_id,
                        )
                        .limit(1)
                    )
                    task = await session.get(DesktopThread, task_id)
                    if not binding or material.workspace_id != task.workspace_id:
                        raise HTTPException(404, "材料不存在或不属于当前任务")
                historical_task_id = await session.scalar(
                    select(RunMaterialBinding.task_id)
                    .where(RunMaterialBinding.material_id_snapshot == material_id)
                    .limit(1)
                )
                if (
                    historical_task_id is not None
                    and historical_task_id != task_id
                    and (
                        not (material is not None and material.workspace_id is not None)
                    )
                ):
                    raise HTTPException(404, "材料历史不存在或不属于当前任务")
            return await ports.run_material_history.list_for_task(
                session, task_id, material_id
            )

    async def update_material(
        self, material_id: str, body: MaterialUpdate
    ) -> dict[str, Any]:
        ports = self._ports
        async with ports.session_factory() as session:
            material, workspace = await self._get_material_entities(
                session, material_id
            )
            await ensure_material_mutable(session, material)
            if (
                body.retention == "irreplaceable"
                and material.retention != "irreplaceable"
            ):
                await self._ensure_git(Path(workspace.path), body.confirm_git_init)
                material.retention = body.retention
                await self._save_material_version(
                    session, material, workspace.path, "protect"
                )
            material.reading_mode = body.reading_mode
            material.instruction_mode = body.instruction_mode
            material.retention = body.retention
            await session.commit()
            return self._material_payload(material, workspace.path)

    async def clear_material(self, material_id: str) -> dict[str, Any]:
        ports = self._ports
        async with ports.session_factory() as session:
            material, workspace = await self._get_material_entities(
                session, material_id
            )
            await ensure_material_mutable(session, material)
            if material.retention != "irreplaceable":
                raise HTTPException(409, "仅不可遗失材料使用置空操作")
            path = resolve_material_path(workspace.path, material.relative_path)
            path.write_bytes(b"")
            await self._save_material_version(
                session, material, workspace.path, "clear"
            )
            material.needs_confirmation = False
            await session.commit()
            return self._material_payload(material, workspace.path)

    async def delete_material(self, material_id: str) -> None:
        ports = self._ports
        async with ports.session_factory() as session:
            material, workspace = await self._get_material_entities(
                session, material_id
            )
            await ensure_material_mutable(session, material)
            if material.retention == "irreplaceable":
                raise HTTPException(409, "不可遗失材料不能删除，请置空或先解除保护")
            path = resolve_material_path(workspace.path, material.relative_path)
            if path.exists():
                path.unlink()
            await session.delete(material)
            await session.commit()

    async def material_versions(self, material_id: str) -> list[dict[str, Any]]:
        ports = self._ports
        async with ports.session_factory() as session:
            await load_material(session, material_id)
            versions = (
                await session.scalars(
                    select(MaterialVersion)
                    .where(MaterialVersion.material_id == material_id)
                    .order_by(MaterialVersion.created_at.desc())
                )
            ).all()
            return [self._version_payload(version) for version in versions]

    async def restore_material(
        self, material_id: str, version_id: str
    ) -> dict[str, Any]:
        ports = self._ports
        async with ports.session_factory() as session:
            material, workspace = await self._get_material_entities(
                session, material_id
            )
            await ensure_material_mutable(session, material)
            version = await session.get(MaterialVersion, version_id)
            if not version or version.material_id != material_id:
                raise HTTPException(404, "材料版本不存在")
            content = await self._git_bytes(
                Path(workspace.path), "cat-file", "blob", version.object_id
            )
            path = resolve_material_path(workspace.path, material.relative_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            await self._save_material_version(
                session, material, workspace.path, "restore"
            )
            material.needs_confirmation = False
            await session.commit()
            return self._material_payload(material, workspace.path)

    async def _material_context(self, task_id: str) -> tuple[str, str]:
        ports = self._ports
        async with ports.session_factory() as session:
            _, workspace = await ports._get_task_entities(session, task_id)
            materials = (
                await session.scalars(
                    select(DesktopMaterial)
                    .where(DesktopMaterial.task_id == task_id)
                    .order_by(DesktopMaterial.created_at)
                )
            ).all()
        lines = []
        upload_names: list[str] = []
        for material in materials:
            lines.append(
                _material_policy_line(
                    resolve_material_path(workspace.path, material.relative_path),
                    material.reading_mode,
                    material.instruction_mode,
                )
            )
            upload_names.append(material.relative_path)
        uploads_tag = (
            "<current_uploads>\n" + "\n".join(upload_names) + "\n</current_uploads>"
            if upload_names
            else ""
        )
        return ("\n".join(lines), uploads_tag)

    async def _validate_attachments(
        self, session: AsyncSession, task_id: str, messages: list[dict[str, Any]]
    ) -> None:
        ports = self._ports
        _, workspace = await ports._get_task_entities(session, task_id)
        for message in messages:
            for item in message.get("files", []):
                path = item.get("path") if isinstance(item, dict) else item
                if (
                    not path
                    or not self._resolve_workspace_path(workspace.path, path).exists()
                ):
                    raise HTTPException(422, f"附件引用失效: {path}")

    async def _get_material_entities(
        self, session: AsyncSession, material_id: str
    ) -> tuple[DesktopMaterial, DesktopWorkspace]:
        material, _ = await load_material(session, material_id)
        await lock_material_upload(session, material.intake_upload_key)
        return await load_material(session, material_id, lock=True)

    @staticmethod
    def _resolve_workspace_path(root: str | Path, value: str) -> Path:
        workspace = Path(root).resolve()
        candidate = Path(value).expanduser()
        target = (
            candidate.resolve()
            if candidate.is_absolute()
            else (workspace / candidate).resolve()
        )
        if target != workspace and workspace not in target.parents:
            raise HTTPException(403, "路径不属于当前工作区")
        return target

    @staticmethod
    def _digest(path: Path) -> str:
        return MaterialSnapshotVerifier._digest(path)

    async def _ensure_git(self, workspace: Path, confirmed: bool) -> None:
        if (workspace / ".git").exists():
            return
        if not confirmed:
            raise HTTPException(
                409, {"code": "git_init_required", "path": str(workspace)}
            )
        await self._git(workspace, "init")

    async def _save_material_version(
        self,
        session: AsyncSession,
        material: DesktopMaterial,
        workspace_path: str,
        source: str,
    ) -> MaterialVersion | None:
        ports = self._ports
        return await ports.material_snapshots.protect(
            session, material, workspace_path, source
        )

    async def _git(self, workspace: Path, *args: str, stdin: str | None = None) -> str:

        def run() -> str:
            env = os.environ.copy()
            env.update(
                {
                    "GIT_AUTHOR_NAME": "Focus",
                    "GIT_AUTHOR_EMAIL": "focus@localhost",
                    "GIT_COMMITTER_NAME": "Focus",
                    "GIT_COMMITTER_EMAIL": "focus@localhost",
                }
            )
            result = subprocess.run(
                ["git", "-C", str(workspace), *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=30,
                shell=False,
                env=env,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
            return result.stdout

        return await asyncio.to_thread(run)

    async def _git_bytes(self, workspace: Path, *args: str) -> bytes:

        def run() -> bytes:
            result = subprocess.run(
                ["git", "-C", str(workspace), *args],
                capture_output=True,
                timeout=30,
                shell=False,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.decode(errors="replace"))
            return result.stdout

        return await asyncio.to_thread(run)

    async def _watch_materials(self) -> None:
        ports = self._ports
        monitor = MaterialFileMonitor(self._digest)
        ports._material_watch_failures = set()
        feed = MaterialChangeFeed()
        signal = ports.state_changes.materials
        paths, retry_at = ({}, {})
        next_scan = 0
        scan_retry_at = 0
        generation = signal.generation
        try:
            while True:
                file_generation = feed.signal.generation
                dirty = feed.take()
                full = (
                    monotonic() >= next_scan
                    or generation != signal.generation
                    or dirty is None
                )
                generation = signal.generation
                now = monotonic()
                targets = (
                    None
                    if full
                    else {
                        identity
                        for identity, deadline in retry_at.items()
                        if deadline <= now
                    }
                    | {
                        identity
                        for identity, path in paths.items()
                        if identity not in retry_at
                        and any(
                            (
                                path_key(path) == item
                                or path_key(path).startswith(item + os.sep)
                                for item in dirty
                            )
                        )
                    }
                )
                if (full or targets) and monotonic() >= scan_retry_at:
                    try:
                        observed, pending = await self._scan_materials(monitor, targets)
                        if full:
                            retry_at.clear()
                        else:
                            for identity in targets:
                                retry_at.pop(identity, None)
                        retry_at.update(
                            {identity: monotonic() + 2 for identity in pending}
                        )
                        if full:
                            paths = observed
                            next_scan = monotonic() + 60
                            await feed.set_paths(paths.values())
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning("材料核对失败，稍后重试", exc_info=True)
                        retry_at.clear()
                        scan_retry_at = next_scan = monotonic() + 3
                timeout = max(0, min([next_scan, *retry_at.values()]) - monotonic())
                waits = [
                    asyncio.create_task(signal.wait(generation, timeout)),
                    asyncio.create_task(feed.signal.wait(file_generation, timeout)),
                ]
                try:
                    await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for waiter in waits:
                        waiter.cancel()
                    await asyncio.gather(*waits, return_exceptions=True)
        except asyncio.CancelledError:
            return
        finally:
            await feed.close()

    @observe_operation("material_watch")
    async def _scan_materials(self, monitor, material_ids=None):
        ports = self._ports
        failures = ports._material_watch_failures
        pending = set()
        observed = {}
        async with ports.session_factory() as session:
            query = (
                select(DesktopMaterial, DesktopThread, DesktopWorkspace)
                .join(DesktopThread, DesktopMaterial.task_id == DesktopThread.task_id)
                .join(
                    DesktopWorkspace,
                    DesktopThread.workspace_id == DesktopWorkspace.workspace_id,
                )
                .where(DesktopMaterial.retention == "irreplaceable")
            )
            if material_ids is not None:
                query = query.where(DesktopMaterial.material_id.in_(material_ids))
            rows = (await session.execute(query)).all()
            paths = [
                (
                    material,
                    workspace,
                    Path(workspace.path, *Path(material.relative_path).parts),
                )
                for material, _, workspace in rows
            ]
            observed = {material.material_id: path for material, _, path in paths}
            if material_ids is None:
                monitor.retain(
                    {(material.material_id, path) for material, _, path in paths}
                )
                failures.intersection_update(observed)
            changed = False
            for material, workspace, path in paths:
                try:
                    try:
                        digest = await asyncio.to_thread(
                            monitor.digest, material.material_id, path
                        )
                        if digest is None:
                            pending.add(material.material_id)
                    except FileNotFoundError:
                        latest = await session.scalar(
                            select(MaterialVersion)
                            .where(MaterialVersion.material_id == material.material_id)
                            .order_by(MaterialVersion.created_at.desc())
                        )
                        if latest:
                            content = await self._git_bytes(
                                Path(workspace.path),
                                "cat-file",
                                "blob",
                                latest.object_id,
                            )
                            material.digest = await asyncio.to_thread(
                                restore_material_file, path, content
                            )
                            material.needs_confirmation = True
                            changed = True
                    else:
                        if (
                            digest is not None
                            and material.digest
                            and (digest != material.digest)
                        ):
                            await self._save_material_version(
                                session, material, workspace.path, "external"
                            )
                            changed = True
                    failures.discard(material.material_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pending.add(material.material_id)
                    if material.material_id not in failures:
                        logger.warning(
                            "材料监测失败，跳过该材料: material_id=%s",
                            material.material_id,
                            exc_info=True,
                        )
                        failures.add(material.material_id)
                    continue
            if changed:
                await session.commit()
        monitor.metrics()
        return (observed, pending)

    @staticmethod
    def _material_payload(
        material: DesktopMaterial, workspace_path: str
    ) -> dict[str, Any]:
        path = resolve_material_path(workspace_path, material.relative_path)
        return {
            "material_id": material.material_id,
            "task_id": material.task_id,
            "workspace_id": material.workspace_id,
            "intake_upload_key": material.intake_upload_key,
            "name": path.name,
            "display_name": path.name,
            "media_type": IMAGE_MIME_BY_SUFFIX.get(
                path.suffix.lower(), "application/octet-stream"
            ),
            "path": str(path),
            "relative_path": material.relative_path,
            "reading_mode": material.reading_mode,
            "instruction_mode": material.instruction_mode,
            "retention": material.retention,
            "digest": material.digest,
            "sha256": material.digest,
            "git_ref": material.git_ref,
            "needs_confirmation": material.needs_confirmation,
            "is_image": is_image_name(material.relative_path),
            "material_kind": MaterialKindClassifier.classify(material.relative_path),
            "size_bytes": path.stat().st_size if path.is_file() else 0,
        }

    async def store_uploaded_material(
        self, task_id: str, upload: UploadFile
    ) -> dict[str, Any]:
        ports = self._ports
        stored = await ports.material_uploads.store(task_id, upload)
        return self._material_payload(stored.material, stored.workspace_path)

    async def resolve_material_content(
        self, task_id: str, material_id: str
    ) -> ResolvedMaterialContent:
        ports = self._ports
        return await ports.material_contents.resolve(task_id, material_id)

    async def validate_image_material(
        self, task_id: str, material_id: str
    ) -> dict[str, Any]:
        ports = self._ports
        async with ports.session_factory() as session:
            _, workspace = await ports._get_task_entities(session, task_id)
            inputs = await ports.run_images.resolve(
                session, task_id, workspace.path, [material_id], []
            )
        return inputs.attached[0].to_json()

    @staticmethod
    def _version_payload(version: MaterialVersion) -> dict[str, Any]:
        return {
            "version_id": version.version_id,
            "commit_id": version.commit_id,
            "object_id": version.object_id,
            "digest": version.digest,
            "source": version.source,
            "created_at": version.created_at.isoformat()
            if version.created_at
            else None,
        }
