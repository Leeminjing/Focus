"""本文件对外提供 SourceAvailabilityReader.inspect 的冻结来源只读状态查询。

输入为调用方 session 与服务器冻结的 source refs；输出为 opaque ref 到 available/unavailable/unknown 及可显示原因。
工作流按精确 Context/Run/checkpoint/材料版本定位原对象，文件只检查可读性、Git 只检查原 object 存在；重复来源复用状态。
查询不读取新正文、不修改冻结 record/hash、不提交事务；磁盘检查在工作线程执行，异常只返回无敏感载荷的原因。
示例：await SourceAvailabilityReader(host).inspect(session, draft.frozen_sources)，原文件删除后正文仍由原草稿提供。
"""
import asyncio
import subprocess
from backend.app.desktop.models import DesktopThread, DesktopWorkspace, DesktopRun, DesktopMaterial, MaterialVersion
from backend.app.desktop.context_evolution.models import ContextRevision
from backend.app.desktop.material_files import resolve_material_path
from focus.history import content_hash


class SourceAvailabilityReader:
    def __init__(self, host):
        self._host = host

    async def inspect(self, session, sources):
        result, cache = {}, {}
        for ref, value in sources.items():
            source = value.get("source", {})
            key = content_hash({k: v for k, v in source.items() if k not in {"message_id", "content_hash"}})
            if key not in cache:
                try:
                    reason = await self._unavailable_reason(session, source)
                    cache[key] = {"status": "unavailable" if reason else "available", "message": reason or "原来源可读取"}
                except (OSError, ValueError, subprocess.SubprocessError):
                    cache[key] = {"status": "unavailable", "message": "原来源无法读取"}
                if source.get("kind") not in {"context", "patrol", "file", "material"}:
                    cache[key] = {"status": "unknown", "message": "旧来源没有完整可检查身份"}
            result[ref] = dict(cache[key])
        return result

    async def _unavailable_reason(self, session, source):
        kind = source.get("kind")
        if kind == "patrol":
            run = await session.get(DesktopRun, source.get("run_id")) if source.get("run_id") else None
            if run is None:
                return "原运行不存在"
            return await self._checkpoint_reason(source)
        if kind not in {"context", "file", "material"}:
            return None
        task = await session.get(DesktopThread, source.get("context_id")) if source.get("context_id") else None
        if task is None or task.deleted_at is not None:
            return "原会话不可读取"
        if kind == "context":
            revision = await session.get(ContextRevision, source.get("revision_id")) if source.get("revision_id") else None
            if revision is None or revision.context_id != task.task_id:
                return "原历史版本不存在"
            return await self._checkpoint_reason(source) if source.get("checkpoint_id") else None
        workspace = await session.get(DesktopWorkspace, source.get("workspace_id") or task.workspace_id)
        if workspace is None:
            return "原工作区不存在"
        if kind == "material" and source.get("material_id"):
            material = await session.get(DesktopMaterial, source["material_id"])
            if material is None or material.task_id != task.task_id:
                return "原材料不存在"
        if kind == "material" and source.get("version_id"):
            version = await session.get(MaterialVersion, source["version_id"])
            if version is None or (source.get("material_id") and version.material_id != source["material_id"]):
                return "原材料版本不存在"
            return await asyncio.to_thread(self._git_reason, workspace.path, version.object_id)
        path = resolve_material_path(workspace.path, source.get("path", ""))
        return await asyncio.to_thread(self._file_reason, path)

    async def _checkpoint_reason(self, source):
        thread_id = source.get("execution_thread_id") or source.get("thread_id")
        if not thread_id or not source.get("checkpoint_id"):
            return "原 checkpoint 身份不完整"
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": source.get("checkpoint_ns", ""), "checkpoint_id": source["checkpoint_id"]}}
        checkpoint = await self._host.checkpointer.aget_tuple(config)
        if checkpoint is None or checkpoint.config["configurable"].get("checkpoint_id") != source["checkpoint_id"]:
            return "原 checkpoint 不存在"
        return None

    @staticmethod
    def _file_reason(path):
        if not path.is_file():
            return "原文件不存在"
        with path.open("rb"):
            return None

    @staticmethod
    def _git_reason(workspace_path, object_id):
        probe = subprocess.run(["git", "cat-file", "-e", object_id], cwd=workspace_path, capture_output=True, timeout=10)
        return None if probe.returncode == 0 else "原材料冻结对象不存在"
