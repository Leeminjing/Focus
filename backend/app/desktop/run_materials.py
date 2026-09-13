"""本文件对外提供 RunMaterialRequest 与 RunMaterialResolver，建立权威逐轮材料聚合。

输入为数据库会话、任务/工作区、origin 身份、有序材料 ID/备注和独立必看图片 ID；输出为
不可变 RunMaterialInputs。具体工作流为统一规范化新旧请求、一次查询任务材料、校验路径、文件
存在性、摘要和备注预算，再仅把图片子集交给 RunImageResolver 做真实字节与送模预算验证，
最后按原顺序合并为唯一运行材料事实。

示例：inputs = await resolver.resolve(session, task, workspace, run_id, message_id, requests, required)。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.material_files import resolve_material_path
from backend.app.desktop.material_kinds import MaterialKindClassifier
from backend.app.desktop.models import DesktopMaterial
from backend.app.desktop.resource_limits import ImageResourceLimits
from backend.app.desktop.run_images import RunImageResolver
from focus.agents.material_inputs import RunMaterialInput, RunMaterialInputs


@dataclass(frozen=True, slots=True)
class RunMaterialRequest:
    material_id: str
    note: str = ""


class RunMaterialResolver:
    def __init__(self, limits: ImageResourceLimits | None = None) -> None:
        self._limits = limits or ImageResourceLimits()
        self._images = RunImageResolver(self._limits)

    async def resolve(
        self,
        session: AsyncSession,
        task_id: str,
        workspace_path: str,
        origin_run_id: str,
        origin_message_id: str,
        material_inputs: list[RunMaterialRequest] | None,
        legacy_material_ids: list[str] | None,
        required_image_ids: list[str],
    ) -> RunMaterialInputs:
        requests = self._normalize_requests(material_inputs, legacy_material_ids)
        self._validate_request_limits(requests)
        if not requests:
            if required_image_ids:
                raise HTTPException(422, "必须看图片必须先选择为本轮材料")
            return RunMaterialInputs.build(origin_run_id, origin_message_id, ())
        ids = [item.material_id for item in requests]
        rows = (
            await session.scalars(
                select(DesktopMaterial).where(
                    DesktopMaterial.task_id == task_id,
                    DesktopMaterial.material_id.in_(ids),
                )
            )
        ).all()
        by_id = {item.material_id: item for item in rows}
        snapshots = [
            self._resolve_snapshot(request, by_id.get(request.material_id), workspace_path)
            for request in requests
        ]
        image_ids = [item.material_id for item in snapshots if item.material_kind == "image"]
        detached = [value for value in required_image_ids if value not in image_ids]
        if detached:
            raise HTTPException(422, f"只有本轮已选有效图片可以标记为必须看: {', '.join(detached)}")
        images = await self._images.resolve(session, task_id, workspace_path, image_ids, required_image_ids)
        image_by_id = {item.material_id: item for item in images.attached}
        merged = [
            RunMaterialInput(
                **{
                    **item.to_json(),
                    "source_bytes": image_by_id[item.material_id].source_bytes,
                    "model_bytes": image_by_id[item.material_id].model_bytes,
                    "model_tokens": image_by_id[item.material_id].model_tokens,
                    "snapshot_object_id": image_by_id[item.material_id].snapshot_object_id,
                }
            )
            if item.material_id in image_by_id
            else item
            for item in snapshots
        ]
        return RunMaterialInputs.build(
            origin_run_id, origin_message_id, merged, required_image_ids
        )

    def _normalize_requests(
        self,
        material_inputs: list[RunMaterialRequest] | None,
        legacy_material_ids: list[str] | None,
    ) -> list[RunMaterialRequest]:
        if material_inputs is not None and legacy_material_ids is not None:
            raise HTTPException(422, "material_inputs 与 attached_material_ids 不能同时提交")
        requests = material_inputs or [RunMaterialRequest(value) for value in (legacy_material_ids or [])]
        ids = [item.material_id for item in requests]
        if len(ids) != len(set(ids)):
            raise HTTPException(422, "本轮材料不能重复")
        return requests

    def _validate_request_limits(self, requests: list[RunMaterialRequest]) -> None:
        if len(requests) > self._limits.run_material_count:
            raise HTTPException(413, f"本轮最多选择 {self._limits.run_material_count} 份材料")
        for item in requests:
            if len(item.note) > self._limits.material_note_chars:
                raise HTTPException(413, f"单份材料备注最多 {self._limits.material_note_chars} 个字符")
        if sum(len(item.note) for item in requests) > self._limits.aggregate_material_note_chars:
            raise HTTPException(413, f"本轮材料备注合计最多 {self._limits.aggregate_material_note_chars} 个字符")

    def _resolve_snapshot(
        self,
        request: RunMaterialRequest,
        material: DesktopMaterial | None,
        workspace_path: str,
    ) -> RunMaterialInput:
        if material is None:
            raise HTTPException(422, f"材料不存在或不属于当前任务: {request.material_id}")
        try:
            path = resolve_material_path(workspace_path, material.relative_path)
        except ValueError as error:
            raise HTTPException(422, f"材料路径无效: {material.relative_path}") from error
        if not path.is_file() or path.stat().st_size == 0:
            raise HTTPException(422, f"材料内容为空或文件不存在: {material.relative_path}")
        size = path.stat().st_size
        digest = self._digest(path)
        if path.stat().st_size != size:
            raise HTTPException(409, f"材料在解析期间发生变化: {material.material_id}")
        kind = MaterialKindClassifier.classify(material.relative_path)
        return RunMaterialInput(
            material_id=material.material_id,
            relative_path=material.relative_path,
            digest=digest,
            material_kind=kind,
            delivery_mode="image" if kind == "image" else "reference",
            reading_mode=material.reading_mode,
            instruction_mode=material.instruction_mode,
            size_bytes=size,
            note=request.note,
        )

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
