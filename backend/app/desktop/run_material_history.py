"""本文件对外提供 RunMaterialHistoryRepository，持久化并读取线程内逐轮材料历史。

输入为异步数据库会话、DesktopRun、RunMaterialInputs 和可选材料快照标识；输出为同事务待提交
的 binding 行或按线程顺序排列的历史载荷。具体工作流为批量写入有序不可变快照，查询时联接
运行状态但不依赖当前材料外键，因此材料删除后备注、路径、摘要和 message ID 仍然可追溯。

示例：repository.add(session, run, inputs); rows = await repository.list_for_task(session, task_id)。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.models import DesktopMaterial, DesktopRun, RunMaterialBinding
from focus.agents.material_inputs import RunMaterialInputs


class RunMaterialHistoryRepository:
    def add(
        self,
        session: AsyncSession,
        run: DesktopRun,
        inputs: RunMaterialInputs,
    ) -> list[RunMaterialBinding]:
        required = set(inputs.required_image_ids)
        rows = [
            RunMaterialBinding(
                binding_id=uuid.uuid4().hex,
                run_id=run.run_id,
                task_id=run.task_id,
                message_id=inputs.origin_message_id,
                material_id_snapshot=item.material_id,
                relative_path_snapshot=item.relative_path,
                digest_snapshot=item.digest,
                material_kind_snapshot=item.material_kind,
                note=item.note,
                ordinal=ordinal,
                must_view_requested=item.material_id in required,
            )
            for ordinal, item in enumerate(inputs.attached)
        ]
        session.add_all(rows)
        return rows

    async def list_for_task(
        self,
        session: AsyncSession,
        task_id: str,
        material_id: str | None = None,
    ) -> list[dict]:
        statement = (
            select(RunMaterialBinding, DesktopRun, DesktopMaterial.material_id)
            .join(DesktopRun, DesktopRun.run_id == RunMaterialBinding.run_id)
            .outerjoin(
                DesktopMaterial,
                (DesktopMaterial.task_id == RunMaterialBinding.task_id)
                & (DesktopMaterial.material_id == RunMaterialBinding.material_id_snapshot),
            )
            .where(RunMaterialBinding.task_id == task_id)
            .order_by(RunMaterialBinding.created_at, RunMaterialBinding.ordinal)
        )
        if material_id is not None:
            statement = statement.where(RunMaterialBinding.material_id_snapshot == material_id)
        rows = (await session.execute(statement)).all()
        return [self._payload(binding, run, current_id is not None) for binding, run, current_id in rows]

    @staticmethod
    def _payload(binding: RunMaterialBinding, run: DesktopRun, current_available: bool) -> dict:
        return {
            "binding_id": binding.binding_id,
            "run_id": binding.run_id,
            "task_id": binding.task_id,
            "message_id": binding.message_id,
            "material_id": binding.material_id_snapshot,
            "relative_path": binding.relative_path_snapshot,
            "digest": binding.digest_snapshot,
            "material_kind": binding.material_kind_snapshot,
            "note": binding.note,
            "ordinal": binding.ordinal,
            "must_view_requested": binding.must_view_requested,
            "created_at": binding.created_at.isoformat() if binding.created_at else None,
            "run_status": run.status,
            "current_available": current_available,
        }
