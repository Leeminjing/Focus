"""工作区材料查询、冻结附件读取和事务内删除；保留上传身份锁与 owner 校验。"""

import asyncio
import mimetypes
from pathlib import Path
from typing import Any, Protocol

from fastapi import HTTPException
from focus.agents.material_inputs import RunMaterialInput
from sqlalchemy import select

from backend.app.desktop.materials.files import resolve_material_path
from backend.app.desktop.materials.ownership import (
    ensure_material_mutable,
    load_material,
    lock_material_upload,
    material_upload_key,
)
from backend.app.desktop.materials.snapshots import MaterialSnapshotVerifier
from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopUserIntent
from backend.app.desktop.persistence.models import DesktopMaterial, DesktopWorkspace


async def _workspace(session, workspace_id):
    workspace = await session.get(DesktopWorkspace, workspace_id)
    if workspace is None or not Path(workspace.path).is_dir():
        raise HTTPException(404, "工作区不存在或目录不可用")
    return workspace


class WorkspaceMaterialPorts(Protocol):
    session_factory: Any

    def _workspace_payload(self, workspace: DesktopWorkspace) -> dict: ...

    def _material_payload(
        self, material: DesktopMaterial, workspace_path: str
    ) -> dict: ...


class WorkspaceMaterials:
    def __init__(self, ports: WorkspaceMaterialPorts) -> None:
        self._ports = ports

    async def workspace_details(self, workspace_id: str) -> dict:
        async with self._ports.session_factory() as session:
            workspace = await _workspace(session, workspace_id)
            return self._ports._workspace_payload(workspace)

    async def workspace_materials(self, workspace_id: str) -> list[dict]:
        async with self._ports.session_factory() as session:
            workspace = await _workspace(session, workspace_id)
            materials = await session.scalars(
                select(DesktopMaterial)
                .where(DesktopMaterial.workspace_id == workspace_id)
                .order_by(DesktopMaterial.created_at, DesktopMaterial.material_id)
            )
            return [
                self._ports._material_payload(material, workspace.path)
                for material in materials
            ]

    async def workspace_input_attachment(
        self, workspace_id: str, intent_id: str, material_id: str
    ) -> tuple[bytes, str, str]:
        """Read the accepted input's frozen version, even when the source file changes."""
        async with self._ports.session_factory() as session:
            workspace = await _workspace(session, workspace_id)
            intent = await session.scalar(
                select(LoopUserIntent)
                .join(AgentLoop, AgentLoop.loop_id == LoopUserIntent.loop_id)
                .where(
                    AgentLoop.workspace_id == workspace_id,
                    LoopUserIntent.intent_id == intent_id,
                    LoopUserIntent.intent_kind == "workspace_input",
                )
            )
            reference = next(
                (
                    item
                    for item in (
                        intent.request_payload.get("attachments", []) if intent else []
                    )
                    if item.get("material_id") == material_id
                ),
                None,
            )
            if reference is None:
                raise HTTPException(404, "输入附件不存在或不属于当前工作区")
            await load_material(session, material_id, workspace_id=workspace_id)
            item = RunMaterialInput.from_mapping(reference)
            try:
                raw = await asyncio.to_thread(
                    MaterialSnapshotVerifier().read, item, workspace.path
                )
            except (OSError, ValueError) as error:
                raise HTTPException(409, "附件的提交版本已不可用") from error
        filename = Path(item.relative_path).name
        media_type = (
            "text/plain"
            if item.material_kind == "text"
            else mimetypes.guess_type(filename)[0]
            if item.material_kind == "image"
            else "application/octet-stream"
        )
        return (raw, media_type or "application/octet-stream", filename)

    async def delete_workspace_material(
        self, workspace_id: str, material_id: str, submission_id: str | None = None
    ) -> dict:
        async with self._ports.session_factory.begin() as session:
            material, _ = await load_material(
                session, material_id, workspace_id=workspace_id
            )
            if submission_id is not None and (
                not material.intake_upload_key
                or material.intake_upload_key[:32]
                != material_upload_key(submission_id, "")[:32]
            ):
                raise HTTPException(409, "清理身份不属于这次上传")
            await lock_material_upload(session, material.intake_upload_key)
            material, workspace = await load_material(
                session, material_id, workspace_id=workspace_id, lock=True
            )
            await ensure_material_mutable(session, material)
            if material.retention == "irreplaceable":
                raise HTTPException(409, "不可遗失材料不能删除")
            try:
                path = resolve_material_path(workspace.path, material.relative_path)
            except ValueError as error:
                raise HTTPException(409, "材料路径无效") from error
            if path.is_file():
                path.unlink()
            await session.delete(material)
        return {"ok": True}
