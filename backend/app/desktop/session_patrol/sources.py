"""本文件对外提供 PatrolSourceResolver 的精确版本冻结与来源目录。

输入为 Context revision、Patrol Run/checkpoint 或工作区文件引用；输出为冻结正文、hash 和宿主保存的不透明 source refs。
工作流为 canonical Reader 读取精确 R/C，默认排除 runtime/opaque，文件经过工作区路径边界解析；保存时不追随来源更新。
不可读文件或不支持的编码返回可定位诊断，已冻结草稿正文保持不变。
文件/材料来源保存原工作区及材料版本身份，后续只读可用性检查不刷新正文。
示例：await resolver.import_into(draft_id, {"kind":"context", "revision_id":"r1"})。
"""
from copy import deepcopy
from fastapi import HTTPException
from sqlalchemy import select
from backend.app.desktop.models import DesktopThread, DesktopWorkspace, DesktopRun, PatrolDraft, PatrolAgent, DesktopMaterial, MaterialVersion
from backend.app.desktop.context_evolution.repository import ContextRevisionRepository
from backend.app.desktop.context_evolution.reader import ContextRevisionReader
from backend.app.desktop.material_files import resolve_material_path
from focus.history import serialize_history_message
from .source_projection import freeze_authoring_sources
from .repository import DraftRepository


class PatrolSourceResolver:
    def __init__(self, host):
        self._host = host
        self._repository = ContextRevisionRepository()
        self._reader = ContextRevisionReader(self._repository, host.checkpointer)

    async def catalog(self):
        from backend.app.desktop.context_evolution.models import ContextRevision
        async with self._host.session_factory() as session:
            tasks = list((await session.scalars(select(DesktopThread).where(DesktopThread.deleted_at.is_(None)))).all())
            revisions = list((await session.scalars(select(ContextRevision).order_by(ContextRevision.created_at.desc()))).all())
            runs = list((await session.scalars(select(DesktopRun).where(DesktopRun.kind == "patrol").order_by(DesktopRun.created_at.desc()))).all())
            return {"contexts": [{"context_id": t.task_id, "title": t.title,
                     "revisions": [{"revision_id": r.revision_id, "generation": r.generation, "checkpoint_id": r.checkpoint_id} for r in revisions if r.context_id == t.task_id]} for t in tasks],
                    "branches": [{"run_id": r.run_id, "agent_id": r.agent_id, "task_id": r.task_id, "thread_id": r.execution_thread_id, "checkpoint_ns": r.checkpoint_ns, "checkpoint_id": r.final_checkpoint_id, "status": r.status} for r in runs]}

    async def import_into(self, draft_id: str, request: dict):
        async with self._host.session_factory() as session:
            draft = await session.get(PatrolDraft, draft_id, with_for_update=True)
            if draft is None or draft.status != "editing":
                raise HTTPException(404, "草稿不存在")
            if request.get("draft_revision") != draft.draft_revision:
                raise HTTPException(409, {"code": "draft_revision_conflict"})
            records, source = await self._resolve(session, request)
            document = DraftRepository.document(draft)
            sources = deepcopy(draft.frozen_sources or {})
            entries, frozen = freeze_authoring_sources(records, source, selected=request.get("message_ids"),
                include_historical=bool(request.get("include_historical_runtime")))
            document.entries.extend(entries)
            sources.update(frozen)
            draft.authoring_document = document.model_dump(mode="json")
            draft.frozen_sources = sources
            draft.draft_revision += 1
            await session.commit()
            return self._host._draft_payload(draft)

    async def _resolve(self, session, request):
        kind = request.get("kind")
        if kind == "context":
            revision = await self._repository.get_by_id(session, request["revision_id"])
            if request.get("context_id", revision.ref.context_id) != revision.ref.context_id:
                raise HTTPException(409, "来源 Context 身份不匹配")
            view = await self._reader.read(session, revision.ref, "execution")
            task = await session.get(DesktopThread, revision.ref.context_id)
            source = {"kind": kind, "title": task.title if task else "Context", **revision.ref.model_dump(mode="json")}
            if revision.history_payload is not None:
                return [item.model_dump(mode="json") for item in revision.history_payload.execution_items], source
            return list(view.messages), source
        if kind == "patrol":
            run = await session.get(DesktopRun, request["run_id"])
            if run is None or run.kind != "patrol":
                raise HTTPException(404, "来源小兵不存在")
            checkpoint_id = request.get("checkpoint_id") or run.final_checkpoint_id
            if not checkpoint_id:
                raise HTTPException(409, "请选择已冻结 checkpoint")
            task = await session.get(DesktopThread, run.task_id)
            agent = await session.get(PatrolAgent, run.agent_id)
            config = {"configurable": {"thread_id": run.execution_thread_id or task.thread_id, "checkpoint_ns": run.checkpoint_ns or agent.checkpoint_ns, "checkpoint_id": checkpoint_id}}
            checkpoint = await self._host.checkpointer.aget_tuple(config)
            if checkpoint is None or checkpoint.config["configurable"]["checkpoint_id"] != checkpoint_id:
                raise HTTPException(404, "精确 checkpoint 不存在")
            values = checkpoint.checkpoint["channel_values"]
            records = deepcopy(values["execution_items"]) if values.get("execution_items") is not None else [serialize_history_message(m) for m in values.get("messages", [])]
            return records, {"kind": kind, "run_id": run.run_id, **config["configurable"]}
        if kind in {"file", "material"}:
            task = await session.get(DesktopThread, request["context_id"])
            if task is None:
                raise HTTPException(404, "来源工作区不存在")
            workspace = await session.get(DesktopWorkspace, task.workspace_id)
            relative = request.get("path")
            version = None
            if kind == "material":
                material = await session.get(DesktopMaterial, request["material_id"])
                if material is None or material.task_id != task.task_id:
                    raise HTTPException(404, "材料不存在")
                relative = material.relative_path
                if request.get("version_id"):
                    version = await session.get(MaterialVersion, request["version_id"])
                    if version is None or version.material_id != material.material_id:
                        raise HTTPException(404, "材料版本不存在")
            path = resolve_material_path(workspace.path, relative)
            if version:
                import subprocess
                try:
                    payload = subprocess.run(["git", "show", version.object_id], cwd=workspace.path, capture_output=True, check=True).stdout
                except subprocess.CalledProcessError as exc:
                    raise HTTPException(422, {"code": "source_unavailable", "message": "材料冻结版本不可读取，已有草稿正文仍可编辑"}) from exc
            else:
                try:
                    payload = path.read_bytes()
                except OSError as exc:
                    raise HTTPException(422, {"code": "source_unavailable", "message": "来源文件不可读取，已有草稿正文仍可编辑"}) from exc
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                import base64
                import mimetypes
                content = [{"type": "input_image", "image_url": "data:" + (mimetypes.guess_type(str(path))[0] or "image/png") + ";base64," + base64.b64encode(payload).decode()}]
            else:
                try:
                    content = payload.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise HTTPException(422, {"code": "unsupported_source_content", "message": "来源不是可直接导入的 UTF-8 文本或图片"}) from exc
            return [{"role": "user", "content": content}], {"kind": kind, "context_id": task.task_id, "workspace_id": workspace.workspace_id,
                "material_id": material.material_id if kind == "material" else None, "path": relative, "version_id": version.version_id if version else None}
        raise HTTPException(422, "未知来源类型")
