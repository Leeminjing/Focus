"""WorkspaceService 的业务实现与显式依赖端口。"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Protocol

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.capabilities.equipment import _resolve_access_mode
from backend.app.desktop.persistence.ids import new_id
from backend.app.desktop.persistence.models import DesktopThread, DesktopWorkspace
from backend.app.desktop.runs.identity import _identity_unregistered
from backend.app.desktop.workspaces.assembly import (
    _ASSEMBLY_THREAD_ID,
    _ASSEMBLY_WORKSPACE_DISPLAY,
)

logger = logging.getLogger(__name__)


class WorkspaceServicePorts(Protocol):
    async def _task_payload(
        self, session: AsyncSession, task: DesktopThread, workspace: DesktopWorkspace
    ) -> dict[str, Any]: ...

    session_factory: Any


class WorkspaceService:
    def __init__(self, ports: WorkspaceServicePorts) -> None:
        self._ports = ports

    async def create_workspace(
        self, path: str, display_name: str | None = None
    ) -> dict[str, Any]:
        ports = self._ports
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise HTTPException(422, "工作区必须是已存在的本地文件夹")
        managed_app = os.getenv("FOCUS_MANAGED_APP")
        if managed_app:
            managed_root = Path(managed_app).expanduser().resolve()
            if resolved == managed_root or managed_root in resolved.parents:
                raise HTTPException(422, "工作区不能位于 Focus 托管程序目录内")
        normalized = os.path.normpath(str(resolved))
        async with ports.session_factory() as session:
            existing = await session.scalar(
                select(DesktopWorkspace).where(DesktopWorkspace.path == normalized)
            )
            if existing:
                return self._workspace_payload(existing)
            workspace = DesktopWorkspace(
                workspace_id=new_id(),
                path=normalized,
                display_name=display_name or resolved.name or normalized,
            )
            session.add(workspace)
            await session.commit()
            return self._workspace_payload(workspace)

    async def create_thread(
        self,
        workspace_id: str,
        thread_id: str | None,
        title: str,
        access_mode: str | None = None,
    ) -> dict[str, Any]:
        ports = self._ports
        async with ports.session_factory() as session:
            workspace = await session.get(DesktopWorkspace, workspace_id)
            if not workspace:
                raise HTTPException(404, "工作区不存在")
            identity = thread_id or new_id()
            existing = await session.scalar(
                select(DesktopThread).where(
                    DesktopThread.workspace_id == workspace_id,
                    DesktopThread.thread_id == identity,
                )
            )
            if existing:
                return await ports._task_payload(session, existing, workspace)
            task = DesktopThread(
                task_id=new_id(),
                workspace_id=workspace_id,
                thread_id=identity,
                title=title,
                ui_state={"access_mode": str(_resolve_access_mode(access_mode))},
            )
            session.add(task)
            await session.commit()
            return await ports._task_payload(session, task, workspace)

    async def ensure_assembly_task(
        self, workspace_id: str | None = None
    ) -> dict[str, Any]:
        ports = self._ports
        async with ports.session_factory() as session:
            if workspace_id:
                workspace = await session.get(DesktopWorkspace, workspace_id)
                if workspace is None:
                    raise HTTPException(404, "目标工作区不存在")
                if not Path(workspace.path).is_dir():
                    raise HTTPException(422, "目标工作区目录不存在")
            else:
                workspace = await self._ensure_assembly_workspace(session)
            from focus.config.layered import global_home

            global_scope = Path(workspace.path).resolve() == global_home().resolve()
            thread = await self._ensure_assembly_thread(
                session, workspace, global_scope=global_scope
            )
            await session.commit()
            return await ports._task_payload(session, thread, workspace)

    async def _ensure_assembly_workspace(
        self, session: AsyncSession
    ) -> DesktopWorkspace:
        from focus.config.layered import global_home

        path = str(global_home().resolve())
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("无法创建全局配置家目录 %s（可能受文件沙箱限制）", path)
        workspace = await session.scalar(
            select(DesktopWorkspace).where(DesktopWorkspace.path == path)
        )
        if workspace is None:
            workspace = DesktopWorkspace(
                workspace_id=new_id(),
                path=path,
                display_name=_ASSEMBLY_WORKSPACE_DISPLAY,
            )
            session.add(workspace)
            await session.flush()
        return workspace

    async def _ensure_assembly_thread(
        self,
        session: AsyncSession,
        workspace: DesktopWorkspace,
        *,
        global_scope: bool = True,
    ) -> DesktopThread:
        thread_id = (
            _ASSEMBLY_THREAD_ID
            if global_scope
            else f"{_ASSEMBLY_THREAD_ID}:{workspace.workspace_id}"
        )
        thread = await session.scalar(
            select(DesktopThread).where(
                DesktopThread.workspace_id == workspace.workspace_id,
                DesktopThread.thread_id == thread_id,
            )
        )
        if thread is None:
            thread = DesktopThread(
                task_id=new_id(),
                workspace_id=workspace.workspace_id,
                thread_id=thread_id,
                title="无工作区模式"
                if global_scope
                else f"装配 · {workspace.display_name}",
            )
            session.add(thread)
            await session.flush()
        return thread

    @staticmethod
    def _workspace_payload(workspace: DesktopWorkspace) -> dict[str, Any]:
        return {
            "workspace_id": workspace.workspace_id,
            "path": workspace.path,
            "display_name": workspace.display_name,
        }

    async def _get_task_entities(
        self, session: AsyncSession, task_id: str
    ) -> tuple[DesktopThread, DesktopWorkspace]:
        task = await session.get(DesktopThread, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        workspace = await session.get(DesktopWorkspace, task.workspace_id)
        if not workspace:
            raise HTTPException(404, "工作区不存在")
        return (task, workspace)

    async def _execution_entities(
        self, session: AsyncSession, task_id: str
    ) -> tuple[DesktopThread, DesktopWorkspace]:
        try:
            return await self._get_task_entities(session, task_id)
        except HTTPException as exc:
            raise _identity_unregistered(f"任务 {task_id}") from exc
