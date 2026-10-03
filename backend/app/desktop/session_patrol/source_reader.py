"""本文件对外提供 PatrolSourceReader 的精确来源读取端口 read。

输入为调用方独占的 AsyncSession 和 Context Revision、Run/checkpoint、文件或材料版本引用；
输出为完整 canonical records 与服务端确认的来源身份，不修改草稿或创建冻结引用。
具体工作流为按来源类型校验归属和精确身份，读取历史/工作区内容，再返回可投影记录；不存在时不追随 latest。
示例：records, source = await reader.read(session, {"kind": "context", "revision_id": "r1"})。
"""
from copy import deepcopy
import asyncio
import base64
import mimetypes
import subprocess

from fastapi import HTTPException
from backend.app.desktop.models import DesktopThread, DesktopWorkspace, DesktopRun, PatrolAgent, DesktopMaterial, MaterialVersion
from backend.app.desktop.context_evolution.repository import ContextRevisionRepository, ContextRevisionRepositoryError
from backend.app.desktop.context_evolution.reader import ContextRevisionReader
from backend.app.desktop.material_files import resolve_material_path
from focus.history import serialize_history_message


class PatrolSourceReader:
    def __init__(self, host):
        self._host = host
        self._repository = ContextRevisionRepository()
        self._reader = ContextRevisionReader(self._repository, host.checkpointer)

    async def read(self, session, request):
        readers = {"context": self._context, "patrol": self._patrol, "file": self._file, "material": self._file}
        reader = readers.get(request.get("kind"))
        if reader is None:
            raise HTTPException(422, {"code": "unknown_source", "message": "请选择来源类型"})
        try:
            return await reader(session, request)
        except ContextRevisionRepositoryError as exc:
            raise HTTPException(404, {"code": "source_unavailable", "message": "精确来源版本不存在或不可读取"}) from exc
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(422, {"code": "source_unavailable", "message": str(exc)}) from exc

    async def _context(self, session, request):
        revision = await self._repository.get_by_id(session, request["revision_id"])
        task = await session.get(DesktopThread, revision.ref.context_id)
        if task is None or task.deleted_at is not None:
            raise HTTPException(404, "来源 Context 不存在")
        if request.get("context_id", revision.ref.context_id) != revision.ref.context_id:
            raise HTTPException(409, "来源 Context 身份不匹配")
        view = await self._reader.read(session, revision.ref, "execution")
        source = {"kind": "context", "title": task.title, **revision.ref.model_dump(mode="json")}
        records = ([item.model_dump(mode="json") for item in revision.history_payload.execution_items]
                   if revision.history_payload is not None else list(view.messages))
        return records, source

    async def _patrol(self, session, request):
        run = await session.get(DesktopRun, request["run_id"])
        if run is None or run.kind != "patrol":
            raise HTTPException(404, "来源小兵不存在")
        checkpoint_id = request.get("checkpoint_id") or run.final_checkpoint_id
        if not checkpoint_id:
            raise HTTPException(409, "请选择已冻结 checkpoint")
        task = await session.get(DesktopThread, run.task_id)
        agent = await session.get(PatrolAgent, run.agent_id)
        if task is None or task.deleted_at is not None:
            raise HTTPException(404, "来源 Context 不存在")
        config = {"configurable": {"thread_id": run.execution_thread_id or task.thread_id,
                  "checkpoint_ns": run.checkpoint_ns or (agent.checkpoint_ns if agent else ""), "checkpoint_id": checkpoint_id}}
        checkpoint = await self._host.checkpointer.aget_tuple(config)
        if checkpoint is None or checkpoint.config["configurable"]["checkpoint_id"] != checkpoint_id:
            raise HTTPException(404, "精确 checkpoint 不存在")
        values = checkpoint.checkpoint["channel_values"]
        records = (deepcopy(values["execution_items"]) if values.get("execution_items") is not None
                   else [serialize_history_message(m) for m in values.get("messages", [])])
        return records, {"kind": "patrol", "run_id": run.run_id, **config["configurable"]}

    async def _file(self, session, request):
        task = await session.get(DesktopThread, request["context_id"])
        if task is None or task.deleted_at is not None:
            raise HTTPException(404, "来源工作区不存在")
        workspace = await session.get(DesktopWorkspace, task.workspace_id)
        if workspace is None:
            raise HTTPException(404, "来源工作区不存在")
        relative, material, version = request.get("path"), None, None
        if request["kind"] == "material":
            material = await session.get(DesktopMaterial, request["material_id"])
            if material is None or material.task_id != task.task_id:
                raise HTTPException(404, "材料不存在")
            relative = material.relative_path
            if request.get("version_id"):
                version = await session.get(MaterialVersion, request["version_id"])
                if version is None or version.material_id != material.material_id:
                    raise HTTPException(404, "材料版本不存在")
        path = resolve_material_path(workspace.path, relative)
        payload = await asyncio.to_thread(self._read_bytes, path, version, workspace.path)
        content = self._decode(payload, path)
        source = {"kind": request["kind"], "context_id": task.task_id, "workspace_id": workspace.workspace_id,
                  "material_id": material.material_id if material else None, "path": relative,
                  "version_id": version.version_id if version else None}
        return [{"role": "user", "content": content}], source

    @staticmethod
    def _read_bytes(path, version, workspace):
        try:
            if version:
                return subprocess.run(["git", "show", version.object_id], cwd=workspace, capture_output=True, check=True).stdout
            return path.read_bytes()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise HTTPException(422, {"code": "source_unavailable", "message": "来源内容不可读取，已有草稿正文仍可编辑"}) from exc

    @staticmethod
    def _decode(payload, path):
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            mime = mimetypes.guess_type(str(path))[0] or "image/png"
            return [{"type": "input_image", "image_url": "data:" + mime + ";base64," + base64.b64encode(payload).decode()}]
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(422, {"code": "unsupported_source_content", "message": "来源不是可直接导入的 UTF-8 文本或图片"}) from exc
