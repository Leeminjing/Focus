"""Resolve file links by workspace/slot/path/version; historical reads never use disk.

The desktop sends the complete reference to one read-only endpoint. Current reads
are scoped to registered workspace slots; historical reads use persisted Fact or
MaterialVersion content only. No filename search or root-directory fallback.
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path, PureWindowsPath
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from focus.security import (
    AccessDecision, AccessOperation, canonical_target, decide_path_access,
    is_within, restrictive_policy,
)

from backend.app.desktop.models import DesktopThread, DesktopWorkspace, DesktopRun, MaterialVersion
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot


class FileLinkReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    workspace_id: str | None = None
    run_id: str | None = None
    slot_id: str | None = None
    view: Literal["current", "history"] = "current"
    fact_id: str | None = None
    revision: int | None = Field(default=None, ge=1)
    result_key: str | None = None
    sha256: str | None = None
    version_id: str | None = None
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_range(self):
        if self.end_line is not None and self.start_line is None:
            raise ValueError("行范围缺少起始行")
        if self.start_line is not None:
            if self.end_line is None:
                self.end_line = self.start_line
            if self.end_line < self.start_line:
                raise ValueError("结束行不能早于起始行")
        return self


@dataclass(frozen=True)
class FileLinkContent:
    reference: dict
    data: bytes
    media_type: str


def resolve_link_path(root: str | Path, raw: str) -> Path:
    """Already URL-decoded path; never unquote it again or discard directories."""
    if "\0" in raw:
        raise HTTPException(422, "文件路径无效")
    raw = raw.replace("\\", "/")
    # A Windows drive/UNC identity must never become a relative POSIX filename.
    if PureWindowsPath(raw).drive and not Path(raw).is_absolute():
        raise HTTPException(422, "当前系统无法解析此 Windows 路径")
    root = Path(root).resolve()
    path = canonical_target(root, raw)
    if not is_within(root, path):
        raise HTTPException(404, "文件路径不属于指定工作区或工作树")
    if decide_path_access(restrictive_policy(root), path, AccessOperation.READ) is not AccessDecision.ALLOW:
        raise HTTPException(403, "当前不允许读取此文件")
    return path


class FileLinkReader:
    async def read(self, session, task_id: str, ref: FileLinkReference) -> FileLinkContent:
        task = await session.get(DesktopThread, task_id)
        workspace = await session.get(DesktopWorkspace, task.workspace_id) if task else None
        if workspace is None or (ref.workspace_id and ref.workspace_id != workspace.workspace_id):
            raise HTTPException(404, "文件工作区不存在或不匹配")
        identity = ref.model_dump(exclude_none=True)
        identity["workspace_id"] = workspace.workspace_id
        if ref.view == "history":
            return await self._history(session, task_id, workspace, ref, identity)
        if any((ref.fact_id, ref.revision, ref.result_key, ref.sha256, ref.version_id)):
            raise HTTPException(422, "版本引用必须使用历史版本入口")
        root, slot_id = await self._root(session, task_id, workspace, ref)
        path = resolve_link_path(root, ref.path)
        if not path.is_file():
            raise HTTPException(404, "指定路径的当前磁盘文件不存在")
        identity.update(path=str(path), slot_id=slot_id)
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise HTTPException(404, "指定路径的当前磁盘文件无法读取") from error
        identity["current_sha256"] = hashlib.sha256(raw).hexdigest()
        return self._content(identity, raw, path.suffix.lower())

    async def _root(self, session, task_id, workspace, ref):
        slot_id = ref.slot_id
        if ref.run_id:
            run = await session.get(DesktopRun, ref.run_id)
            if run is None or run.task_id != task_id:
                raise HTTPException(404, "文件运行来源不属于此任务")
            anchor = run.workspace_anchor or {}
            run_slot = anchor.get("slot_id")
            if slot_id and slot_id != run_slot:
                raise HTTPException(409, "文件工作树与运行来源不匹配")
            slot_id = run_slot
            if not slot_id and anchor.get("workspace_path"):
                if Path(anchor["workspace_path"]).resolve() != Path(workspace.path).resolve():
                    raise HTTPException(409, "运行工作树身份缺失，无法定位文件")
        if slot_id:
            slot = await session.get(WorkspaceSlot, slot_id)
            if slot is None or slot.workspace_id != workspace.workspace_id or slot.lifecycle == "deleted":
                raise HTTPException(404, "指定工作树不可用")
            return slot.root_path, slot.slot_id
        # Absolute links can identify a registered worktree without a run ID.
        if Path(ref.path.replace("\\", "/")).is_absolute():
            slots = (await session.scalars(select(WorkspaceSlot).where(
                WorkspaceSlot.workspace_id == workspace.workspace_id,
                WorkspaceSlot.lifecycle != "deleted",
            ))).all()
            candidates = [(workspace.path, None), *((slot.root_path, slot.slot_id) for slot in slots)]
            target = Path(ref.path.replace("\\", "/")).resolve()
            matches = [(root, sid) for root, sid in candidates if is_within(Path(root).resolve(), target)]
            if not matches:
                raise HTTPException(404, "文件路径不属于指定工作区或工作树")
            return max(matches, key=lambda item: len(Path(item[0]).resolve().parts))
        anchors = (await session.scalars(select(DesktopRun.workspace_anchor).where(
            DesktopRun.task_id == task_id,
        ))).all()
        for anchor in anchors:
            if not anchor or not anchor.get("slot_id"):
                continue
            slot = await session.get(WorkspaceSlot, anchor["slot_id"])
            if slot is None or Path(slot.root_path).resolve() != Path(workspace.path).resolve():
                raise HTTPException(409, "相对文件链接缺少运行或工作树身份，无法确定来源；请使用完整路径")
        return workspace.path, None

    async def _history(self, session, task_id, workspace, ref, identity):
        if ref.fact_id and ref.version_id:
            raise HTTPException(422, "文件链接不能同时指定两种历史版本来源")
        if ref.fact_id:
            from backend.app.desktop.agent_loop.fact_models import LoopFact
            from backend.app.desktop.agent_loop.models import AgentLoop
            from backend.app.desktop.agent_loop.live_access import LoopLiveAccessPolicy
            from backend.app.desktop.domain_evidence.file_reads import FileVersionReader

            if ref.revision is None or not ref.result_key or not ref.slot_id:
                raise HTTPException(409, "历史版本身份不完整，无法展示对应内容")
            fact = await session.get(LoopFact, ref.fact_id)
            loop = await session.get(AgentLoop, fact.loop_id) if fact else None
            if loop is None or loop.workspace_id != workspace.workspace_id:
                raise HTTPException(404, "历史文件不属于指定工作区")
            access = await LoopLiveAccessPolicy().resolve(session, loop.loop_id)
            if not access.permissions.intersection({"read", "view_evidence"}):
                raise HTTPException(403, "当前无权查看文件历史版本")
            version = await FileVersionReader().read(session, loop.loop_id, ref.fact_id, ref.revision)
            if version is None or any(version.get(key) != getattr(ref, key)
                    for key in ("result_key", "slot_id", "path")) or (ref.sha256 and version.get("sha256") != ref.sha256):
                raise HTTPException(409, "历史文件版本身份不匹配")
            if ref.run_id and version.get("run_id") != ref.run_id:
                raise HTTPException(409, "历史文件运行来源不匹配")
            after = version.get("after") or {}
            if not after.get("available") or not isinstance(after.get("text"), str):
                raise HTTPException(409, "此历史版本未保存可展示的内容；不能以当前磁盘文件代替")
            identity["sha256"] = version.get("sha256")
            return FileLinkContent(identity, after["text"].encode("utf-8"), "text/plain")
        if ref.version_id:
            from backend.app.desktop.material_ownership import load_material
            from backend.app.desktop.material_snapshots import MaterialSnapshotVerifier

            version = await session.get(MaterialVersion, ref.version_id)
            if version is None:
                raise HTTPException(404, "材料历史版本不存在")
            material, source_workspace = await load_material(session, version.material_id, task_id=task_id)
            if source_workspace.workspace_id != workspace.workspace_id or material.relative_path != ref.path:
                raise HTTPException(409, "材料历史路径或工作区不匹配")
            if ref.run_id or ref.slot_id or ref.result_key or ref.revision or (ref.sha256 and ref.sha256 != version.digest):
                raise HTTPException(409, "材料历史版本身份不匹配")
            raw = MaterialSnapshotVerifier()._blob(Path(workspace.path), version.object_id)
            if hashlib.sha256(raw).hexdigest() != version.digest:
                raise HTTPException(409, "材料历史内容与版本不匹配")
            identity["sha256"] = version.digest
            return self._content(identity, raw, Path(ref.path).suffix.lower())
        raise HTTPException(409, "此历史版本未保存对应内容；不能以当前磁盘文件代替")

    @staticmethod
    def _content(identity, raw, suffix):
        media_types = {".md": "text/plain", ".txt": "text/plain", ".pdf": "application/pdf",
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
            ".webp": "image/webp", ".bmp": "image/bmp"}
        if suffix in (".docx", ".doc"):
            # Extract from the bytes just read, including Git snapshots, never from
            # a second read of the live file. Keep linked documents read-only.
            import tempfile
            from focus.readers import _read_docx, _read_doc
            try:
                with tempfile.TemporaryDirectory(prefix="focus-file-link-") as directory:
                    path = Path(directory) / f"content{suffix}"
                    path.write_bytes(raw)
                    text = (_read_docx if suffix == ".docx" else _read_doc)(str(path))
            except Exception as error:
                raise HTTPException(422, "该文件内容无法提取为只读预览") from error
            return FileLinkContent(identity, text.encode("utf-8"), "text/plain")
        if suffix not in media_types:
            raise HTTPException(422, "当前不支持此文件类型的预览")
        return FileLinkContent(identity, raw, media_types[suffix])


file_link_router = APIRouter()


@file_link_router.post("/tasks/{task_id}/file-links/content")
async def read_file_link(task_id: str, body: FileLinkReference, request: Request):
    import json
    from urllib.parse import quote
    async with request.app.state.desktop_service.session_factory() as session:
        content = await FileLinkReader().read(session, task_id, body)
    return Response(content=content.data, media_type=content.media_type, headers={
        "Cache-Control": "no-store",
        "X-Focus-File-Reference": quote(json.dumps(content.reference, ensure_ascii=False)),
    })
