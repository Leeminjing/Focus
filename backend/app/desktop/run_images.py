"""本文件对外提供 RunImageResolver，负责把本轮材料 ID 解析为权威 RunImageInputs。

输入为数据库 session、任务 ID、工作区路径、attached IDs、required IDs 和资源限制；输出为
按用户顺序去重且预算完整的 RunImageInputs。具体工作流为先校验 required 是 attached 子集，
再仅查询当前任务材料，逐项确认文件存在、非空且真实内容可解码为受支持图片，生成送模副本
的字节/token 估算，最后校验单轮数量与聚合字节边界。

示例：inputs = await resolver.resolve(session, task_id, workspace, attached, required)。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.material_files import resolve_material_path
from backend.app.desktop.material_upload import inspect_image_file
from backend.app.desktop.models import DesktopMaterial
from backend.app.desktop.resource_limits import ImageResourceLimits
from focus.agents.image_inputs import RunImageInput, RunImageInputs
from focus.images import image_dimensions, measure_image_tokens, scale_for_model


class RunImageResolver:
    def __init__(self, limits: ImageResourceLimits | None = None) -> None:
        self._limits = limits or ImageResourceLimits()

    async def resolve(
        self,
        session: AsyncSession,
        task_id: str,
        workspace_path: str,
        attached_ids: list[str],
        required_ids: list[str],
    ) -> RunImageInputs:
        attached = list(dict.fromkeys(attached_ids))
        required = list(dict.fromkeys(required_ids))
        detached = [material_id for material_id in required if material_id not in attached]
        if detached:
            raise HTTPException(422, f"必须看图片必须先附加: {', '.join(detached)}")
        if len(attached) > self._limits.run_image_count:
            raise HTTPException(413, f"本轮最多附加 {self._limits.run_image_count} 张图片")
        if not attached:
            return RunImageInputs.empty()
        rows = (
            await session.scalars(
                select(DesktopMaterial).where(
                    DesktopMaterial.task_id == task_id,
                    DesktopMaterial.material_id.in_(attached),
                )
            )
        ).all()
        by_id = {item.material_id: item for item in rows}
        resolved = [
            self._resolve_one(material_id, by_id.get(material_id), workspace_path)
            for material_id in attached
        ]
        inputs = RunImageInputs.build(resolved, required)
        if inputs.total_model_bytes > self._limits.run_model_bytes:
            raise HTTPException(
                413, f"本轮图片送模总量超过上限 {self._limits.run_model_bytes} 字节"
            )
        return inputs

    def _resolve_one(
        self, material_id: str, material: DesktopMaterial | None, workspace_path: str
    ) -> RunImageInput:
        if material is None:
            raise HTTPException(422, f"图片附件不存在或不属于当前任务: {material_id}")
        try:
            path = resolve_material_path(workspace_path, material.relative_path)
        except ValueError as error:
            raise HTTPException(422, f"图片附件路径无效: {material.relative_path}") from error
        if not path.is_file() or path.stat().st_size == 0:
            raise HTTPException(422, f"图片附件内容为空或文件不存在: {material.relative_path}")
        source_bytes = path.stat().st_size
        if source_bytes > self._limits.original_image_bytes:
            raise HTTPException(
                413, f"图片超过原图体积上限 {self._limits.original_image_bytes} 字节"
            )
        image = inspect_image_file(path, self._limits.image_pixels)
        if image is None:
            raise HTTPException(422, f"材料不是有效图片: {material.relative_path}")
        with path.open("rb") as source:
            original = source.read(source_bytes + 1)
        if len(original) != source_bytes:
            raise HTTPException(409, f"图片附件在校验期间发生变化: {material.relative_path}")
        current_dimensions = image_dimensions(original)
        if current_dimensions != (image.width, image.height):
            raise HTTPException(409, f"图片附件在校验期间发生变化: {material.relative_path}")
        _mime, scaled = scale_for_model(original)
        dimensions = image_dimensions(scaled)
        if dimensions is None:
            raise HTTPException(422, f"图片送模副本无法解码: {material.relative_path}")
        return RunImageInput(
            material_id=material.material_id,
            relative_path=material.relative_path,
            source_bytes=source_bytes,
            model_bytes=len(scaled),
            model_tokens=measure_image_tokens(*dimensions),
        )
