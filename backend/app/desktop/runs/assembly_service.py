"""RunAssemblyService 的业务实现与显式依赖端口。"""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable, Protocol

from focus.agents.material_inputs import RunMaterialInputs, project_run_material_context
from focus.runtime.runs.manager import RunRecord
from focus.runtime.runs.worker import run_agent
from langgraph.graph.state import CompiledStateGraph

from backend.app.desktop.capabilities.equipment import _resolve_access_mode
from backend.app.desktop.materials.context import MaterialContextProjector
from backend.app.desktop.materials.policy import _must_view_prompt
from backend.app.desktop.persistence.models import (
    DesktopRun,
    DesktopThread,
    DesktopWorkspace,
)
from backend.app.desktop.runs.contracts import PreparedRun
from backend.app.desktop.runs.orchestration import (
    RunExecutionAssembly,
    RunExecutionResources,
    execute_prepared_run,
)
from backend.app.desktop.runs.orchestration.input_provenance import bind_run_inputs
from backend.app.desktop.runs.policy import _RUN_DISPATCH_EQUIPMENT_KEY
from backend.app.gateway.routers.thread_runs import RunCreateRequest


class RunAssemblyServicePorts(Protocol):
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

    async def _material_context(self, task_id: str) -> tuple[str, str]: ...

    def _model_supports_image_input(self, model_name: str | None) -> bool: ...

    @staticmethod
    def _run_payload(run: DesktopRun) -> dict[str, Any]: ...

    app_config: Any
    bridge: Any
    browsers: Any
    checkpointer: Any
    material_snapshots: Any
    run_manager: Any
    session_factory: Any
    session_patrol: Any
    store: Any


