"""spatial-patrol 插件的桌面 API 路由(经系统挂载在 /desktop/api/plugin/spatial-patrol 前缀)。

对外提供:
    router — APIRouter:锚点 CRUD、投放/复制/完成/回收、观察预览、PDF 页渲染

投放边界:
    deploy/copy/continue 必须显式提交非空 permissions;服务端不补默认只读权限。

锚点语义(f18-spatial-anchor spec):
    点击立即成立锚点(kind=anchor);投放升格为空间小兵(kind=patrol),run 记录进
    desktop_runs(kind="spatial"),经统一 run_agent 链路执行,checkpoint 命名空间
    patrol:{spatial_id};投放失败锚点保留;载体失效标记 invalid。

统一会话保护由系统 AuthMiddleware 承担,本路由不重复校验。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from focus.agents.lead import make_lead_agent
from focus.config import get_app_config
from focus.runtime.checkpointer.namespaced import NamespacedCheckpointer
from focus.runtime.runs.limits import DEFAULT_AGENT_RECURSION_LIMIT
from focus.runtime.runs.schemas import DisconnectMode
from focus.runtime.runs.worker import run_agent
from focus.tools.builtins.workspace_tools import select_workspace_tools

from plugins.spatial_patrol import spatial
from plugins.spatial_patrol import db as spatial_db
from plugins.spatial_patrol.docx_routes import router as docx_router
from plugins.spatial_patrol.docx_semantic import DOCX_COORDINATE_SPACE
from plugins.spatial_patrol.docx_sessions import SessionError, session_manager as docx_sessions
from plugins.spatial_patrol.docx_tools import apply_docx_edit, observe_docx_target
from plugins.spatial_patrol.models import SpatialAnchor


@asynccontextmanager
async def _plugin_lifespan(_app):
    await spatial_db.ensure_tables()
    yield


router = APIRouter(lifespan=_plugin_lifespan)
router.include_router(docx_router)
TEXT_COORDINATE_SPACE = "text-character-v1"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnchorCreate(_Strict):
    task_id: str = Field(min_length=1)
    content_ref: str = Field(min_length=1)
    page: int = Field(default=1, ge=1)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    coordinate_space: str | None = Field(default=None, pattern=r"^text-character-v1$")


class AnchorPatch(_Strict):
    page: int | None = Field(default=None, ge=1)
    x: float | None = Field(default=None, ge=0, le=1)
    y: float | None = Field(default=None, ge=0, le=1)
    coordinate_space: str | None = Field(default=None, pattern=r"^text-character-v1$")


class DeployRequest(_Strict):
    instruction: str = Field(min_length=1)
    permissions: list[str] = Field(min_length=1)


class CopyRequest(_Strict):
    page: int | None = Field(default=None, ge=1)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    coordinate_space: str | None = Field(default=None, pattern=r"^text-character-v1$")
    permissions: list[str] = Field(min_length=1)


class ObserveRequest(_Strict):
    radius: float | None = Field(default=None, gt=0)


def _session_factory():
    from focus.persistence.engine import get_session_factory

    return get_session_factory()


async def _task_workspace(session: Any, task_id: str) -> tuple[str, str]:
    """返回 (thread_id, workspace_path);任务不存在抛 404。"""
    from backend.app.desktop.models import DesktopThread, DesktopWorkspace

    task = await session.get(DesktopThread, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    workspace = await session.get(DesktopWorkspace, task.workspace_id)
    if not workspace:
        raise HTTPException(404, "工作区不存在")
    return task.thread_id, workspace.path


def _validate_content_ref(workspace_path: str, content_ref: str, page: int = 1) -> None:
    """载体必须存在于工作区内且支持空间能力(图像页 + docx/doc);否则锚点不成立(422)。"""
    service = spatial.get_service()
    if not service.supports_spatial(content_ref):
        raise HTTPException(422, "载体不支持空间能力(仅图片/PDF/docx/doc)")
    path = service.resolve_path(workspace_path, content_ref)
    if not path.is_file():
        raise HTTPException(422, "载体文件不存在")
    if path.suffix.lower() == ".pdf":
        if page > service.pdf_page_count(path):
            raise HTTPException(422, f"PDF 页码不存在: {page}")
    elif page != 1:
        raise HTTPException(422, "单页载体的页码必须为 1")


def _is_text_content(content_ref: str) -> bool:
    return content_ref.lower().endswith((".docx", ".doc"))


def _text_coordinate_region(content_ref: str, coordinate_space: str | None) -> dict | None:
    if not _is_text_content(content_ref):
        return None
    if coordinate_space != TEXT_COORDINATE_SPACE:
        raise HTTPException(422, "文本锚点必须使用字符坐标，请重新点击目标文字")
    return {"coordinate_space": TEXT_COORDINATE_SPACE}


def _validate_write_anchor(anchor: SpatialAnchor, permissions: list[str]) -> None:
    if not _is_docx_write(anchor.content_ref, permissions):
        return
    if not isinstance(anchor.region, dict) or anchor.region.get("coordinate_space") != DOCX_COORDINATE_SPACE:
        raise HTTPException(
            409,
            "旧 DOCX 锚点不具备 Word 语义目标，不能执行结构化写操作；"
            "请重新点击目标文字建立锚点",
        )


async def _mark_invalid(session: Any, anchor: SpatialAnchor, workspace_path: str) -> SpatialAnchor:
    """载体失效检查:文件消失 → status=invalid(不静默迁移)。"""
    if anchor.status == "dismissed":
        return anchor
    service = spatial.get_service()
    try:
        path = service.resolve_path(workspace_path, anchor.content_ref)
    except ValueError:
        anchor.status = "invalid"
        return anchor
    if not path.is_file():
        anchor.status = "invalid"
    elif path.suffix.lower() == ".pdf":
        try:
            if anchor.page > service.pdf_page_count(path):
                anchor.status = "invalid"
        except Exception:
            anchor.status = "invalid"
    elif anchor.page != 1:
        anchor.status = "invalid"
    return anchor


# === 锚点 CRUD ===


@router.post("/anchors")
async def create_anchor(body: AnchorCreate, request: Request) -> dict[str, Any]:
    """点击立即成立锚点:先于小兵、不依赖对象识别。"""
    factory = _session_factory()
    async with factory() as session:
        thread_id, workspace_path = await _task_workspace(session, body.task_id)
        _validate_content_ref(workspace_path, body.content_ref, body.page)
        anchor = SpatialAnchor(
            spatial_id=_new_id(),
            task_id=body.task_id,
            kind="anchor",
            content_ref=body.content_ref,
            page=body.page,
            x=body.x,
            y=body.y,
            region=_text_coordinate_region(body.content_ref, body.coordinate_space),
            status="active",
        )
        session.add(anchor)
        await session.commit()
        return anchor.to_payload()


@router.get("/tasks/{task_id}/anchors")
async def list_anchors(task_id: str, request: Request) -> list[dict[str, Any]]:
    """列出任务全部锚点与小兵;载体失效的条目标记 invalid。"""
    from backend.app.desktop.models import DesktopRun

    factory = _session_factory()
    async with factory() as session:
        _, workspace_path = await _task_workspace(session, task_id)
        rows = (
            await session.execute(
                select(SpatialAnchor, DesktopRun)
                .outerjoin(DesktopRun, SpatialAnchor.run_id == DesktopRun.run_id)
                .where(SpatialAnchor.task_id == task_id)
                .order_by(SpatialAnchor.created_at)
            )
        ).all()
        changed = False
        payloads: list[dict[str, Any]] = []
        for anchor, run in rows:
            previous_status = anchor.status
            await _mark_invalid(session, anchor, workspace_path)
            if anchor.status == "deployed" and run is not None and run.status == "error":
                anchor.status = "needs_action"
            if anchor.status != previous_status:
                changed = True
            payloads.append(_anchor_payload(anchor, run))
        if changed:
            await session.commit()
        return payloads


def _anchor_payload(anchor: SpatialAnchor, run: Any | None = None) -> dict[str, Any]:
    payload = anchor.to_payload()
    if run is not None:
        payload["run_status"] = run.status
        payload["run_error"] = run.error
    return payload


@router.patch("/anchors/{spatial_id}")
async def patch_anchor(spatial_id: str, body: AnchorPatch, request: Request) -> dict[str, Any]:
    """拖拽重投:更新锚点坐标(新坐标成为小兵新的工作中心)。"""
    factory = _session_factory()
    async with factory() as session:
        anchor = await session.get(SpatialAnchor, spatial_id)
        if not anchor:
            raise HTTPException(404, "锚点不存在")
        if anchor.status == "dismissed":
            raise HTTPException(409, "小兵已回收,不能移动")
        _, workspace_path = await _task_workspace(session, anchor.task_id)
        target_page = body.page if body.page is not None else anchor.page
        _validate_content_ref(workspace_path, anchor.content_ref, target_page)
        if body.page is not None:
            anchor.page = body.page
        if body.x is not None:
            anchor.x = body.x
        if body.y is not None:
            anchor.y = body.y
        if _is_text_content(anchor.content_ref) and (body.x is not None or body.y is not None):
            anchor.region = _text_coordinate_region(anchor.content_ref, body.coordinate_space)
        await session.commit()
        return anchor.to_payload()


# === 投放与生命周期 ===


@router.post("/anchors/{spatial_id}/deploy")
async def deploy(spatial_id: str, body: DeployRequest, request: Request) -> dict[str, Any]:
    """在锚点上投放空间小兵:升格记录、建 DesktopRun、经统一链路执行。"""
    factory = _session_factory()
    async with factory() as session:
        anchor = await session.get(SpatialAnchor, spatial_id)
        if not anchor:
            raise HTTPException(404, "锚点不存在")
        thread_id, workspace_path = await _task_workspace(session, anchor.task_id)
        _validate_content_ref(workspace_path, anchor.content_ref, anchor.page)
        if anchor.status == "invalid":
            raise HTTPException(409, "锚点已失效,不能投放")
        permissions = _normalize_permissions(body.permissions)
        _validate_write_scope(anchor.content_ref, permissions)
        _validate_write_anchor(anchor, permissions)
        previous = (
            anchor.kind, anchor.status, anchor.task_instruction, anchor.run_id,
        )
        from backend.app.desktop.models import DesktopRun

        run_row = DesktopRun(
            run_id=_new_id(),
            task_id=anchor.task_id,
            agent_id=spatial_id,
            kind="spatial",
            status="pending",
            input_messages=[{"role": "human", "content": body.instruction}],
        )
        anchor.kind = "patrol"
        anchor.status = "deployed"
        anchor.task_instruction = body.instruction
        anchor.run_id = run_row.run_id
        session.add(run_row)
        await session.commit()
        from backend.app.desktop.models import DesktopThread

        task_row = await session.get(DesktopThread, anchor.task_id)
        workspace_id = task_row.workspace_id
        thread_id = task_row.thread_id
        model_name = None

    try:
        record = await _launch_spatial_run(
            request, run_row.run_id, thread_id, anchor, workspace_id, workspace_path,
            permissions, model_name,
        )
    except Exception as exc:
        # 投放失败:锚点保留(位置已经确定,但小兵暂时无法进入)
        async with factory() as session:
            run_row = await session.get(DesktopRun, run_row.run_id)
            if run_row:
                run_row.status = "error"
                run_row.error = f"投放失败: {exc}"
            saved_anchor = await session.get(SpatialAnchor, spatial_id)
            if saved_anchor:
                (
                    saved_anchor.kind,
                    saved_anchor.status,
                    saved_anchor.task_instruction,
                    saved_anchor.run_id,
                ) = previous
            await session.commit()
        raise HTTPException(502, f"位置已经确定,但小兵暂时无法进入: {exc}") from exc
    return {"spatial_id": spatial_id, "run_id": run_row.run_id, "status": record.status.value}


@router.post("/anchors/{spatial_id}/copy")
async def copy_anchor(spatial_id: str, body: CopyRequest, request: Request) -> dict[str, Any]:
    """复制小兵到另一坐标:相似任务要求、不同空间锚点。"""
    factory = _session_factory()
    async with factory() as session:
        source = await session.get(SpatialAnchor, spatial_id)
        if not source:
            raise HTTPException(404, "锚点不存在")
        if source.kind != "patrol" or source.status in ("invalid", "dismissed"):
            raise HTTPException(409, "只有有效空间小兵可以复制投放")
        thread_id, workspace_path = await _task_workspace(session, source.task_id)
        target_page = body.page if body.page is not None else source.page
        _validate_content_ref(workspace_path, source.content_ref, target_page)
        permissions = _normalize_permissions(body.permissions)
        _validate_write_scope(source.content_ref, permissions)
        copy_region = _text_coordinate_region(source.content_ref, body.coordinate_space)
        from backend.app.desktop.models import DesktopRun, DesktopThread

        copy = SpatialAnchor(
            spatial_id=_new_id(),
            task_id=source.task_id,
            kind="patrol",
            content_ref=source.content_ref,
            page=target_page,
            x=body.x,
            y=body.y,
            region=copy_region,
            status="deployed",
            task_instruction=source.task_instruction,
        )
        run_row = DesktopRun(
            run_id=_new_id(), task_id=source.task_id, agent_id=copy.spatial_id,
            kind="spatial", status="pending",
            input_messages=[{"role": "human", "content": copy.task_instruction}],
        )
        copy.run_id = run_row.run_id
        session.add(copy)
        session.add(run_row)
        await session.commit()
        task_row = await session.get(DesktopThread, source.task_id)
        workspace_id = task_row.workspace_id

    try:
        record = await _launch_spatial_run(
            request, run_row.run_id, thread_id, copy, workspace_id, workspace_path,
            permissions, None,
        )
    except Exception as exc:
        async with factory() as session:
            failed_run = await session.get(DesktopRun, run_row.run_id)
            if failed_run:
                failed_run.status = "error"
                failed_run.error = f"复制投放失败: {exc}"
            saved_copy = await session.get(SpatialAnchor, copy.spatial_id)
            if saved_copy:
                saved_copy.kind = "anchor"
                saved_copy.status = "active"
                saved_copy.run_id = None
            await session.commit()
        raise HTTPException(502, f"新位置已经确定,但复制的小兵暂时无法进入: {exc}") from exc
    return {
        "anchor": copy.to_payload(), "spatial_id": copy.spatial_id,
        "run_id": run_row.run_id, "status": record.status.value,
    }


@router.post("/anchors/{spatial_id}/continue")
async def continue_patrol(
    spatial_id: str, body: DeployRequest, request: Request,
) -> dict[str, Any]:
    """把后续指令交给已聚焦空间小兵，沿用其 checkpoint 命名空间。"""
    factory = _session_factory()
    async with factory() as session:
        anchor = await session.get(SpatialAnchor, spatial_id)
        if not anchor:
            raise HTTPException(404, "空间小兵不存在")
        if anchor.kind != "patrol" or anchor.status in ("invalid", "dismissed"):
            raise HTTPException(409, "当前锚点不是可继续执行的空间小兵")
        thread_id, workspace_path = await _task_workspace(session, anchor.task_id)
        _validate_content_ref(workspace_path, anchor.content_ref, anchor.page)
        permissions = _normalize_permissions(body.permissions)
        _validate_write_scope(anchor.content_ref, permissions)
        _validate_write_anchor(anchor, permissions)
        previous_status, previous_run_id = anchor.status, anchor.run_id
        from backend.app.desktop.models import DesktopRun, DesktopThread

        run_row = DesktopRun(
            run_id=_new_id(), task_id=anchor.task_id, agent_id=spatial_id,
            kind="spatial", status="pending",
            input_messages=[{"role": "human", "content": body.instruction}],
        )
        anchor.status = "deployed"
        anchor.run_id = run_row.run_id
        session.add(run_row)
        await session.commit()
        task_row = await session.get(DesktopThread, anchor.task_id)
        workspace_id = task_row.workspace_id

    try:
        record = await _launch_spatial_run(
            request, run_row.run_id, thread_id, anchor, workspace_id, workspace_path,
            permissions, None, input_instruction=body.instruction,
        )
    except Exception as exc:
        async with factory() as session:
            failed_run = await session.get(DesktopRun, run_row.run_id)
            if failed_run:
                failed_run.status = "error"
                failed_run.error = f"继续执行失败: {exc}"
            saved_anchor = await session.get(SpatialAnchor, spatial_id)
            if saved_anchor:
                saved_anchor.status = previous_status
                saved_anchor.run_id = previous_run_id
            await session.commit()
        raise HTTPException(502, f"空间小兵暂时无法继续: {exc}") from exc
    return {"spatial_id": spatial_id, "run_id": run_row.run_id, "status": record.status.value}


@router.post("/anchors/{spatial_id}/complete")
async def complete(spatial_id: str, request: Request) -> dict[str, Any]:
    """任务完成:位置化完成状态(⚔ → ✓)。"""
    factory = _session_factory()
    async with factory() as session:
        anchor = await session.get(SpatialAnchor, spatial_id)
        if not anchor:
            raise HTTPException(404, "锚点不存在")
        if anchor.kind != "patrol" or anchor.status in ("invalid", "dismissed"):
            raise HTTPException(409, "只有有效空间小兵可以标记完成")
        anchor.status = "done"
        await session.commit()
        return anchor.to_payload()


@router.post("/anchors/{spatial_id}/dismiss")
async def dismiss(spatial_id: str, request: Request) -> dict[str, Any]:
    """回收小兵:从原位置淡出,不影响该空间本身。"""
    factory = _session_factory()
    async with factory() as session:
        anchor = await session.get(SpatialAnchor, spatial_id)
        if not anchor:
            raise HTTPException(404, "锚点不存在")
        anchor.status = "dismissed"
        await session.commit()
        return anchor.to_payload()


# === 观察与预览 ===


@router.post("/anchors/{spatial_id}/observe")
async def observe(spatial_id: str, body: ObserveRequest, request: Request) -> dict[str, Any]:
    """观察预览:直接取样锚点附近内容(供 UI 与调试,不经小兵工具)。"""
    factory = _session_factory()
    async with factory() as session:
        anchor = await session.get(SpatialAnchor, spatial_id)
        if not anchor:
            raise HTTPException(404, "锚点不存在")
        _, workspace_path = await _task_workspace(session, anchor.task_id)
    service = spatial.get_service()
    radius = body.radius if body.radius is not None else service.radius_start
    try:
        text = await service.observe(
            workspace_path, anchor.content_ref, anchor.page, anchor.x, anchor.y, radius,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"spatial_id": spatial_id, "radius": radius, "content": text}


@router.get("/preview")
async def preview(request: Request, task_id: str, content_ref: str, page: int = 1):
    """载体页图像:图片返回原文件字节,PDF 返回按约定比例渲染的页 PNG。"""
    from fastapi.responses import Response

    factory = _session_factory()
    async with factory() as session:
        _, workspace_path = await _task_workspace(session, task_id)
    service = spatial.get_service()
    path = service.resolve_path(workspace_path, content_ref)
    if not path.is_file():
        raise HTTPException(404, "载体文件不存在")
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            data = service.pdf_page_png(path, page)
        except Exception as exc:
            raise HTTPException(422, f"PDF 页渲染失败: {exc}") from exc
        return Response(content=data, media_type="image/png")
    if suffix in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
        return Response(content=path.read_bytes(), media_type=f"image/{suffix.lstrip('.')}")
    raise HTTPException(422, "载体不是可查看类型(图片或 PDF)")


@router.get("/metadata")
async def content_metadata(request: Request, task_id: str, content_ref: str) -> dict[str, Any]:
    """返回查看器需要的载体元数据，主要用于 PDF 页数边界与翻页控件。"""
    factory = _session_factory()
    async with factory() as session:
        _, workspace_path = await _task_workspace(session, task_id)
    service = spatial.get_service()
    path = service.resolve_path(workspace_path, content_ref)
    if not path.is_file() or not service.is_viewable(content_ref):
        raise HTTPException(404, "载体文件不存在或不可查看")
    return {
        "content_ref": content_ref,
        "page_count": service.pdf_page_count(path) if path.suffix.lower() == ".pdf" else 1,
    }


@router.get("/text")
async def text_content(request: Request, task_id: str, content_ref: str) -> dict:
    """文本类载体内容:docx/doc 经 focus/readers 提取,md/txt 直接读取,按扩展名分发。"""
    factory = _session_factory()
    async with factory() as session:
        _, workspace_path = await _task_workspace(session, task_id)
    service = spatial.get_service()
    path = service.resolve_path(workspace_path, content_ref)
    if not path.is_file():
        raise HTTPException(404, "载体文件不存在")
    suffix = path.suffix.lower()
    try:
        if suffix == ".docx":
            from focus.readers import _read_docx

            text = _read_docx(str(path))
        elif suffix == ".doc":
            from focus.readers import _read_doc

            text = _read_doc(str(path))
        elif suffix in (".md", ".txt"):
            text = path.read_text(encoding="utf-8", errors="replace")
        else:
            raise HTTPException(422, "载体不是文本类型(docx/doc/md/txt)")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"文本提取失败: {exc}") from exc
    return {"content": text, "length": len(text)}


# === 投放执行链路 ===


async def _launch_spatial_run(
    request: Request,
    run_id: str,
    thread_id: str,
    anchor: SpatialAnchor,
    workspace_id: str,
    workspace_path: str,
    permissions: list[str],
    model_name: str | None,
    input_instruction: str | None = None,
):
    """经统一 run_agent 链路启动空间小兵(参考桌面 _launch_swarm_run)。"""
    from langchain_core.messages import HumanMessage

    from backend.app.desktop.checkpoint_recovery import select_checkpoint_base
    from backend.app.desktop.tool_error_provider import build_tool_error_middleware

    app_config = get_app_config("config.yaml")
    state = request.app.state
    run_manager = state.run_manager
    checkpointer = state.checkpointer
    checkpoint_ns = f"patrol:{anchor.spatial_id}"
    checkpoint_id = await select_checkpoint_base(checkpointer, thread_id, checkpoint_ns)
    requires_verified_change = _is_docx_write(anchor.content_ref, permissions)
    is_docx_session = anchor.content_ref.lower().endswith(".docx")
    docx_session = None
    if is_docx_session:
        try:
            docx_session = await docx_sessions.find_active(
                task_id=anchor.task_id,
                content_ref=anchor.content_ref,
                require_edit=requires_verified_change,
            )
        except SessionError as exc:
            raise HTTPException(409, str(exc)) from exc
    docx_target_id = (
        anchor.region.get("target", {}).get("target_id")
        if isinstance(anchor.region, dict) else None
    )
    change_evidence: dict[str, Any] = {}
    docx_observation_candidates: dict[str, Any] = {}
    # Legacy observe_docx_delete_candidate/delete_docx_paragraph stay in docx_edit.py
    # for historical callers, but active editor sessions intentionally never register them.

    def factory():
        service = spatial.get_service()
        workspace_permissions = (
            [permission for permission in permissions if permission != "write"]
            if requires_verified_change else permissions
        )
        tools = [*select_workspace_tools(workspace_permissions)]
        if is_docx_session:
            tools.append(observe_docx_target)
            if requires_verified_change:
                tools.append(apply_docx_edit)
        else:
            tools.extend(spatial.build_observation_tools(service))
        prompt = (
            "你是 Focus 的空间小兵,驻守在一个用户指定的空间锚点上。\n"
            f"锚点: 载体 {anchor.content_ref},第 {anchor.page} 页,"
            f"坐标 ({anchor.x:.3f}, {anchor.y:.3f})。\n"
            + (
                f"用户把你放在 Word 语义目标 target_id={docx_target_id} 上，从该目标开始观察；"
                "不要先读全文。\n"
                if is_docx_session else
                "用户把你放在这里,从这里开始找:先用 observe_anchor 观察锚点附近内容,"
                "信息不足时用 expand_observation 逐圈扩大观察半径,由近及远。\n"
            )
            + "观察到的对象识别结果不改变你的驻守锚点。\n"
            f"任务: {anchor.task_instruction}\n"
            "完成后简短汇报:你在锚点附近发现了什么、做了什么。"
        )
        if requires_verified_change:
            prompt += (
                "\n当前 DOCX 已获得本次写授权。必须先用 observe_docx_target 观察锚点中的"
                "语义 target_id，再用 apply_docx_edit 对该目标执行白名单结构化操作。"
                "每次修改必须由编辑器形成一个撤销历史点；只有返回 changed=true 且"
                "document_version 递增，才能宣称编辑器内容已修改。保存状态与编辑状态分开汇报。"
            )
        elif is_docx_session:
            prompt += (
                "\n当前 DOCX 为 Word 语义只读会话。只用 observe_docx_target 从锚点目标开始观察，"
                "不要提取或扫描全文。"
            )
        return make_lead_agent(
            model_name=model_name,
            tools=tools,
            system_prompt=prompt,
            middlewares=[],
            additional_middlewares=[build_tool_error_middleware()],
            app_config=app_config,
        )

    graph_input = {"messages": [HumanMessage(content=input_instruction or anchor.task_instruction)]}
    runnable_config: dict[str, Any] = {
        "max_concurrency": None,
        "recursion_limit": DEFAULT_AGENT_RECURSION_LIMIT,
        "metadata": {"run_id": run_id},
        "configurable": {
            "thread_id": thread_id,
            "run_id": run_id,
            "checkpoint_ns": checkpoint_ns,
        },
    }
    if checkpoint_id is not None:
        runnable_config["configurable"]["checkpoint_id"] = checkpoint_id
    langgraph_context: dict[str, Any] = {
        "model_name": model_name,
        "workspace_id": workspace_id,
        "agent_id": anchor.spatial_id,
        "task_id": anchor.task_id,
        "permissions": permissions,
        "workspace": workspace_path,
        "checkpoint_ns": checkpoint_ns,
        "run_id": run_id,
        "app_config": app_config,
        "user_id": None,
        # 空间上下文:观察工具经 runtime.context 读取
        "spatial_id": anchor.spatial_id,
        "content_ref": anchor.content_ref,
        "page": anchor.page,
        "x": anchor.x,
        "y": anchor.y,
        "docx_change_evidence": change_evidence,
        "docx_observation_candidates": docx_observation_candidates,
        "docx_session_id": docx_session.session_id if docx_session else None,
        "docx_document_id": docx_session.document_id if docx_session else None,
        "docx_document_version": docx_session.document_version if docx_session else None,
        "docx_target_id": docx_target_id,
    }
    namespaced = NamespacedCheckpointer(checkpointer, checkpoint_ns)
    record = run_manager.create(
        thread_id=thread_id, run_id=run_id,
        on_disconnect=DisconnectMode.cancel, model_name=model_name,
    )
    task = asyncio.create_task(
        run_agent(
            record=record,
            bridge=state.stream_bridge,
            run_manager=run_manager,
            app_config=app_config,
            graph_input=graph_input,
            runnable_config=runnable_config,
            stream_modes=["values"],
            langgraph_context=langgraph_context,
            agent_factory=factory,
            checkpointer=namespaced,
            store=state.store,
        )
    )
    record.task = task
    asyncio.create_task(
        _sync_spatial_status(
            record,
            anchor.spatial_id,
            requires_verified_change=requires_verified_change,
            change_evidence=change_evidence,
        )
    )
    service = request.app.state.desktop_service
    service.attach_run_sync(record)
    return record


async def _sync_spatial_status(
    record: Any,
    spatial_id: str,
    *,
    requires_verified_change: bool = False,
    change_evidence: dict[str, Any] | None = None,
) -> None:
    """把技术终态映射为领域终态；写 run 需要已复核的 changed 证据。"""
    try:
        await record.task
    except asyncio.CancelledError:
        pass
    from backend.app.desktop.models import DesktopRun

    factory = _session_factory()
    async with factory() as session:
        anchor = await session.get(SpatialAnchor, spatial_id)
        run = await session.get(DesktopRun, record.run_id)
        if run is not None:
            run.status = record.status.value
            run.error = _spatial_run_error(record.error, change_evidence)
        if not anchor or anchor.run_id != record.run_id:
            await session.commit()
            return
        if anchor.status in ("invalid", "dismissed"):
            await session.commit()
            return
        anchor.status = _spatial_terminal_status(
            record.status.value,
            requires_verified_change=requires_verified_change,
            change_evidence=change_evidence,
        )
        await session.commit()


def _spatial_terminal_status(
    run_status: str,
    *,
    requires_verified_change: bool,
    change_evidence: dict[str, Any] | None,
) -> str:
    if run_status == "error":
        return "needs_action"
    if run_status != "success":
        return "deployed"
    if requires_verified_change and not (
        isinstance(change_evidence, dict) and change_evidence.get("changed") is True
    ):
        return "needs_action"
    return "done"


def _spatial_run_error(
    record_error: str | None,
    change_evidence: dict[str, Any] | None,
) -> str | None:
    evidence_error = change_evidence.get("error") if isinstance(change_evidence, dict) else None
    return record_error or evidence_error


def _new_id() -> str:
    import uuid

    return uuid.uuid4().hex


def _normalize_permissions(raw: list[str]) -> list[str]:
    permissions = list(dict.fromkeys(raw))
    invalid = set(permissions) - {"read", "write", "host_command"}
    if invalid:
        raise HTTPException(422, f"未知权限: {', '.join(sorted(invalid))}")
    return permissions


def _is_docx_write(content_ref: str, permissions: list[str]) -> bool:
    return content_ref.lower().endswith(".docx") and "write" in permissions


def _validate_write_scope(content_ref: str, permissions: list[str]) -> None:
    if "write" in permissions and not content_ref.lower().endswith(".docx"):
        raise HTTPException(422, "当前仅支持 DOCX 结构化修改；.doc 及其他载体保持只读")
