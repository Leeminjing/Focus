"""本文件提供材料归属读取、工作区提交锁及已引用版本的修改保护。

输入为数据库事务、材料身份和可选精确 owner；输出为材料/工作区或可诊断拒绝。
工作流按 task/workspace 排他归属校验，使用输入同源的提交锁协调上传与清理，行锁阻止受理和删除竞争；
已受理附件或实际运行引用不得被应用侧修改、删除。示例：material, workspace = await load_material(session, mid, workspace_id=wid, lock=True)。
"""

import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import func, or_, select

from backend.app.desktop.persistence.models import DesktopMaterial, DesktopThread, DesktopWorkspace, RunMaterialBinding
from backend.app.desktop.materials.files import resolve_material_path


def material_upload_key(submission_id: str, item_id: str) -> str:
    identity = uuid.uuid5(uuid.NAMESPACE_URL, f"focus:patrol-input:{submission_id}").hex
    return identity + ":" + hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:32]


async def lock_workspace_submission(session, submission_id: str) -> None:
    await lock_material_upload(session, material_upload_key(submission_id, ""))


async def lock_material_upload(session, upload_key: str | None) -> None:
    if upload_key:
        await session.execute(select(func.pg_advisory_xact_lock(int(upload_key[:15], 16))))


async def load_material(session, material_id: str, *, task_id=None, workspace_id=None, lock=False):
    query = select(DesktopMaterial).where(DesktopMaterial.material_id == material_id)
    if task_id is not None:
        query = query.where(DesktopMaterial.task_id == task_id)
    if workspace_id is not None:
        query = query.where(DesktopMaterial.workspace_id == workspace_id)
    material = await session.scalar(query.with_for_update().execution_options(populate_existing=True) if lock else query)
    if material is None:
        raise HTTPException(404, "材料不存在或不属于指定任务/工作区")
    owner_id = material.workspace_id
    if material.task_id is not None:
        task = await session.get(DesktopThread, material.task_id)
        owner_id = task.workspace_id if task else None
    workspace = await session.get(DesktopWorkspace, owner_id) if owner_id else None
    if workspace is None:
        raise HTTPException(404, "材料工作区不存在")
    try:
        resolve_material_path(workspace.path, material.relative_path)
    except ValueError as error:
        raise HTTPException(404, "材料路径超出工作区") from error
    return material, workspace


async def ensure_material_mutable(session, material) -> None:
    if material.workspace_id is None:
        return
    from backend.app.desktop.patrol.agent_loop.models import LoopUserIntent

    reference = [{"material_id": material.material_id}]
    intent = await session.scalar(select(LoopUserIntent.intent_id).where(or_(
        LoopUserIntent.request_payload["attachments"].contains(reference),
        LoopUserIntent.request_payload["request"]["attachments"].contains(reference),
    )).limit(1))
    run = await session.scalar(select(RunMaterialBinding.binding_id).where(
        RunMaterialBinding.material_id_snapshot == material.material_id,
    ).limit(1))
    if intent is not None or run is not None:
        raise HTTPException(409, "已受理输入或运行引用此材料版本，不能修改或删除")
