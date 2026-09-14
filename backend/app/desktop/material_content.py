"""本文件对外提供 MaterialContentService 与 ResolvedMaterialContent。

输入为任务 ID、材料 ID、数据库 session factory 和图片资源限制；输出为已校验归属且仍存在的
文件路径、可信媒体类型与下载文件名。具体工作流为同时查询 task/material 归属，解析工作区内
相对路径，拒绝跨任务或缺失文件；图片媒体类型取自实际文件格式，最终交给 FileResponse 传输。

示例：content = await service.resolve(task_id, material_id)。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.material_files import resolve_material_path
from backend.app.desktop.material_upload import inspect_image_file
from backend.app.desktop.models import DesktopMaterial, DesktopThread, DesktopWorkspace
from backend.app.desktop.resource_limits import ImageResourceLimits


@dataclass(frozen=True, slots=True)
class ResolvedMaterialContent:
    path: Path
    media_type: str
    filename: str


class MaterialContentService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        limits: ImageResourceLimits | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._limits = limits or ImageResourceLimits()

    async def resolve(self, task_id: str, material_id: str) -> ResolvedMaterialContent:
        async with self._session_factory() as session:
            task = await session.get(DesktopThread, task_id)
            if task is None:
                raise HTTPException(404, "任务不存在")
            material = await session.scalar(
                select(DesktopMaterial).where(
                    DesktopMaterial.material_id == material_id,
                    DesktopMaterial.task_id == task_id,
                )
            )
            if material is None:
                raise HTTPException(404, "材料不存在或不属于当前任务")
            workspace = await session.get(DesktopWorkspace, task.workspace_id)
            if workspace is None:
                raise HTTPException(404, "工作区不存在")
        try:
            path = resolve_material_path(workspace.path, material.relative_path)
        except ValueError as error:
            raise HTTPException(404, "材料路径无效") from error
        if not path.is_file():
            raise HTTPException(404, "材料文件不存在")
        media_type = "application/octet-stream"
        try:
            image = inspect_image_file(path, self._limits.image_pixels)
        except HTTPException:
            image = None
        if image is not None:
            media_type = image.mime
        return ResolvedMaterialContent(
            path=path,
            media_type=media_type,
            filename=Path(material.relative_path).name,
        )
