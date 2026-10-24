"""RunService 的业务实现与显式依赖端口。"""

from __future__ import annotations

from pathlib import Path
from time import monotonic
from typing import Any, Protocol

from fastapi import HTTPException
from focus.models.telemetry import emit, observe_operation
from focus.security.approval import APPROVAL_TYPE
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.capabilities.equipment import _resolve_access_mode
from backend.app.desktop.capabilities.prompts import (
    MAIN_SYSTEM_PROMPT as _MAIN_SYSTEM_PROMPT,
)
from backend.app.desktop.capabilities.prompts import assembly_system_prompt
from backend.app.desktop.contexts.evolution import ContextRevisionRepository
from backend.app.desktop.conversations.checkpoint_recovery import select_checkpoint_base
from backend.app.desktop.conversations.compression import compression_recovery_payload
from backend.app.desktop.conversations.interrupts import main_pending_interrupt
from backend.app.desktop.memory.service import _MAIN_RUNTIME_MEMORY_KEY
from backend.app.desktop.persistence.contracts import _MAIN_RUNTIME_EQUIPMENT_KEY
from backend.app.desktop.persistence.ids import new_id
from backend.app.desktop.persistence.models import (
    DesktopRun,
    DesktopThread,
    DesktopWorkspace,
)
from backend.app.desktop.runs.contracts import PreparedRun
from backend.app.desktop.runs.estimates import estimate_tokens
from backend.app.desktop.runs.identity import latest_main_run
from backend.app.desktop.runs.material_message import RunMaterialMessageProjector
from backend.app.desktop.runs.materials import RunMaterialRequest
from backend.app.desktop.runs.must_view_recovery import must_view_recovery_payload
from backend.app.desktop.runs.orchestration import (
    RunAdmissionConflict,
    RunAdmissionService,
    RunDispatch,
)
from backend.app.desktop.runs.policy import (
    _RUN_DISPATCH_EQUIPMENT_KEY,
    _TERMINAL_STATUSES,
)
from backend.app.desktop.workspaces.assembly import (
    _ASSEMBLY_THREAD_ID,
    _is_assembly_thread,
)
from backend.app.gateway.routers.thread_runs import RunCreateRequest


class RunServicePorts(Protocol):
    async def _commitment_recovery_payload(
        self, session: AsyncSession, task: DesktopThread
    ) -> dict[str, Any] | None: ...

    async def _execution_entities(
        self, session: AsyncSession, task_id: str
    ) -> tuple[DesktopThread, DesktopWorkspace]: ...

    def _freeze_skills(
        self, workspace: str | Path, selected: Any
    ) -> list[dict[str, str]]: ...

    def _model_supports_image_input(self, model_name: str | None) -> bool: ...

    async def _prepare(
        self,
        run: DesktopRun,
        thread_id: str,
        workspace_id: str,
        workspace_path: str,
        messages: list[dict[str, Any]],
        base_prompt: str,
        equipment: dict[str, Any],
        checkpoint_ns: str,
        agent_role: str,
        checkpoint_id: str | None = None,
        allow_global_config: bool = False,
        swarm_depth: int = 0,
    ) -> PreparedRun: ...

    def _require_model(self, model_name: str | None): ...

    def _validate_model_window(self, model_name: str | None, estimate: int) -> None: ...

    app_config: Any
    checkpointer: Any
    contexts: Any

    async def get_checkpoint_messages(
        self, thread_id: str, checkpoint_ns: str
    ) -> list[dict[str, Any]]: ...

    memory: Any
    run_manager: Any
    run_material_history: Any
    run_materials: Any
    session_factory: Any


