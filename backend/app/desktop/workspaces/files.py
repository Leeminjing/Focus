"""工作区文件发现、路径授权、版本读取和有界文档预览。"""

from __future__ import annotations

import asyncio
import base64
import io
import os
from hashlib import sha256
from pathlib import Path

from fastapi import HTTPException
from focus.security import (
    AccessDecision,
    AccessOperation,
    canonical_target,
    decide_path_access,
    is_within,
    restrictive_policy,
)
from pydantic import BaseModel, ConfigDict

from backend.app.desktop.patrol.agent_loop.live.live_access import LoopLiveAccessPolicy
from backend.app.desktop.patrol.agent_loop.models import AgentLoop
from backend.app.desktop.persistence.models import DesktopThread, DesktopWorkspace
from backend.app.desktop.runs.evidence.file_reads import FileVersionReader
from backend.app.desktop.workspaces.coordination.models import WorkspaceSlot
from backend.app.desktop.workspaces.file_links import (
    FileLinkReader,
    FileLinkReference,
    resolve_link_path,
)

PREVIEW_LIMIT = 4 * 1024 * 1024


def search_workspace_files(root: str, query: str, limit: int) -> dict:
    """Bounded discovery in the task workspace; every result passes read policy."""
    base = Path(root).resolve()
    policy = restrictive_policy(base)
    excluded = {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".focus",
    }
    needle = query.replace("\\", "/").casefold()
    found, scanned, truncated = ([], 0, False)
    for directory, dirs, names in os.walk(base, followlinks=False):
        dirs[:] = sorted(
            (
                name
                for name in dirs
                if name not in excluded
                and (not (Path(directory) / name).is_symlink())
                and (
                    decide_path_access(
                        policy, Path(directory) / name, AccessOperation.READ
                    )
                    is AccessDecision.ALLOW
                )
            )
        )
        for name in sorted(names):
            scanned += 1
            if scanned > 20000:
                truncated = True
                break
            path = Path(directory) / name
            relative = path.relative_to(base).as_posix()
            if needle not in relative.casefold():
                continue
            resolved = path.resolve()
            if not is_within(base, resolved) or not resolved.is_file():
                continue
            if (
                decide_path_access(policy, resolved, AccessOperation.READ)
                is not AccessDecision.ALLOW
            ):
                continue
            found.append({"path": str(resolved), "relative_path": relative})
            if len(found) >= limit:
                truncated = True
                break
        if truncated:
            break
    return {"files": found, "truncated": truncated}


async def search_task_files(
    task_id: str, session_factory, q: str = "", limit: int = 30
):
    async with session_factory() as session:
        task = await session.get(DesktopThread, task_id)
        workspace = (
            await session.get(DesktopWorkspace, task.workspace_id) if task else None
        )
        if workspace is None:
            raise HTTPException(404, "任务工作区不存在")
        root, workspace_id = (workspace.path, workspace.workspace_id)
    result = await asyncio.to_thread(search_workspace_files, root, q, limit)
    for item in result["files"]:
        item["workspace_id"] = workspace_id
    return result


class FileTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str | None = None
    content_ref: str | None = None
    loop_id: str | None = None
    fact_id: str | None = None
    revision: int | None = None
    current: bool = False
    link: FileLinkReference | None = None


def resolve_file(root: str, reference: str) -> Path:
    base = Path(root).resolve()
    path = canonical_target(base, reference)
    if not is_within(base, path):
        raise HTTPException(403, "文件不属于当前工作区")
    if (
        decide_path_access(restrictive_policy(base), path, AccessOperation.READ)
        is not AccessDecision.ALLOW
    ):
        raise HTTPException(403, "文件不可读取")
    if not path.is_file():
        raise HTTPException(404, "文件不存在，可能已被移动或删除")
    return path


async def resolve_target(session, target: FileTarget):
    if target.link is not None:
        if (
            not target.task_id
            or target.content_ref
            or target.loop_id
            or target.fact_id
            or target.revision
        ):
            raise HTTPException(422, "文件链接需要精确的来源任务")
        ref = target.link
        if ref.view != "current" or any(
            (ref.fact_id, ref.revision, ref.result_key, ref.sha256, ref.version_id)
        ):
            raise HTTPException(422, "历史链接必须使用历史版本入口")
        task = await session.get(DesktopThread, target.task_id)
        workspace = (
            await session.get(DesktopWorkspace, task.workspace_id) if task else None
        )
        if workspace is None or (
            ref.workspace_id and ref.workspace_id != workspace.workspace_id
        ):
            raise HTTPException(404, "文件工作区不存在或不匹配")
        root, _ = await FileLinkReader()._root(session, target.task_id, workspace, ref)
        return (resolve_file(root, str(resolve_link_path(root, ref.path))), None)
    if (
        target.task_id
        and target.content_ref
        and (not target.loop_id)
        and (not target.fact_id)
    ):
        task = await session.get(DesktopThread, target.task_id)
        workspace = (
            await session.get(DesktopWorkspace, task.workspace_id) if task else None
        )
        if workspace is None:
            raise HTTPException(404, "任务工作区不存在")
        return (resolve_file(workspace.path, target.content_ref), None)
    if not target.loop_id or not target.fact_id or target.task_id or target.content_ref:
        raise HTTPException(422, "需要任务文件或精确事实版本")
    access = await LoopLiveAccessPolicy().resolve(session, target.loop_id)
    if not access.permissions.intersection({"read", "view_evidence"}):
        raise HTTPException(403, "当前无权读取文件证据")
    version = await FileVersionReader().read(
        session, target.loop_id, target.fact_id, target.revision
    )
    if version is None:
        raise HTTPException(404, "文件版本不可用")
    loop = await session.get(AgentLoop, target.loop_id)
    if not target.current and version.get("after", {}).get("available"):
        return (None, version)
    slot = await session.get(WorkspaceSlot, version["slot_id"])
    if slot is None or slot.workspace_id != loop.workspace_id:
        raise HTTPException(409, "文件执行位置不可用")
    return (resolve_file(slot.root_path, version["path"]), version)


