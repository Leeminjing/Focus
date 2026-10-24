"""RunRecoveryService 的业务实现与显式依赖端口。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from fastapi import HTTPException
from focus.agents.commitment.middleware import commitment_subgraph_thread_id
from focus.agents.material_inputs import RunMaterialInputs, project_run_material_context
from focus.security.approval import APPROVAL_TYPE
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.capabilities.prompts import (
    MAIN_SYSTEM_PROMPT as _MAIN_SYSTEM_PROMPT,
)
from backend.app.desktop.capabilities.prompts import assembly_system_prompt
from backend.app.desktop.conversations.compression import compression_recovery_payload
from backend.app.desktop.conversations.interrupts import main_pending_interrupt
from backend.app.desktop.materials.context import MaterialContextProjector
from backend.app.desktop.materials.policy import _must_view_prompt
from backend.app.desktop.persistence.contracts import _MAIN_RUNTIME_EQUIPMENT_KEY
from backend.app.desktop.persistence.ids import new_id
from backend.app.desktop.persistence.models import (
    DesktopRun,
    DesktopThread,
    DesktopWorkspace,
)
from backend.app.desktop.runs.contracts import PreparedRun
from backend.app.desktop.runs.identity import (
    _identity_unregistered,
    identity_from_main_run,
    latest_main_run,
    resolve_main_execution_identity,
)
from backend.app.desktop.runs.must_view_recovery import (
    must_view_recovery_payload,
    validate_must_view_resume,
)
from backend.app.desktop.runs.policy import _ACCESS_DECISIONS
from backend.app.desktop.workspaces.assembly import (
    _ASSEMBLY_THREAD_ID,
    _is_assembly_thread,
)
from backend.app.gateway.routers.thread_runs import RunCreateRequest


class RunRecoveryServicePorts(Protocol):
    def _attach_session_mode_resolver(
        self, context: dict[str, Any], run: DesktopRun, equipment: dict[str, Any]
    ) -> None: ...

    def _build_agent_factory(
        self,
        task_id: str,
        agent_id: str,
        workspace_path: str,
        equipment: dict[str, Any],
        base_prompt: str,
        material_context: str,
        agent_role: str,
        preparation_observer=None,
    ) -> Callable[[], Awaitable[CompiledStateGraph]]: ...

    async def _commitment_recovery_payload(
        self, session: AsyncSession, task: DesktopThread
    ) -> dict[str, Any] | None: ...

    def _freeze_skills(
        self, workspace: str | Path, selected: Any
    ) -> list[dict[str, str]]: ...

    def _governed_context(
        self,
        *,
        thread_id: str,
        run: DesktopRun,
        workspace_id: str,
        workspace_path: str,
        permissions: list[str],
        access_mode: str | None,
        checkpoint_ns: str,
        agent_role: str,
        model_name: str | None,
        allow_global_config: bool,
        extras: dict[str, Any],
        swarm_depth: int = 0,
    ) -> dict[str, Any]: ...

    def _model_supports_image_input(self, model_name: str | None) -> bool: ...

    @staticmethod
    def _normalize_skill_names(value: Any) -> list[str]: ...

    @staticmethod
    def _run_payload(run: DesktopRun) -> dict[str, Any]: ...

    checkpointer: Any
    material_snapshots: Any
    session_factory: Any


class RunRecoveryService:
    def __init__(self, ports: RunRecoveryServicePorts) -> None:
        self._ports = ports

    async def resume_run(
        self,
        thread_id: str,
        resume: dict[str, Any],
        run_identity: dict[str, Any] | None = None,
    ) -> PreparedRun:
        ports = self._ports
        async with ports.session_factory() as session:
            task = await session.scalar(
                select(DesktopThread).where(DesktopThread.thread_id == thread_id)
            )
            if not task:
                raise _identity_unregistered(f"会话 {thread_id}")
            recovery = await ports._commitment_recovery_payload(session, task)
            if recovery is not None:
                self._require_resumable(recovery, "commitment_review")
                return await self._prepare_main_resume(
                    session,
                    task,
                    await self._resume_workspace(session, task),
                    resume,
                    run_identity,
                )
            must_view_recovery = await must_view_recovery_payload(
                session, task, ports.checkpointer
            )
            if must_view_recovery is not None:
                if must_view_recovery["status"] != "resumable":
                    raise HTTPException(409, "必看报告中断当前不可恢复")
                return await self._prepare_main_resume(
                    session,
                    task,
                    await self._resume_workspace(session, task),
                    validate_must_view_resume(resume),
                    run_identity,
                )
            compression_recovery = await compression_recovery_payload(
                session, task, ports.checkpointer
            )
            if compression_recovery is not None:
                if not isinstance(resume, dict) or resume.get("type") != "compression":
                    raise HTTPException(
                        409,
                        {
                            "code": "compression_request_pending",
                            "message": "存在待确认的压缩请求，请以压缩载荷恢复",
                        },
                    )
                self._require_resumable(compression_recovery, "compression_request")
                return await self._prepare_main_resume(
                    session,
                    task,
                    await self._resume_workspace(session, task),
                    resume,
                    run_identity,
                )
            access_recovery = await main_pending_interrupt(
                session, task, ports.checkpointer, APPROVAL_TYPE
            )
            if access_recovery is not None:
                if (
                    not isinstance(resume, dict)
                    or resume.get("decision") not in _ACCESS_DECISIONS
                ):
                    raise HTTPException(
                        409,
                        {
                            "code": "access_review_pending",
                            "message": "存在待批准的本机资源访问，请以批准、拒绝或取消载荷恢复",
                        },
                    )
                self._require_resumable(access_recovery, "access_review")
                return await self._prepare_main_resume(
                    session,
                    task,
                    await self._resume_workspace(session, task),
                    resume,
                    run_identity,
                )
            raise HTTPException(409, "无可恢复的人工决定")

    @staticmethod
    def _require_resumable(recovery: dict[str, Any], code_prefix: str) -> None:
        if recovery["status"] == "resumable":
            return
        processing = recovery["status"] == "processing"
        raise HTTPException(
            409,
            {
                "code": f"{code_prefix}_{recovery['status']}",
                "message": "该人工决定正在处理，请等待当前运行结束"
                if processing
                else "父图已无法恢复该人工决定，请先显式放弃旧流程并重开",
            },
        )

    @staticmethod
    async def _resume_workspace(
        session: AsyncSession, task: DesktopThread
    ) -> DesktopWorkspace:
        workspace = await session.get(DesktopWorkspace, task.workspace_id)
        if not workspace:
            raise HTTPException(404, "工作区不存在")
        return workspace

    async def _prepare_main_resume(
        self,
        session: AsyncSession,
        task: DesktopThread,
        workspace: DesktopWorkspace,
        resume: dict[str, Any],
        run_identity: dict[str, Any] | None = None,
    ) -> PreparedRun:
        ports = self._ports
        interrupted_run = await latest_main_run(session, task)
        execution_identity = identity_from_main_run(task, interrupted_run)
        equipment = dict(
            interrupted_run.equipment
            if interrupted_run is not None and interrupted_run.equipment
            else (task.ui_state or {}).get(_MAIN_RUNTIME_EQUIPMENT_KEY) or {}
        )
        if not equipment:
            skills = ports._normalize_skill_names((task.ui_state or {}).get("skills"))
            equipment = {
                "model_name": None,
                "skills": skills,
                "skill_snapshots": ports._freeze_skills(workspace.path, skills),
                "permissions": ["read"],
            }
        run_materials = RunMaterialInputs.from_equipment(equipment)
        if any((item.digest != "legacy" for item in run_materials.attached)):
            run_materials = await ports.material_snapshots.verify(
                session, run_materials, workspace.path
            )
        equipment.pop("must_view_materials", None)
        equipment.pop("run_image_inputs", None)
        equipment.update(run_materials.to_equipment())
        identity = dict(run_identity or {})
        requested_run_id = str(identity.get("run_id") or new_id())
        run = (
            await session.get(DesktopRun, requested_run_id)
            if identity.get("run_id")
            else None
        )
        if run is not None and run.status != "pending":
            raise HTTPException(409, "自治恢复 Run 已经启动或终止")
        run = run or DesktopRun(
            run_id=requested_run_id,
            task_id=task.task_id,
            agent_id=f"main:{task.task_id}",
            kind="main",
            status="pending",
            input_messages=[],
            model_name=equipment.get("model_name"),
            origin=str(identity.get("origin") or "resume"),
            loop_id=identity.get("loop_id"),
            round_id=identity.get("round_id"),
            action_id=identity.get("action_id"),
            idempotency_key=identity.get("idempotency_key"),
            execution_thread_id=execution_identity.thread_id,
            checkpoint_ns=execution_identity.checkpoint_ns,
            context_revision_id=execution_identity.context_revision_id,
            context_checkpoint_id=interrupted_run.context_checkpoint_id
            if interrupted_run
            else None,
            origin_message_id=interrupted_run.origin_message_id
            if interrupted_run is not None
            else run_materials.origin_message_id,
            equipment=equipment,
            workspace_anchor={
                "workspace_id": workspace.workspace_id,
                "workspace_path": workspace.path,
                **(
                    {"compression_resolution_id": identity["resolution_id"]}
                    if identity.get("resolution_id")
                    else {}
                ),
            },
        )
        session.add(run)
        await session.commit()
        projection = MaterialContextProjector.project(run_materials, workspace.path)
        material_context = projection.policy_text
        memory_base_prompt = (
            assembly_system_prompt(
                workspace.path, global_scope=task.thread_id == _ASSEMBLY_THREAD_ID
            )
            if _is_assembly_thread(task.thread_id)
            else _MAIN_SYSTEM_PROMPT
        )
        required = list(run_materials.images.required)
        if required:
            material_context += _must_view_prompt(
                [
                    {
                        "material_id": item.material_id,
                        "relative_path": item.relative_path,
                    }
                    for item in required
                ]
            )
        factory = ports._build_agent_factory(
            task.task_id,
            run.agent_id,
            workspace.path,
            equipment,
            memory_base_prompt,
            material_context,
            "main",
        )
        context = ports._governed_context(
            thread_id=execution_identity.thread_id,
            run=run,
            workspace_id=workspace.workspace_id,
            workspace_path=workspace.path,
            permissions=list(equipment.get("permissions") or ["read"]),
            access_mode=equipment.get("access_mode"),
            checkpoint_ns=execution_identity.checkpoint_ns,
            agent_role="main",
            model_name=equipment.get("model_name"),
            allow_global_config=task.thread_id == _ASSEMBLY_THREAD_ID,
            extras={"skills": equipment.get("skills") or []},
        )
        if run.origin == "direct_user":
            ports._attach_session_mode_resolver(context, run, equipment)
        project_run_material_context(
            context,
            run_materials,
            ports._model_supports_image_input(equipment.get("model_name")),
        )
        body = RunCreateRequest(
            input=None,
            resume=resume,
            context=context,
            stream_mode=["messages-tuple", "values"],
        )
        return PreparedRun(
            body=body,
            thread_id=execution_identity.thread_id,
            agent_factory=factory,
            payload=ports._run_payload(run),
        )

    async def abandon_commitment(self, thread_id: str) -> dict[str, Any]:
        ports = self._ports
        async with ports.session_factory() as session:
            task = await session.scalar(
                select(DesktopThread).where(DesktopThread.thread_id == thread_id)
            )
            if not task:
                raise HTTPException(404, "任务不存在")
            active = await session.scalar(
                select(DesktopRun).where(
                    DesktopRun.task_id == task.task_id,
                    DesktopRun.agent_id == f"main:{task.task_id}",
                    DesktopRun.status.in_(["pending", "running"]),
                )
            )
            if active:
                raise HTTPException(409, "主 Agent 仍在运行，不能废弃承诺流程")
            execution_identity = await resolve_main_execution_identity(session, task)
        await ports.checkpointer.adelete_thread(
            commitment_subgraph_thread_id(execution_identity.thread_id)
        )
        async with ports.session_factory.begin() as session:
            latest = await latest_main_run(session, task)
            if latest and identity_from_main_run(task, latest) == execution_identity:
                result = dict(latest.workspace_result or {})
                if isinstance(result.get("task_attention"), list):
                    result["task_attention"] = [
                        item
                        for item in result["task_attention"]
                        if item["kind"] != "commitment"
                    ]
                    latest.workspace_result = result
        getattr(ports, "_task_attention_cache", {}).clear()
        return {"ok": True, "thread_id": thread_id}
