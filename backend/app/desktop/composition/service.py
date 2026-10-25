"""桌面组合根：装配资源与领域端口，协调启动、关闭和持久派发。"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from functools import cached_property
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import UploadFile
from focus.config.app_config import AppConfig
from focus.runtime.runs.manager import RunManager, RunRecord
from focus.runtime.stream_bridge.base import StreamBridge
from focus.security.policy import AccessMode
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agents.collaboration import AgentCollab
from backend.app.desktop.agents.service import CollaborationService
from backend.app.desktop.api.schemas import (
    ContextCurationPolicy,
    DraftUpdate,
    MaterialCreate,
    MaterialUpdate,
)
from backend.app.desktop.capabilities.service import CapabilityService
from backend.app.desktop.contexts.service import ContextService
from backend.app.desktop.conversations.compression_service import CompressionService
from backend.app.desktop.conversations.service import ConversationService
from backend.app.desktop.materials.content import (
    MaterialContentService,
    ResolvedMaterialContent,
)
from backend.app.desktop.materials.groups import MaterialGroupService
from backend.app.desktop.materials.limits import ImageResourceLimits
from backend.app.desktop.materials.service import MaterialService
from backend.app.desktop.materials.snapshots import MaterialSnapshotVerifier
from backend.app.desktop.materials.upload import MaterialUploadService
from backend.app.desktop.memory.service import MemoryService
from backend.app.desktop.patrol.context_service import ContextPatrolService
from backend.app.desktop.patrol.service import PatrolService
from backend.app.desktop.persistence.models import (
    DesktopMaterial,
    DesktopRun,
    DesktopThread,
    DesktopWorkspace,
    MaterialVersion,
    PatrolDraft,
)
from backend.app.desktop.persistence.state_changes import DesktopStateChanges
from backend.app.desktop.runs.agent_factory import AgentFactory
from backend.app.desktop.runs.assembly_service import RunAssemblyService
from backend.app.desktop.runs.contracts import PreparedRun
from backend.app.desktop.runs.images import RunImageResolver
from backend.app.desktop.runs.lifecycle_service import RunLifecycleService
from backend.app.desktop.runs.material_history import RunMaterialHistoryRepository
from backend.app.desktop.runs.materials import RunMaterialRequest, RunMaterialResolver
from backend.app.desktop.runs.orchestration import (
    DurableRunDispatchWorker,
    RunAdmissionService,
    RunDispatch,
    RunDispatchRecovery,
    RunDispatchRepository,
    RunExecutionAssembler,
    RunExecutionAssembly,
    RunLifecycleFinalizer,
)
from backend.app.desktop.runs.recovery_service import RunRecoveryService
from backend.app.desktop.runs.security_service import RunSecurityService
from backend.app.desktop.runs.service import RunService
from backend.app.desktop.workspaces.service import WorkspaceService

logger = logging.getLogger(__name__)


class DesktopService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        checkpointer: Any,
        store: Any,
        bridge: StreamBridge,
        app_config: AppConfig,
        run_manager: RunManager,
    ) -> None:
        self.session_factory = session_factory
        self.checkpointer = checkpointer
        self.store = store
        self.bridge = bridge
        self.app_config = app_config
        self.run_manager = run_manager
        from backend.app.desktop.browser.service import ContextBrowsers

        self.browsers = ContextBrowsers(session_factory, bridge)
        self.image_limits = ImageResourceLimits()
        self.material_uploads = MaterialUploadService(
            session_factory, self.image_limits
        )
        self.material_contents = MaterialContentService(
            session_factory, self.image_limits
        )
        self.run_images = RunImageResolver(self.image_limits)
        self.run_materials = RunMaterialResolver(self.image_limits)
        self.run_material_history = RunMaterialHistoryRepository()
        self.material_groups = MaterialGroupService(session_factory)
        self.material_snapshots = MaterialSnapshotVerifier()
        self.contexts = ContextService(session_factory, checkpointer, app_config)
        from backend.app.desktop.patrol.session_patrol.deployment import (
            PatrolDeploymentCoordinator,
        )
        from backend.app.desktop.patrol.session_patrol.sources import (
            PatrolSourceResolver,
        )

        self.session_patrol = PatrolDeploymentCoordinator(self)
        self.patrol_sources = PatrolSourceResolver(self)
        self.context_patrol = ContextPatrolService(
            session_factory,
            self.contexts,
            checkpointer,
            store,
            bridge,
            run_manager,
            app_config,
        )
        from backend.app.desktop.capabilities.secrets import configured_secret_values

        self.run_lifecycle = RunLifecycleFinalizer(
            session_factory,
            checkpointer,
            secret_provider=lambda: configured_secret_values(self.app_config),
        )
        from backend.app.desktop.runs.execution_attempts.shell_recovery import (
            ShellCleanupRecovery,
        )

        self.shell_recovery = ShellCleanupRecovery(
            session_factory, self.run_lifecycle, run_manager
        )
        self._run_admission = self._admission_service()
        self._run_dispatch_repository = RunDispatchRepository()
        self._run_dispatch_recovery = RunDispatchRecovery(session_factory)
        self._run_dispatch_wakeup = asyncio.Event()
        from backend.app.desktop.conversations.inputs import ConversationInputs

        self.conversation_inputs = ConversationInputs(self)
        self._run_dispatch_worker = DurableRunDispatchWorker(
            session_factory,
            f"desktop-main:{uuid.uuid4().hex}",
            RunExecutionAssembler(self),
            self._start_dispatched_run,
            self._run_dispatch_repository,
            on_started=self.attach_run_sync,
            on_failed=self.run_lifecycle.abort_prepared,
        )
        self._run_dispatch_task: asyncio.Task | None = None
        self.agent_collab = AgentCollab(
            session_factory, swarm_launcher=self._auto_wake_swarm
        )
        self.memory = MemoryService(
            session_factory, app_config, self._memory_context_messages
        )
        self._sync_tasks: set[asyncio.Task] = set()
        self._run_sync_tasks: dict[str, asyncio.Task] = {}
        self._pending_run_finalizations: dict[str, RunRecord] = {}
        self._closing = False
        self._watcher: asyncio.Task | None = None
        self._material_watch_failures: set[str] = set()

    def _admission_service(self) -> RunAdmissionService:
        return self.runs._admission_service()

    async def start(self) -> None:
        await self.browsers.start()
        self.state_changes = DesktopStateChanges(self.session_factory)
        self.state_changes.start()
        self._performance = None
        if os.environ.get("FOCUS_PERFORMANCE_METRICS") == "1":
            from backend.app.desktop.persistence.performance import (
                DesktopPerformanceMonitor,
            )

            self._performance = DesktopPerformanceMonitor(
                self.session_factory.kw["bind"]
            )
            self._performance.start()
        dispatch_recovery = await self._run_dispatch_recovery.reconcile()
        await self.run_lifecycle.reconcile_active(
            "桌面后端重启，原运行无法继续",
            exclude_run_ids=set(dispatch_recovery.safe_run_ids),
        )
        await self.context_patrol.start()
        self._watcher = asyncio.create_task(self._watch_materials())
        self._run_dispatch_task = asyncio.create_task(
            self._run_dispatch_loop(), name="desktop-main-run-dispatch"
        )
        self._run_dispatch_wakeup.set()

    async def close(self) -> None:
        self._closing = True
        if hasattr(self, "browsers"):
            await self.browsers.close()
        await self.context_patrol.close()
        background = list(self._sync_tasks)
        if self._run_dispatch_task:
            self._run_dispatch_task.cancel()
            background.append(self._run_dispatch_task)
            self._run_dispatch_task = None
        if self._watcher:
            self._watcher.cancel()
            background.append(self._watcher)
        for task in background:
            if not task.done():
                task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        if hasattr(self, "state_changes"):
            await self.state_changes.close()
        if getattr(self, "_performance", None) is not None:
            await self._performance.close()

    def notify_run_dispatch(self) -> None:
        self._run_dispatch_wakeup.set()

    async def _run_dispatch_loop(self) -> None:
        while True:
            self._run_dispatch_wakeup.clear()
            self._resume_pending_run_finalizations()
            try:
                await self.conversation_inputs.drain()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ordinary conversation input dispatch cycle failed")
            try:
                await self._run_dispatch_worker.drain(limit=4)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("durable Main Run dispatch cycle failed")
            try:
                await asyncio.wait_for(self._run_dispatch_wakeup.wait(), timeout=0.5)
            except TimeoutError:
                continue

    async def _start_dispatched_run(self, assembly: RunExecutionAssembly) -> RunRecord:
        return await self.run_assembly._start_dispatched_run(assembly)

    async def assemble_run(self, run_id: str) -> RunExecutionAssembly:
        return await self.run_assembly.assemble_run(run_id)

    async def equipment(self, *, include_tools: bool = True) -> dict[str, Any]:
        return await self.capabilities.equipment(include_tools=include_tools)

    async def run_by_idempotency(self, idempotency_key: str) -> dict[str, Any] | None:
        return await self.runs.run_by_idempotency(idempotency_key)

    async def get_run_payload(self, run_id: str) -> dict[str, Any]:
        return await self.runs.get_run_payload(run_id)

    async def get_run_causality(self, run_id: str) -> dict[str, Any]:
        return await self.runs.get_run_causality(run_id)

    async def _equipment_tools(self) -> list[dict[str, Any]]:
        return await self.capabilities._equipment_tools()

    async def create_workspace(
        self, path: str, display_name: str | None = None
    ) -> dict[str, Any]:
        return await self.workspaces.create_workspace(path, display_name)

    async def create_thread(
        self,
        workspace_id: str,
        thread_id: str | None,
        title: str,
        access_mode: str | None = None,
    ) -> dict[str, Any]:
        return await self.workspaces.create_thread(
            workspace_id, thread_id, title, access_mode
        )

    async def list_tasks(self) -> list[dict[str, Any]]:
        return await self.conversations.list_tasks()

    async def _task_attention(self, run, task, persisted=None):
        return await self.conversations._task_attention(run, task, persisted)

    async def _add_loop_task_summaries(self, session, summaries):
        return await self.conversations._add_loop_task_summaries(session, summaries)

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self.conversations.get_task(task_id)

    async def _memory_context_messages(self, context_id: str) -> list[dict[str, Any]]:
        return await self.conversations._memory_context_messages(context_id)

    async def list_task_skills(self, task_id: str) -> list[dict[str, str]]:
        return await self.capabilities.list_task_skills(task_id)

    async def refresh_task_capabilities(self, task_id: str) -> dict[str, Any]:
        return await self.capabilities.refresh_task_capabilities(task_id)

    async def save_ui_state(self, task_id: str, ui_state: dict[str, Any]) -> None:
        return await self.conversations.save_ui_state(task_id, ui_state)

    async def set_session_access_mode(self, task_id: str, access_mode: str) -> None:
        return await self.conversations.set_session_access_mode(task_id, access_mode)

    async def get_checkpoint_messages(
        self, thread_id: str, checkpoint_ns: str
    ) -> list[dict[str, Any]]:
        return await self.conversations.get_checkpoint_messages(
            thread_id, checkpoint_ns
        )

    async def _compression_execution_identity(
        self, session: AsyncSession, task: DesktopThread
    ) -> tuple[str, str]:
        return await self.compression._compression_execution_identity(session, task)

    async def get_compression_messages(self, task_id: str) -> list[dict[str, Any]]:
        return await self.compression.get_compression_messages(task_id)

    async def quick_compression_preview(
        self, task_id: str, keyword: str, case_sensitive: bool = False
    ) -> dict[str, Any]:
        return await self.compression.quick_compression_preview(
            task_id, keyword, case_sensitive
        )

    async def quick_compression_apply(
        self,
        task_id: str,
        ranges: list[dict[str, Any]],
        scrub_terms: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self.compression.quick_compression_apply(
            task_id, ranges, scrub_terms
        )

    async def open_draft(self, task_id: str) -> dict[str, Any]:
        return await self.patrol.open_draft(task_id)

    async def update_draft(self, draft_id: str, body: DraftUpdate) -> dict[str, Any]:
        return await self.patrol.update_draft(draft_id, body)

    async def quick_deploy_context_curator(
        self, task_id: str, deployment_id: str
    ) -> PreparedRun:
        return await self.patrol.quick_deploy_context_curator(task_id, deployment_id)

    async def _prepare_context_curator_draft(
        self, draft: PatrolDraft, model_name: str | None, policy: ContextCurationPolicy
    ) -> str:
        return await self.patrol._prepare_context_curator_draft(
            draft, model_name, policy
        )

    def _default_curation_model_name(self) -> str:
        return self.patrol._default_curation_model_name()

    async def deploy(
        self, draft_id: str, deployment_id: str, preview_token: str | None = None
    ) -> PreparedRun:
        return await self.patrol.deploy(draft_id, deployment_id, preview_token)

    def _require_run_admission(self) -> None:
        return self.runs._require_run_admission()

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
        return await self.runs.start_main_run(
            task_id,
            message,
            model_name,
            permissions,
            skills,
            spatial_focus,
            memory_ids,
            material_inputs,
            attached_material_ids,
            must_view_material_ids,
            access_mode,
            run_identity,
            execution_workspace_path,
            execution_slot_id,
            admit_only,
            message_reference,
            reasoning_effort,
        )

    async def _project_loop_pending_decision(
        self,
        identity: dict[str, Any],
        kind: str,
        recovery: dict[str, Any],
        *,
        source: dict[str, Any] | None = None,
    ) -> None:
        return await self.runs._project_loop_pending_decision(
            identity, kind, recovery, source=source
        )

    async def ensure_assembly_task(
        self, workspace_id: str | None = None
    ) -> dict[str, Any]:
        return await self.workspaces.ensure_assembly_task(workspace_id)

    async def _ensure_assembly_workspace(
        self, session: AsyncSession
    ) -> DesktopWorkspace:
        return await self.workspaces._ensure_assembly_workspace(session)

    async def _ensure_assembly_thread(
        self,
        session: AsyncSession,
        workspace: DesktopWorkspace,
        *,
        global_scope: bool = True,
    ) -> DesktopThread:
        return await self.workspaces._ensure_assembly_thread(
            session, workspace, global_scope=global_scope
        )

    async def resume_run(
        self,
        thread_id: str,
        resume: dict[str, Any],
        run_identity: dict[str, Any] | None = None,
    ) -> PreparedRun:
        return await self.run_recovery.resume_run(thread_id, resume, run_identity)

    @staticmethod
    def _require_resumable(recovery: dict[str, Any], code_prefix: str) -> None:
        return RunRecoveryService._require_resumable(recovery, code_prefix)

    @staticmethod
    async def _resume_workspace(
        session: AsyncSession, task: DesktopThread
    ) -> DesktopWorkspace:
        return await RunRecoveryService._resume_workspace(session, task)

    async def _prepare_main_resume(
        self,
        session: AsyncSession,
        task: DesktopThread,
        workspace: DesktopWorkspace,
        resume: dict[str, Any],
        run_identity: dict[str, Any] | None = None,
    ) -> PreparedRun:
        return await self.run_recovery._prepare_main_resume(
            session, task, workspace, resume, run_identity
        )

    @staticmethod
    def _execution_workspace_path(value: str) -> str:
        return RunService._execution_workspace_path(value)

    async def abandon_commitment(self, thread_id: str) -> dict[str, Any]:
        return await self.run_recovery.abandon_commitment(thread_id)

    async def retry_agent(self, agent_id: str) -> PreparedRun:
        return await self.patrol.retry_agent(agent_id)

    async def continue_agent(
        self,
        agent_id: str,
        message: str,
        run_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> PreparedRun:
        return await self.patrol.continue_agent(
            agent_id, message, run_id, checkpoint_id
        )

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        return await self.runs.cancel_run(run_id)

    async def list_agents(self, task_id: str) -> list[dict[str, Any]]:
        return await self.patrol.list_agents(task_id)

    async def agent_history(self, agent_id: str) -> list[dict[str, Any]]:
        return await self.patrol.agent_history(agent_id)

    async def get_run(self, run_id: str) -> DesktopRun:
        return await self.runs.get_run(run_id)

    async def enroll_material(
        self, task_id: str, body: MaterialCreate
    ) -> dict[str, Any]:
        return await self.materials.enroll_material(task_id, body)

    async def list_materials(self, task_id: str) -> list[dict[str, Any]]:
        return await self.materials.list_materials(task_id)

    async def list_material_history(
        self, task_id: str, material_id: str | None = None
    ) -> list[dict[str, Any]]:
        return await self.materials.list_material_history(task_id, material_id)

    async def update_material(
        self, material_id: str, body: MaterialUpdate
    ) -> dict[str, Any]:
        return await self.materials.update_material(material_id, body)

    async def clear_material(self, material_id: str) -> dict[str, Any]:
        return await self.materials.clear_material(material_id)

    async def delete_material(self, material_id: str) -> None:
        return await self.materials.delete_material(material_id)

    async def material_versions(self, material_id: str) -> list[dict[str, Any]]:
        return await self.materials.material_versions(material_id)

    async def restore_material(
        self, material_id: str, version_id: str
    ) -> dict[str, Any]:
        return await self.materials.restore_material(material_id, version_id)

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
        return await self.run_assembly._prepare(
            run,
            thread_id,
            workspace_id,
            workspace_path,
            messages,
            base_prompt,
            equipment,
            checkpoint_ns,
            agent_role,
            checkpoint_id,
            allow_global_config,
            swarm_depth,
        )

    def _attach_session_mode_resolver(
        self, context: dict[str, Any], run: DesktopRun, equipment: dict[str, Any]
    ) -> None:
        return self.run_security._attach_session_mode_resolver(context, run, equipment)

    def _round_mode_resolver(self, run_id: str):
        return self.run_security._round_mode_resolver(run_id)

    def _session_mode_resolver(self, task_id: str, initial: AccessMode):
        return self.run_security._session_mode_resolver(task_id, initial)

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
    ) -> dict[str, Any]:
        return self.run_security._governed_context(
            thread_id=thread_id,
            run=run,
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            permissions=permissions,
            access_mode=access_mode,
            checkpoint_ns=checkpoint_ns,
            agent_role=agent_role,
            model_name=model_name,
            allow_global_config=allow_global_config,
            extras=extras,
            swarm_depth=swarm_depth,
        )

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
    ) -> Callable[[], Awaitable[CompiledStateGraph]]:
        return self.agent_factory._build_agent_factory(
            task_id,
            agent_id,
            workspace_path,
            equipment,
            base_prompt,
            material_context,
            agent_role,
            preparation_observer,
        )

    def _model_supports_image_input(self, model_name: str | None) -> bool:
        return self.capabilities._model_supports_image_input(model_name)

    def _compression_context_window(self, model_name: str | None) -> int | None:
        return self.capabilities._compression_context_window(model_name)

    _TEAMMATE_PROMPT = "你是 Focus 的 Teammate，隶属于主 Agent 领导的团队。独立完成任务，任务完成后必须调用 send_message 向主 Agent 汇报结果；重大计划先用 request_plan_approval 请主 Agent 批准；如需结束工作可 request_shutdown。"
    _WORKER_PROMPT = "你是 Focus 的 Worker，服务于主 Agent（Coordinator）的任务板。用 claim_task 认领 pending 任务并执行，完成后用 complete_task 提交结果；任务完成后调用 send_message 向主 Agent 汇报。"

    def _build_swarm_tools(self) -> list[BaseTool]:
        return self.collaboration._build_swarm_tools()

    async def _wait_for_swarm(
        self, task_id: str, agent_ids: list[str], timeout_seconds: int
    ) -> dict[str, Any]:
        return await self.collaboration._wait_for_swarm(
            task_id, agent_ids, timeout_seconds
        )

    async def _swarm_snapshot(
        self, task_id: str, agent_ids: list[str]
    ) -> dict[str, Any]:
        return await self.collaboration._swarm_snapshot(task_id, agent_ids)

    @staticmethod
    def _snapshot_is_busy(snapshot: dict[str, Any]) -> bool:
        return CollaborationService._snapshot_is_busy(snapshot)

    async def _spawn_swarm(
        self, task: str, system_prompt: str | None, role: str, runtime: Any
    ) -> str:
        return await self.collaboration._spawn_swarm(task, system_prompt, role, runtime)

    async def _wake_swarm(
        self, agent_id: str, message: str, context: dict[str, Any]
    ) -> str:
        return await self.collaboration._wake_swarm(agent_id, message, context)

    async def _auto_wake_swarm(self, agent_id: str, message: str, depth: int) -> None:
        return await self.collaboration._auto_wake_swarm(agent_id, message, depth)

    async def _swarm_source(self, run_id: str) -> DesktopRun:
        return await self.collaboration._swarm_source(run_id)

    async def _launch_swarm_run(
        self,
        task_id: str,
        agent_id: str,
        role: str,
        task_text: str,
        system_prompt: str,
        workspace_id: str,
        workspace_path: str,
        equipment: dict[str, Any],
        swarm_depth: int = 0,
        parent_run_id: str | None = None,
    ) -> str:
        return await self.collaboration._launch_swarm_run(
            task_id,
            agent_id,
            role,
            task_text,
            system_prompt,
            workspace_id,
            workspace_path,
            equipment,
            swarm_depth,
            parent_run_id,
        )

    def attach_run_sync(self, record: RunRecord) -> None:
        return self.run_finalization.attach_run_sync(record)

    def _resume_pending_run_finalizations(self) -> None:
        return self.run_finalization._resume_pending_run_finalizations()

    async def _sync_run_status(self, record: RunRecord) -> None:
        return await self.run_finalization._sync_run_status(record)

    async def _finalize_run_status(self, record: RunRecord) -> None:
        return await self.run_finalization._finalize_run_status(record)

    async def _material_context(self, task_id: str) -> tuple[str, str]:
        return await self.materials._material_context(task_id)

    def _build_patrol_reader_tools(self, task_id: str) -> list[BaseTool]:
        return self.patrol._build_patrol_reader_tools(task_id)

    def _build_swarm_reader_tools(self, task_id: str) -> list[BaseTool]:
        return self.collaboration._build_swarm_reader_tools(task_id)

    async def _task_thread_id(self, task_id: str) -> str:
        return await self.conversations._task_thread_id(task_id)

    async def _validate_attachments(
        self, session: AsyncSession, task_id: str, messages: list[dict[str, Any]]
    ) -> None:
        return await self.materials._validate_attachments(session, task_id, messages)

    def _require_model(self, model_name: str | None):
        return self.capabilities._require_model(model_name)

    def _validate_model_window(self, model_name: str | None, estimate: int) -> None:
        return self.capabilities._validate_model_window(model_name, estimate)

    @staticmethod
    def _normalize_skill_names(value: Any) -> list[str]:
        return CapabilityService._normalize_skill_names(value)

    def _freeze_skills(
        self, workspace: str | Path, selected: Any
    ) -> list[dict[str, str]]:
        return self.capabilities._freeze_skills(workspace, selected)

    def _normalize_equipment(self, equipment: dict[str, Any]) -> dict[str, Any]:
        return self.capabilities._normalize_equipment(equipment)

    async def _get_task_entities(
        self, session: AsyncSession, task_id: str
    ) -> tuple[DesktopThread, DesktopWorkspace]:
        return await self.workspaces._get_task_entities(session, task_id)

    async def _execution_entities(
        self, session: AsyncSession, task_id: str
    ) -> tuple[DesktopThread, DesktopWorkspace]:
        return await self.workspaces._execution_entities(session, task_id)

    async def _get_material_entities(
        self, session: AsyncSession, material_id: str
    ) -> tuple[DesktopMaterial, DesktopWorkspace]:
        return await self.materials._get_material_entities(session, material_id)

    @staticmethod
    def _resolve_workspace_path(root: str | Path, value: str) -> Path:
        return MaterialService._resolve_workspace_path(root, value)

    @staticmethod
    def _digest(path: Path) -> str:
        return MaterialService._digest(path)

    async def _ensure_git(self, workspace: Path, confirmed: bool) -> None:
        return await self.materials._ensure_git(workspace, confirmed)

    async def _save_material_version(
        self,
        session: AsyncSession,
        material: DesktopMaterial,
        workspace_path: str,
        source: str,
    ) -> MaterialVersion | None:
        return await self.materials._save_material_version(
            session, material, workspace_path, source
        )

    async def _git(self, workspace: Path, *args: str, stdin: str | None = None) -> str:
        return await self.materials._git(workspace, *args, stdin=stdin)

    async def _git_bytes(self, workspace: Path, *args: str) -> bytes:
        return await self.materials._git_bytes(workspace, *args)

    async def _watch_materials(self) -> None:
        return await self.materials._watch_materials()

    async def _scan_materials(self, monitor, material_ids=None):
        return await self.materials._scan_materials(monitor, material_ids)

    @staticmethod
    def _workspace_payload(workspace: DesktopWorkspace) -> dict[str, Any]:
        return WorkspaceService._workspace_payload(workspace)

    def _task_summary(
        self,
        task: DesktopThread,
        workspace: DesktopWorkspace,
        active: DesktopRun | None = None,
        latest: DesktopRun | None = None,
        result: DesktopRun | None = None,
    ) -> dict[str, Any]:
        return self.conversations._task_summary(task, workspace, active, latest, result)

    async def _task_payload(
        self, session: AsyncSession, task: DesktopThread, workspace: DesktopWorkspace
    ) -> dict[str, Any]:
        return await self.conversations._task_payload(session, task, workspace)

    async def _commitment_recovery_payload(
        self, session: AsyncSession, task: DesktopThread
    ) -> dict[str, Any] | None:
        return await self.conversations._commitment_recovery_payload(session, task)

    def _draft_payload(self, draft: PatrolDraft) -> dict[str, Any]:
        return self.patrol._draft_payload(draft)

    @staticmethod
    def _run_payload(run: DesktopRun) -> dict[str, Any]:
        return RunService._run_payload(run)

    @classmethod
    def _run_payload_with_dispatch(
        cls, run: DesktopRun, dispatch: RunDispatch | None
    ) -> dict[str, Any]:
        return RunService._run_payload_with_dispatch.__func__(cls, run, dispatch)

    @staticmethod
    def _material_payload(
        material: DesktopMaterial, workspace_path: str
    ) -> dict[str, Any]:
        return MaterialService._material_payload(material, workspace_path)

    async def store_uploaded_material(
        self, task_id: str, upload: UploadFile
    ) -> dict[str, Any]:
        return await self.materials.store_uploaded_material(task_id, upload)

    async def resolve_material_content(
        self, task_id: str, material_id: str
    ) -> ResolvedMaterialContent:
        return await self.materials.resolve_material_content(task_id, material_id)

    async def validate_image_material(
        self, task_id: str, material_id: str
    ) -> dict[str, Any]:
        return await self.materials.validate_image_material(task_id, material_id)

    @staticmethod
    def _version_payload(version: MaterialVersion) -> dict[str, Any]:
        return MaterialService._version_payload(version)

    @cached_property
    def collaboration(self) -> CollaborationService:
        return CollaborationService(self)

    @cached_property
    def capabilities(self) -> CapabilityService:
        return CapabilityService(self)

    @cached_property
    def compression(self) -> CompressionService:
        return CompressionService(self)

    @cached_property
    def conversations(self) -> ConversationService:
        return ConversationService(self)

    @cached_property
    def materials(self) -> MaterialService:
        return MaterialService(self)

    @cached_property
    def patrol(self) -> PatrolService:
        return PatrolService(self)

    @cached_property
    def agent_factory(self) -> AgentFactory:
        return AgentFactory(self)

    @cached_property
    def run_assembly(self) -> RunAssemblyService:
        return RunAssemblyService(self)

    @cached_property
    def run_finalization(self) -> RunLifecycleService:
        return RunLifecycleService(self)

    @cached_property
    def run_recovery(self) -> RunRecoveryService:
        return RunRecoveryService(self)

    @cached_property
    def run_security(self) -> RunSecurityService:
        return RunSecurityService(self)

    @cached_property
    def runs(self) -> RunService:
        return RunService(self)

    @cached_property
    def workspaces(self) -> WorkspaceService:
        return WorkspaceService(self)