def read_content(
    path: Path | None, version: dict | None, current: bool, limit: int | None = None
) -> bytes:
    if version and (not current) and version.get("after", {}).get("available"):
        data = version["after"]["text"].encode("utf-8")
    else:
        try:
            if limit and path.stat().st_size > limit:
                raise HTTPException(
                    413, "文件超过 4 MiB 预览上限，请用系统应用打开或另存为"
                )
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise HTTPException(404, "文件不存在，可能已被移动或删除") from exc
        except OSError as exc:
            raise HTTPException(422, "无法读取文件，请检查文件权限或占用情况") from exc
        if (
            version
            and (not current)
            and (
                not version.get("sha256")
                or sha256(data).hexdigest() != version["sha256"]
            )
        ):
            raise HTTPException(
                409, "磁盘文件已变化，此历史版本未保存正文；可打开当前文件或所在文件夹"
            )
        if version and (not current) and version.get("after", {}).get("redacted"):
            raise HTTPException(409, "此版本已脱敏，不能读取原始文件")
    if limit and len(data) > limit:
        raise HTTPException(413, "文件超过 4 MiB 预览上限，请用系统应用打开或另存为")
    return data


async def file_location(target: FileTarget, session_factory):
    if target.loop_id and (not target.current):
        raise HTTPException(422, "历史版本没有可直接打开的磁盘位置")
    async with session_factory() as session:
        path, _ = await resolve_target(session, target)
    return {"path": str(path), "name": path.name}


async def file_content(target: FileTarget, session_factory, preview: bool = False):
    async with session_factory() as session:
        path, version = await resolve_target(session, target)
    data = await asyncio.to_thread(
        read_content, path, version, target.current, PREVIEW_LIMIT if preview else None
    )
    return data


async def preview_bytes(target: FileTarget, session_factory):
    async with session_factory() as session:
        path, version = await resolve_target(session, target)
    data = await asyncio.to_thread(
        read_content, path, version, target.current, PREVIEW_LIMIT
    )
    name = version["path"] if version else path.name
    return (name, data)


async def file_text(target: FileTarget, session_factory):
    name, data = await preview_bytes(target, session_factory)

    def extract():
        if Path(name).suffix.lower() == ".docx":
            from docx import Document

            document = Document(io.BytesIO(data))
            from docx.oxml.ns import qn

            return "\n".join(
                (
                    "".join((node.text or "" for node in paragraph.iter(qn("w:t"))))
                    for paragraph in document.element.body.iter(qn("w:p"))
                )
            )
        if Path(name).suffix.lower() == ".doc":
            raise HTTPException(422, "旧版 Word 文件请用系统应用打开或另存为")
        raise HTTPException(422, "此格式没有文本提取器")

    try:
        content = await asyncio.to_thread(extract)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, "文档文本提取失败，请用系统应用打开或另存为") from exc
    return {"content": content}


async def file_pdf_page(target: FileTarget, session_factory, page: int = 1):
    name, data = await preview_bytes(target, session_factory)
    if Path(name).suffix.lower() != ".pdf":
        raise HTTPException(422, "文件不是 PDF")

    def render():
        import pypdfium2 as pdfium

        with pdfium.PdfDocument(data) as document:
            count = len(document)
            if page < 1 or page > count:
                raise HTTPException(422, "PDF 页码超出范围")
            pdf_page = document[page - 1]
            try:
                scale = min(1.5, 2000 / max(pdf_page.get_size()))
                bitmap = pdf_page.render(scale=scale)
                try:
                    output = io.BytesIO()
                    bitmap.to_pil().save(output, format="PNG")
                    return {
                        "image": base64.b64encode(output.getvalue()).decode("ascii"),
                        "page_count": count,
                    }
                finally:
                    bitmap.close()
            finally:
                pdf_page.close()

    try:
        return render()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, "PDF 预览失败，请用系统应用打开或另存为") from exc