class RunAssemblyService:
    def __init__(self, ports: RunAssemblyServicePorts) -> None:
        self._ports = ports

    async def _start_dispatched_run(self, assembly: RunExecutionAssembly) -> RunRecord:
        ports = self._ports
        resources = RunExecutionResources(
            bridge=ports.bridge,
            run_manager=ports.run_manager,
            checkpointer=ports.checkpointer,
            store=ports.store,
            app_config=ports.app_config,
        )
        async with ports.session_factory() as session:
            run = await session.get(DesktopRun, assembly.run_id)
            if run is None:
                raise LookupError(f"Run 不存在: {assembly.run_id}")
            loop_id = run.loop_id
        if loop_id:
            from backend.app.desktop.patrol.agent_loop.execution.run_execution import (
                LoopRunExecutionBoundary,
            )

            return await LoopRunExecutionBoundary(ports.session_factory).start(
                assembly, resources, execute_prepared_run
            )
        return await execute_prepared_run(
            assembly.body,
            assembly.thread_id,
            resources,
            assembly.agent_factory,
            runner=run_agent,
        )

    async def assemble_run(self, run_id: str) -> RunExecutionAssembly:
        ports = self._ports
        loop_id = None
        async with ports.session_factory() as session:
            run = await session.get(DesktopRun, run_id)
            if run is None:
                raise LookupError(f"Run 不存在: {run_id}")
            task = await session.get(DesktopThread, run.task_id)
            if task is None:
                raise LookupError(f"Run Context 不存在: {run.task_id}")
            workspace = await session.get(DesktopWorkspace, task.workspace_id)
            if workspace is None:
                raise LookupError(f"Run Workspace 不存在: {task.workspace_id}")
            equipment = dict(run.equipment or {})
            execution = dict(equipment.get(_RUN_DISPATCH_EQUIPMENT_KEY) or {})
            if run.status != "pending" or execution.get("agent_role") not in {
                "main",
                "patrol",
                "teammate",
                "worker",
            }:
                raise RuntimeError(
                    f"Run 不可由 durable dispatch 装配: {run.run_id}@{run.status}"
                )
            if execution["agent_role"] == "main" and not run.loop_id:
                saved_mode = (task.ui_state or {}).get("access_mode")
                if saved_mode:
                    equipment["access_mode"] = _resolve_access_mode(saved_mode).value
            workspace_path = str(
                (run.workspace_anchor or {}).get("workspace_path") or workspace.path
            )
            slot_id = (run.workspace_anchor or {}).get("slot_id")
            if run.directive_id and (not slot_id):
                raise RuntimeError(
                    f"Loop directive Run 缺少计划 workspace slot: {run.run_id}"
                )
            if slot_id:
                from backend.app.desktop.workspaces.coordination.models import (
                    WorkspaceSlot,
                )

                slot = await session.get(WorkspaceSlot, slot_id)
                if slot is not None:
                    workspace_path = slot.root_path
            loop_id = run.loop_id
            if loop_id:
                from backend.app.desktop.patrol.agent_loop.execution.run_execution import (
                    LoopRunExecutionBoundary,
                )

                await LoopRunExecutionBoundary(ports.session_factory).validate(run_id)
            prepared = await self._prepare(
                run,
                str(run.execution_thread_id or task.thread_id),
                str(
                    (run.workspace_anchor or {}).get("workspace_id")
                    or workspace.workspace_id
                ),
                workspace_path,
                list(run.input_messages or []),
                str(execution["base_prompt"]),
                equipment,
                run.checkpoint_ns or "",
                execution["agent_role"],
                execution.get("checkpoint_id"),
                bool(execution.get("allow_global_config")),
                swarm_depth=int(execution.get("swarm_depth") or 0),
            )
            if "resume_payload" in execution:
                prepared.body.input = None
                prepared.body.resume = execution["resume_payload"]
            elif run.kind == "main":
                from backend.app.desktop.runs.orchestration.execution_history import (
                    repaired_execution_for_run,
                )

                history = await repaired_execution_for_run(
                    session, run, ports.checkpointer
                )
                if history is not None:
                    prepared.body.context["context_execution_messages"] = history
        if os.name == "nt" and hasattr(ports, "browsers"):
            await ports.browsers.ensure(run.task_id)
        if loop_id:
            from backend.app.desktop.patrol.agent_loop.execution.dispatch import (
                LoopRunWorkspaceBinder,
            )

            await LoopRunWorkspaceBinder(ports.session_factory).bind(
                run_id=run_id,
                loop_id=loop_id,
                body=prepared.body,
                slot_id=slot_id,
                directive_id=run.directive_id,
            )
        if prepared.agent_factory is None:
            raise RuntimeError(f"Run 装配未生成 Agent factory: {run_id}")
        return RunExecutionAssembly(
            run_id=run_id,
            body=prepared.body,
            thread_id=prepared.thread_id,
            agent_factory=prepared.agent_factory,
        )

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
    ) -> PreparedRun:
        ports = self._ports
        run_materials = RunMaterialInputs.from_equipment(equipment)
        if agent_role == "main":
            if any((item.digest != "legacy" for item in run_materials.attached)):
                async with ports.session_factory() as session:
                    run_materials = await ports.material_snapshots.verify(
                        session, run_materials, workspace_path
                    )
                equipment = {**equipment, **run_materials.to_equipment()}
            projection = MaterialContextProjector.project(run_materials, workspace_path)
            material_context = projection.policy_text
        elif agent_role == "patrol" and "_patrol_material_policy" in equipment:
            material_context = equipment["_patrol_material_policy"]
        else:
            material_context, _ = await ports._material_context(run.task_id)
        image_inputs = run_materials.images
        required = list(image_inputs.required)
        if required:
            material_context = material_context + _must_view_prompt(
                [
                    {
                        "material_id": item.material_id,
                        "relative_path": item.relative_path,
                    }
                    for item in required
                ]
            )
        factory = ports._build_agent_factory(
            run.task_id,
            run.agent_id,
            workspace_path,
            equipment,
            base_prompt,
            material_context,
            agent_role,
            preparation_observer=ports.session_patrol.preparation_guard(equipment)
            if agent_role == "patrol"
            else None,
        )
        context = ports._governed_context(
            thread_id=thread_id,
            run=run,
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            permissions=list(equipment.get("permissions") or ["read"]),
            access_mode=equipment.get("access_mode"),
            checkpoint_ns=checkpoint_ns,
            agent_role=agent_role,
            swarm_depth=swarm_depth,
            model_name=equipment.get("model_name") or run.model_name,
            allow_global_config=allow_global_config,
            extras={
                "skills": equipment.get("skills") or [],
                **(
                    {"checkpoint_id": checkpoint_id}
                    if checkpoint_id is not None
                    else {}
                ),
            },
        )
        if agent_role == "main":
            ports._attach_session_mode_resolver(context, run, equipment)
        project_run_material_context(
            context,
            run_materials,
            ports._model_supports_image_input(
                equipment.get("model_name") or run.model_name
            ),
        )
        body = RunCreateRequest(
            input={"messages": bind_run_inputs(run, messages)},
            context=context,
            stream_mode=["messages-tuple", "values"],
        )
        return PreparedRun(
            body=body,
            thread_id=thread_id,
            agent_factory=factory,
            payload=ports._run_payload(run),
        )