class RunService:
    def __init__(self, ports: RunServicePorts) -> None:
        self._ports = ports

    def _admission_service(self) -> RunAdmissionService:
        ports = self._ports
        from backend.app.desktop.patrol.agent_loop.execution.execution_ownership import (
            RunOwnershipPolicy,
        )

        return getattr(ports, "_run_admission", None) or RunAdmissionService(
            RunOwnershipPolicy().admit
        )

    async def run_by_idempotency(self, idempotency_key: str) -> dict[str, Any] | None:
        ports = self._ports
        async with ports.session_factory() as session:
            run = await session.scalar(
                select(DesktopRun).where(DesktopRun.idempotency_key == idempotency_key)
            )
            if run is None:
                return None
            dispatch = await session.scalar(
                select(RunDispatch).where(RunDispatch.run_id == run.run_id)
            )
            return self._run_payload_with_dispatch(run, dispatch)

    async def get_run_payload(self, run_id: str) -> dict[str, Any]:
        ports = self._ports
        async with ports.session_factory() as session:
            run = await session.get(DesktopRun, run_id)
            if run is None:
                raise HTTPException(404, "运行不存在")
            dispatch = await session.scalar(
                select(RunDispatch).where(RunDispatch.run_id == run_id)
            )
            return self._run_payload_with_dispatch(run, dispatch)

    async def get_run_causality(self, run_id: str) -> dict[str, Any]:
        ports = self._ports
        "按持久 Run 身份读取其 Directive 因果链，兼容没有 Directive 的历史 Run。"
        from backend.app.desktop.patrol.agent_loop.execution.directive_causality import (
            DirectiveCausalityQuery,
        )

        async with ports.session_factory() as session:
            run = await session.get(DesktopRun, run_id)
            if run is None:
                raise HTTPException(404, "运行不存在")
            chain = None
            if run.directive_id:
                try:
                    chain = await DirectiveCausalityQuery().read(
                        session, run.directive_id
                    )
                except LookupError as error:
                    raise HTTPException(404, str(error)) from error
            return {"run_id": run.run_id, "causality": chain}

    def _require_run_admission(self) -> None:
        ports = self._ports
        if not getattr(ports.app_config, "context_run_admission", True):
            raise HTTPException(
                503,
                "新 Run admission 已暂停；现有 V1/V2 历史可读，恢复前需确认兼容 execution 分支",
            )

    async def start_main_run(
        self,
        task_id: str,
        message: str | list[dict[str, Any]],
        model_name: str | None,
        permissions: list[str],
        skills: list[str],
        spatial_focus: dict[str, Any] | None = None,
        memory_ids: list[str] | None = None,
        material_inputs: list[RunMaterialRequest] | None = None,
        attached_material_ids: list[str] | None = None,
        must_view_material_ids: list[str] | None = None,
        access_mode: str | None = None,
        run_identity: dict[str, Any] | None = None,
        execution_workspace_path: str | None = None,
        execution_slot_id: str | None = None,
        admit_only: bool = False,
        message_reference: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
    ) -> PreparedRun:
        ports = self._ports
        identity = run_identity or {}
        if identity.get("directive_id") and (
            not identity.get("loop_id") or not execution_slot_id
        ):
            raise ValueError(
                "Loop directive Run 必须携带 Loop identity 与计划 workspace slot"
            )
        async with ports.session_factory() as session:
            idempotency_key = identity.get("idempotency_key")
            if idempotency_key:
                existing = await session.scalar(
                    select(DesktopRun).where(
                        DesktopRun.idempotency_key == idempotency_key
                    )
                )
                if existing is not None:
                    return PreparedRun(
                        body=RunCreateRequest(
                            input={"messages": []}, context={"run_id": existing.run_id}
                        ),
                        thread_id=str(existing.execution_thread_id or ""),
                        agent_factory=None,
                        payload=self._run_payload(existing),
                    )
            task_row, workspace = await ports._execution_entities(session, task_id)
            if identity.get("loop_id") and (not execution_slot_id):
                from backend.app.desktop.patrol.agent_loop.models import AgentLoop
                from backend.app.desktop.workspaces.coordination.models import (
                    WorkspaceSlot,
                )

                loop = await session.get(AgentLoop, identity["loop_id"])
                if loop is None or loop.workspace_id != workspace.workspace_id:
                    raise LookupError("Loop Run workspace 身份不匹配")
                slot = await session.scalar(
                    select(WorkspaceSlot).where(
                        WorkspaceSlot.workspace_id == workspace.workspace_id,
                        WorkspaceSlot.kind == "authoritative",
                        WorkspaceSlot.lifecycle == "active",
                    )
                )
                if slot is None:
                    raise LookupError("Loop Run 缺少可用权威 workspace slot")
                execution_slot_id = slot.slot_id
                execution_workspace_path = slot.root_path
            await ports.contexts.ensure_runnable(session, task_id)
            current_revision = await ContextRevisionRepository().current(
                session, task_id
            )
            revision_ref = (
                current_revision.ref if current_revision is not None else None
            )
            execution_thread_id = (
                revision_ref.execution_thread_id
                if revision_ref is not None
                else task_row.thread_id
            )
            execution_checkpoint_ns = (
                revision_ref.checkpoint_ns if revision_ref is not None else ""
            )
            authoritative_checkpoint_id = (
                revision_ref.checkpoint_id if revision_ref is not None else None
            )
            recovery = await ports._commitment_recovery_payload(session, task_row)
            if recovery is not None:
                await self._project_loop_pending_decision(
                    identity, "commitment", recovery
                )
                raise HTTPException(
                    409,
                    {
                        "code": "commitment_review_pending",
                        "recovery": recovery["status"],
                        "message": "存在尚未处理的承诺审批，请先继续审批或显式放弃旧流程",
                    },
                )
            compression_recovery = await compression_recovery_payload(
                session, task_row, ports.checkpointer
            )
            if compression_recovery is not None:
                await self._project_loop_pending_decision(
                    identity, "compression", compression_recovery
                )
                raise HTTPException(
                    409,
                    {
                        "code": "compression_request_pending",
                        "recovery": compression_recovery["status"],
                        "message": "存在待确认的压缩请求，请先完成压缩或取消后继续",
                    },
                )
            must_view_recovery = await must_view_recovery_payload(
                session, task_row, ports.checkpointer
            )
            if must_view_recovery is not None:
                await self._project_loop_pending_decision(
                    identity, "must_view", must_view_recovery
                )
                raise HTTPException(
                    409,
                    {
                        "code": "must_view_report_pending",
                        "recovery": must_view_recovery["status"],
                        "request": must_view_recovery["request"],
                        "message": "存在待处理的必看报告，请先重试或取消当前运行",
                    },
                )
            access_recovery = await main_pending_interrupt(
                session, task_row, ports.checkpointer, APPROVAL_TYPE
            )
            if access_recovery is not None:
                source_run = await latest_main_run(session, task_row)
                source = (
                    {"task_id": task_row.task_id, "run_id": source_run.run_id}
                    if source_run is not None
                    else None
                )
                await self._project_loop_pending_decision(
                    identity, "access_approval", access_recovery, source=source
                )
                raise HTTPException(
                    409,
                    {
                        "code": "access_review_pending",
                        "recovery": access_recovery["status"],
                        "message": "存在待批准的本机资源访问请求，请先批准或拒绝后继续",
                    },
                )
            execution_path = (
                self._execution_workspace_path(execution_workspace_path)
                if execution_workspace_path is not None
                else workspace.path
            )
            snapshots = ports._freeze_skills(execution_path, skills)
            run_id = str(identity.get("run_id") or new_id())
            message_id = str(identity.get("message_id") or new_id())
            run_materials = await ports.run_materials.resolve(
                session,
                task_id,
                workspace.path,
                run_id,
                message_id,
                material_inputs,
                attached_material_ids,
                must_view_material_ids or [],
                execution_workspace_path=execution_path,
            )
            if run_materials.images.attached and (
                not ports._model_supports_image_input(model_name)
            ):
                raise HTTPException(422, "当前执行模型未声明支持图像输入")
            model = ports._require_model(model_name)
            model_name = model.name
            if (
                reasoning_effort is not None
                and reasoning_effort not in model.reasoning_efforts
            ):
                raise HTTPException(422, "当前模型不支持所选思考强度，请重新选择")
            equipment = {
                "model_name": model_name,
                **(
                    {"reasoning_effort": reasoning_effort}
                    if reasoning_effort is not None
                    else {}
                ),
                "skills": list(dict.fromkeys(skills)),
                "skill_snapshots": snapshots,
                "permissions": permissions,
                "access_mode": str(_resolve_access_mode(access_mode)),
                "spatial_focus": spatial_focus,
                **run_materials.to_equipment(),
            }
            history = await ports.get_checkpoint_messages(
                execution_thread_id, execution_checkpoint_ns
            )
            current_message = RunMaterialMessageProjector.project(
                message, run_materials
            )
            from backend.app.desktop.conversations.message_reference import (
                project_message_reference,
            )

            current_message = project_message_reference(
                current_message, message_reference
            )
            checkpoint_id = authoritative_checkpoint_id or await select_checkpoint_base(
                ports.checkpointer, execution_thread_id, execution_checkpoint_ns
            )
            is_assembly = _is_assembly_thread(task_row.thread_id)
            is_global_assembly = task_row.thread_id == _ASSEMBLY_THREAD_ID
            base_prompt = (
                assembly_system_prompt(execution_path, global_scope=is_global_assembly)
                if is_assembly
                else _MAIN_SYSTEM_PROMPT
            )
            equipment["memory_snapshots"] = await ports.memory.freeze_selection(
                memory_ids
            )
            equipment[_RUN_DISPATCH_EQUIPMENT_KEY] = {
                "agent_role": "main",
                "base_prompt": base_prompt,
                "checkpoint_id": checkpoint_id,
                "allow_global_config": is_global_assembly,
            }
            ports._validate_model_window(
                model_name,
                estimate_tokens(
                    _MAIN_SYSTEM_PROMPT,
                    [*history, current_message],
                    "",
                    run_materials.images,
                ),
            )
            self._require_run_admission()
            run = DesktopRun(
                run_id=run_id,
                task_id=task_id,
                agent_id=f"main:{task_id}",
                kind="main",
                status="pending",
                input_messages=[current_message],
                model_name=model_name,
                origin=str(identity.get("origin") or "direct_user"),
                execution_thread_id=execution_thread_id,
                checkpoint_ns=execution_checkpoint_ns,
                context_revision_id=task_row.current_revision_id,
                context_checkpoint_id=authoritative_checkpoint_id,
                origin_message_id=message_id,
                equipment=equipment,
                directive_id=identity.get("directive_id"),
                loop_id=identity.get("loop_id"),
                user_intent_id=identity.get("user_intent_id"),
                round_id=identity.get("round_id"),
                action_id=identity.get("action_id"),
                idempotency_key=identity.get("idempotency_key"),
                workspace_anchor={
                    "workspace_id": workspace.workspace_id,
                    "workspace_path": execution_path,
                    **({"slot_id": execution_slot_id} if execution_slot_id else {}),
                },
            )
            try:
                admission = await self._admission_service().admit(session, run)
            except RunAdmissionConflict as exc:
                raise HTTPException(
                    409, {"code": "main_run_active", "message": str(exc)}
                ) from exc
            if not admission.created:
                await session.rollback()
                return PreparedRun(
                    body=RunCreateRequest(
                        input={"messages": []}, context={"run_id": admission.run.run_id}
                    ),
                    thread_id=str(admission.run.execution_thread_id or ""),
                    agent_factory=None,
                    payload=self._run_payload(admission.run),
                )
            ports.run_material_history.add(session, run, run_materials)
            await session.refresh(task_row, with_for_update=True)
            task_row.ui_state = {
                **(task_row.ui_state or {}),
                _MAIN_RUNTIME_EQUIPMENT_KEY: equipment,
            }
            if memory_ids is not None:
                state = dict(task_row.ui_state or {})
                state[_MAIN_RUNTIME_MEMORY_KEY] = list(dict.fromkeys(memory_ids))
                task_row.ui_state = state
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                active = await session.scalar(
                    select(DesktopRun).where(
                        DesktopRun.task_id == task_id,
                        DesktopRun.execution_thread_id == execution_thread_id,
                        DesktopRun.checkpoint_ns == execution_checkpoint_ns,
                        DesktopRun.kind == "main",
                        DesktopRun.status.in_(["pending", "running"]),
                    )
                )
                if active is not None:
                    raise HTTPException(
                        409,
                        {
                            "code": "main_run_active",
                            "run_id": active.run_id,
                            "message": "当前 Context 已有主 Agent 正在运行",
                        },
                    ) from exc
                raise
            thread_id = execution_thread_id
        if admit_only:
            return PreparedRun(
                body=RunCreateRequest(
                    input={"messages": []}, context={"run_id": run.run_id}
                ),
                thread_id=thread_id,
                agent_factory=None,
                payload={
                    **self._run_payload(run),
                    "dispatch": {
                        "dispatch_id": admission.dispatch.dispatch_id,
                        "status": admission.dispatch.status,
                        "attempt": admission.dispatch.attempt,
                    },
                },
            )
        prepared = await ports._prepare(
            run,
            thread_id,
            str((run.workspace_anchor or {}).get("workspace_id") or ""),
            str((run.workspace_anchor or {}).get("workspace_path") or ""),
            list(run.input_messages or []),
            base_prompt,
            equipment,
            execution_checkpoint_ns,
            "main",
            checkpoint_id,
            allow_global_config=is_global_assembly,
        )
        prepared.payload["dispatch"] = {
            "dispatch_id": admission.dispatch.dispatch_id,
            "status": admission.dispatch.status,
            "attempt": admission.dispatch.attempt,
        }
        return prepared

    async def _project_loop_pending_decision(
        self,
        identity: dict[str, Any],
        kind: str,
        recovery: dict[str, Any],
        *,
        source: dict[str, Any] | None = None,
    ) -> None:
        ports = self._ports
        loop_id = identity.get("loop_id")
        projector = getattr(ports, "pending_decision_projector", None)
        if loop_id and projector is not None:
            await projector.project(
                str(loop_id),
                {
                    "type": kind,
                    "recovery": recovery,
                    **({"source": source} if source is not None else {}),
                },
            )

    @staticmethod
    def _execution_workspace_path(value: str) -> str:
        path = Path(value).resolve(strict=True)
        if not path.is_dir():
            raise HTTPException(422, "执行 Workspace Slot 路径不是目录")
        return str(path)

    @observe_operation("run_cancel_request")
    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        ports = self._ports
        started = monotonic()
        record = ports.run_manager.get(run_id)
        accepted = ports.run_manager.cancel(run_id)
        if (
            accepted
            and record is not None
            and (record.task is not None)
            and (not record.task.done())
        ):
            record.task.add_done_callback(
                lambda _task: emit(
                    "run_cancelled",
                    run_id=run_id,
                    local_completion_ms=(monotonic() - started) * 1000,
                )
            )
        async with ports.session_factory() as session:
            run = await session.get(DesktopRun, run_id)
            if not run:
                raise HTTPException(404, "运行不存在")
            if run.status not in _TERMINAL_STATUSES:
                run.status = "interrupted"
                await session.commit()
            return self._run_payload(run)

    async def get_run(self, run_id: str) -> DesktopRun:
        ports = self._ports
        async with ports.session_factory() as session:
            run = await session.get(DesktopRun, run_id)
            if not run:
                raise HTTPException(404, "运行不存在")
            return run

    @staticmethod
    def _run_payload(run: DesktopRun) -> dict[str, Any]:
        return {
            "run_id": run.run_id,
            "task_id": run.task_id,
            "agent_id": run.agent_id,
            "deployment_id": run.deployment_id,
            "kind": run.kind,
            "status": run.status,
            "model_name": run.model_name,
            "error": run.error,
            "model_call_count": run.model_call_count,
            "prompt_input_tokens": run.prompt_input_tokens,
            "prompt_output_tokens": run.prompt_output_tokens,
            "prompt_cache_hit_tokens": run.prompt_cache_hit_tokens,
            "message_id": run.origin_message_id,
            "origin": run.origin,
            "execution_thread_id": run.execution_thread_id,
            "checkpoint_ns": run.checkpoint_ns,
            "context_revision_id": run.context_revision_id,
            "context_checkpoint_id": run.context_checkpoint_id,
            "directive_id": run.directive_id,
            "loop_id": run.loop_id,
            "round_id": run.round_id,
            "action_id": run.action_id,
            "equipment": run.equipment,
            "workspace_anchor": run.workspace_anchor,
            "idempotency_key": run.idempotency_key,
            "final_checkpoint_id": run.final_checkpoint_id,
            "workspace_result": run.workspace_result,
            "settled_at": run.settled_at.isoformat() if run.settled_at else None,
        }

    @classmethod
    def _run_payload_with_dispatch(
        cls, run: DesktopRun, dispatch: RunDispatch | None
    ) -> dict[str, Any]:
        payload = cls._run_payload(run)
        payload["dispatch"] = (
            None
            if dispatch is None
            else {
                "dispatch_id": dispatch.dispatch_id,
                "status": dispatch.status,
                "attempt": dispatch.attempt,
                "error": dispatch.error,
                "claimed_by": dispatch.claimed_by,
                "fencing_token": dispatch.fencing_token,
            }
        )
        return payload
