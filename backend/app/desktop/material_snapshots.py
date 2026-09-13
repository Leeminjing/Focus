"""本文件对外提供 MaterialSnapshotVerifier，核对活动运行的材料内容快照。

输入为数据库会话、不可变 RunMaterialInputs 和工作区路径；输出为内容未变化的原聚合，或为
变化材料补充受保护 Git blob 标识的新聚合。具体工作流为批量读取匹配摘要的 MaterialVersion，
逐项计算当前文件摘要；匹配则沿用当前路径，不匹配但有受保护版本则切换快照来源，否则在自动
投影前以包含材料身份的明确错误停止，绝不静默使用新内容。

示例：verified = await verifier.verify(session, inputs, workspace_path)。
"""

from dataclasses import replace
import hashlib
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.material_files import resolve_material_path
from backend.app.desktop.models import MaterialVersion
from focus.agents.material_inputs import RunMaterialInputs


class MaterialSnapshotVerifier:
    async def verify(
        self,
        session: AsyncSession,
        inputs: RunMaterialInputs,
        workspace_path: str,
    ) -> RunMaterialInputs:
        if not inputs.attached or all(item.digest == "legacy" for item in inputs.attached):
            return inputs
        ids = [item.material_id for item in inputs.attached]
        digests = [item.digest for item in inputs.attached]
        versions = (
            await session.scalars(
                select(MaterialVersion).where(
                    MaterialVersion.material_id.in_(ids),
                    MaterialVersion.digest.in_(digests),
                )
            )
        ).all()
        protected = {(item.material_id, item.digest): item.object_id for item in versions}
        resolved = []
        for item in inputs.attached:
            try:
                path = resolve_material_path(workspace_path, item.relative_path)
                current_digest = self._digest(path) if path.is_file() else None
            except (OSError, ValueError):
                current_digest = None
            if item.digest == "legacy" or current_digest == item.digest:
                resolved.append(item)
                continue
            object_id = protected.get((item.material_id, item.digest))
            if object_id:
                resolved.append(replace(item, snapshot_object_id=object_id))
                continue
            raise HTTPException(
                409,
                f"运行材料已变化且没有可用受保护快照: {item.relative_path} (material_id={item.material_id})",
            )
        return RunMaterialInputs.build(
            inputs.origin_run_id,
            inputs.origin_message_id,
            resolved,
            inputs.required_image_ids,
        )

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
