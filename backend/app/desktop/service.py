"""
本文件对外提供 DesktopService 与 PreparedRun，编排桌面工作区、Context、Patrol、材料、
主运行、Context revision 终态和三套 subagent 机制（树形 spawn / Swarm / Coordinator）业务。

输入为已初始化的 PostgreSQL session factory、LangGraph checkpointer/store、StreamBridge、
RunManager 和 AppConfig；输出为供 routes.py 调用的异步业务方法以及 PreparedRun（统一
编排入口 start_run 的输入 + agent_factory 闭包）以及可从持久 Run 重建的 dispatch 装配源。
示例：`service = DesktopService(...); await service.start_main_run(task_id, message, ...)`。
基础行为仅进入 instructions；memory／selected skills／材料 policy／空间选择冻结为 Run 输入，能力目录与权限进入 checkpoint-bound WorldState。
真实用户与委托输入由 Run admission/装配端口绑定可信来源；模型 role 和正文不能替代该宿主身份。
Main 执行池按来源选择 custom／MCP；工作区工具和插件桥各自独占内置／插件注入，发现目录仍完整用于装备展示。
协作消息由耐久 inbox 准备，模型与工具尝试通过独立审计端口确认；临时运行控制不进入 authored 或 semantic 历史。
具体工作流为：登记真实宿主机工作区与线程，复制已提交 checkpoint 形成冻结草稿，
准备受文件沙箱约束的工作区 Agent 装配参数（经统一执行链路 worker.run_agent 执行），并把上传、
内容读取、逐轮材料解析/历史/投影和自定义分组分别委托给单一职责服务；主运行在同一事务
持久化稳定用户消息与有序材料绑定，图片是通用材料聚合的派生视图，初始与恢复路径使用同一
投影，图片交付、必看完成门和压缩门按职责独立装配；压缩通过 Context Evolution 迁移端口发布，
稳定 Run 则由 RunLifecycleFinalizer 在同一事务收敛终态、Context revision 与 durable outbox；主 Run 先原子写入 accepted dispatch，后台 worker 通过 lease/fencing 领取并从持久装备重建 Agent，HTTP 确认不再依赖内存 task 接力；Loop
后台启动把计划 Workspace Slot 与 Run 一同持久化，再把受治理工具根绑定到该 Slot；任务详情同时公开最新直接用户
Main Run 供 Loop 绑定首轮，并按活跃执行视图返回该 Context 的会话消息（执行身份上有更新状态时与运行流同源，
否则与已发布 revision 一致），数据库锚点与真实作用路径一致。
Agent 运行使用独立 checkpoint namespace 隔离，并用 Git 隐藏引用保护不可遗失材料。
Curator admission 读取精确 semantic snapshot，与任务页的 display 会话投影分离。
四套机制装配边界经 agent_role 区分：main（spawn 三件套 + 协作工具 + Mailbox 注入
+ 联网工具 web_search/web_fetch + MCP 远端工具 + 压缩门）、teammate/worker（持久派生
Agent，协作工具 + Mailbox 注入 + 联网工具）、patrol（小兵机制，工作区工具仅，无联网
无 MCP）。持久派生（spawn_teammate/spawn_worker）创建 SwarmAgent 身份并经
_launch_swarm_run 启动独立命名空间的后台 run；Swarm 等待快照在同一 repeatable-read 视图中
读取 Run、任务板和未读消息，避免组合出跨事务时刻的伪状态；工具错误 middleware 保证可恢复调用闭合，
主任务运行前的 checkpoint preflight 可从最近合法祖先恢复受损历史；主 Agent 中断恢复
经 resume_run 按载荷分派承诺层、必看报告、压缩流程与本机资源准入；Patrol 自主压缩可附带稳定
resolution Run identity，但仍沿用被中断 Run 的
Context revision execution thread/namespace；快捷压缩也在 current revision 的物理 namespace
原位生成后继 checkpoint，防止恢复或 Context 手术退回 UI 线程的旧历史。
执行身份：三个持久化启动点（主 run、resume、swarm）与本 UI 状态恢复路径统一经
_governed_context 由执行身份档案派生受治理上下文（工作根、能力权限、访问模式、
执行主体角色、执行命名空间），launcher 只提供身份材料；材料与图片投影在该上下文之上
由装配层写入；三档文件模式随装备持久化，直接用户 Run 每次新请求读取会话常驻模式，
并在模型上下文显示本次模式、工作区与升权规则，
与能力权限正交。会话常驻模式只经独立事务更新；普通 UI 状态保存保留数据库中的模式，
两类更新和主 Run 装备回写均锁定任务行，防止旧 UI 快照在完成顺序变化时覆盖新模式。

路径归属：本文件的 _resolve_workspace_path 服务于材料登记，即人在界面侧把工作区文件登记为
材料，属于界面侧入口，不受 Agent 本地访问策略约束；Agent 侧的路径解释与准入判定统一由
focus.security 承担（见 openspec add-local-access-policy）。

会话 standard Patrol 的文档、来源、编译、预览与分支生命周期委托 session_patrol；本组合根仅注入 canonical Reader、统一 Agent 工厂观察端口和现有 dispatch/ledger。Context Curator 和 Loop Patrol 保持各自权威流程。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path
import subprocess
from typing import Any, Awaitable, Callable
import uuid

from fastapi import HTTPException, UploadFile
from langchain.tools import ToolRuntime
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, tool
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.collab import AgentCollab
from backend.app.desktop.checkpoint_recovery import select_checkpoint_base
from backend.app.desktop.compression import compression_recovery_payload
from backend.app.desktop.pending_interrupts import main_pending_interrupt
from backend.app.desktop.context_service import ContextService
from backend.app.desktop.context_evolution import ContextRevisionOriginKind, ContextRevisionRepository
from backend.app.desktop.context_patrol_service import ContextPatrolService
from backend.app.desktop.context_curator import (
    CurationEngineError,
    CurationSourceProjector,
    build_curation_input,
    estimate_curation_tokens,
)
from backend.app.desktop.memory import MemoryService, _MAIN_RUNTIME_MEMORY_KEY
from backend.app.desktop.material_content import MaterialContentService, ResolvedMaterialContent
from backend.app.desktop.material_context import MaterialContextProjector
from backend.app.desktop.material_groups import MaterialGroupService
from backend.app.desktop.material_kinds import MaterialKindClassifier
from backend.app.desktop.material_snapshots import MaterialSnapshotVerifier
from backend.app.desktop.material_upload import MaterialUploadService
from backend.app.desktop.main_execution_identity import (
    identity_from_main_run,
    latest_main_run,
    resolve_main_execution_identity,
)
from backend.app.desktop.must_view_recovery import (
    must_view_recovery_payload,
    validate_must_view_resume,
)
from backend.app.desktop.models import (
    MAIN_RUN_EQUIPMENT_KEY,
    AgentBoardTask,
    AgentMessage,
    DesktopMaterial,
    DesktopRun,
    DesktopThread,
    DesktopWorkspace,
    ContextCurationPolicy,
    DraftUpdate,
    MaterialCreate,
    MaterialUpdate,
    MaterialVersion,
    RunMaterialBinding,
    PatrolAgent,
    PatrolDraft,
    SwarmAgent,
)
from backend.app.desktop.agent_loop.models import MessageProvenance
from backend.app.desktop.skills import build_task_skill_catalog, resolve_task_skills
from backend.app.desktop.context_assembly import desktop_contexts, skill_catalog_records
from backend.app.desktop.inbox import AgentInbox, DurableInboxMiddleware
from backend.app.desktop.execution_attempts import ModelAttemptJournal, ModelAttemptMiddleware, ToolExecutionLedger
from backend.app.desktop.resource_limits import ImageResourceLimits
from backend.app.desktop.run_images import RunImageResolver
from backend.app.desktop.run_material_history import RunMaterialHistoryRepository
from backend.app.desktop.run_material_message import RunMaterialMessageProjector
from backend.app.desktop.run_orchestration.input_provenance import bind_run_inputs
from backend.app.desktop.run_materials import RunMaterialRequest, RunMaterialResolver
from backend.app.desktop.run_orchestration import (
    DurableRunDispatchWorker,
    RunAdmissionConflict,
    RunAdmissionService,
    RunDispatchRecovery,
    RunDispatchRepository,
    RunDispatch,
    RunExecutionAssembler,
    RunExecutionAssembly,
    RunExecutionResources,
    RunLifecycleFinalizer,
    execute_prepared_run,
)
from backend.app.desktop.storage_values import normalize_json_storage_value
from backend.app.desktop.tool_error_provider import build_tool_error_middleware
from focus.tools.builtins.spawn_agent_tool import build_spawn_agent_tool
from backend.app.gateway.routers.thread_runs import RunCreateRequest
from focus.agents.commitment.middleware import commitment_subgraph_thread_id
from focus.agents.commitment.workflow import _human_payload
from focus.agents.compression import apply_compression_ranges, hit_keyword_message_ids
from focus.agents.compression.schemas import validate_apply_decision
from focus.security.approval import APPROVAL_TYPE
from focus.security.context import (
    AuthorizationIdentity,
    ExecutionProfile,
    RoutingIdentity,
    security_context_of,
)
from focus.security.effects import (
    DELEGATED_EXECUTION_EFFECT,
    NO_LOCAL_EFFECT,
    declare_all_effects,
    declare_effect,
)
from focus.security.launch import assemble_run_context
from focus.security.policy import AccessMode, workspace_roots
from backend.app.desktop.material_files import resolve_material_path
from focus.agents.image_attachment import build_image_attachment_middleware
from focus.agents.image_inputs import RunImageInputs
from focus.agents.material_inputs import RunMaterialInputs, project_run_material_context
from focus.agents.must_view import (
    build_must_view_completion_middleware,
    report_must_view_images,
)
from focus.images import is_image_name
from focus.messages import (
    estimate_images_tokens,
    estimate_messages_tokens,
    estimate_model_request_tokens,
    estimate_raw_tokens,
    strip_image_payloads,
)
from focus.agents.lead import make_lead_agent
from focus.config.app_config import AppConfig
from focus.runtime.checkpointer.namespaced import NamespacedCheckpointer
from focus.runtime.runs.events import (
    deserialize_messages,
    serialize_message,
    validate_messages,
)
from focus.runtime.runs.manager import RunManager, RunRecord
from focus.runtime.runs.worker import run_agent
from focus.runtime.stream_bridge.base import StreamBridge
from focus.tools import get_available_tools
from focus.tools.catalog import execution_pool_tools
from focus.tools.builtins.web_tools import web_fetch, web_search
from focus.tools.builtins.workspace_tools import select_workspace_tools
from backend.app.desktop.prompts import (
    MAIN_SYSTEM_PROMPT as _MAIN_SYSTEM_PROMPT,
    ASSEMBLY_SYSTEM_PROMPT as _ASSEMBLY_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)

_MAIN_RUNTIME_EQUIPMENT_KEY = MAIN_RUN_EQUIPMENT_KEY
_RUN_DISPATCH_EQUIPMENT_KEY = "_durable_dispatch_execution"
_TERMINAL_STATUSES = frozenset({"success", "error", "interrupted"})
_ACCESS_DECISIONS = frozenset({"approve", "reject", "cancel"})


def _resolve_access_mode(value: str | None) -> AccessMode:

    if not value:
        return AccessMode.WORKSPACE_WRITE
    try:
        return AccessMode(str(value))
    except ValueError:
        logger.warning("无法识别的访问模式 %r，已按只读处理", value)
        return AccessMode.READ_ONLY


def _identity_unregistered(what: str) -> HTTPException:


    return HTTPException(
        404,
        {"code": "execution_identity_unregistered", "message": f"执行身份未登记：{what}"},
    )


_ASSEMBLY_WORKSPACE_DISPLAY = "无工作区模式"
_ASSEMBLY_THREAD_ID = "focus-assembly"


@dataclass
class PreparedRun:
    """一次运行发起所需的编排输入：统一接口 body + agent_factory 闭包 + 响应载荷。

    agent_factory 为 None 表示幂等命中已有 run，无需发起（routes._launch 据此跳过）。
    """

    body: RunCreateRequest
    thread_id: str
    agent_factory: Callable[[], Awaitable[CompiledStateGraph]] | None
    payload: dict[str, Any]


def new_id() -> str:
    return uuid.uuid4().hex


def _checkpoint_commitment_review(
    checkpoint: Any,
    stage: int,
) -> dict[str, Any] | None:

    pending_writes = list(getattr(checkpoint, "pending_writes", None) or [])
    for _task_id, channel, raw_value in reversed(pending_writes):
        if channel != "__interrupt__":
            continue
        values = raw_value if isinstance(raw_value, (list, tuple)) else [raw_value]
        for item in reversed(values):
            payload = getattr(item, "value", item)
            if not isinstance(payload, dict):
                continue
            try:
                payload_stage = int(payload.get("stage") or 0)
            except (TypeError, ValueError):
                continue
            if payload.get("type") == "commitment_review" and payload_stage == stage:
                return dict(payload)
    return None


def _must_view_prompt(materials: list[dict[str, str]]) -> str:
    lines = "\n".join(
        f"- {item['relative_path']} (material_id={item['material_id']})" for item in materials
    )
    return (
        "\n\n<focus_must_view>\n"
        "用户把以下图片标记为本轮必须查看。请逐张查看，并调用 "
        f"{report_must_view_images.name} 为每一张各给一条声明："
        "成功读到该图片的内容即把 read 置为真。\n"
        f"{lines}\n"
        "</focus_must_view>"
    )


def _material_policy_line(path: Path, reading_mode: str, instruction_mode: str) -> str:
    if is_image_name(path.name):
        return f"- {path} | 图片材料"
    reading = "优先完整阅读" if reading_mode == "full" else "优先粗略阅读，需要时仍可完整读取"
    instruction = "严格遵守" if instruction_mode == "strict" else "仅供参考"
    return f"- {path} | {reading} | {instruction}"


def estimate_tokens(
    system_prompt: str,
    messages: list[dict[str, Any]],
    final_message: str,
    run_images: RunImageInputs | None = None,
) -> int:
    image_tokens = estimate_images_tokens(messages)
    counted = strip_image_payloads(messages) if image_tokens else messages
    raw = system_prompt + final_message + json.dumps(counted, ensure_ascii=False, separators=(",", ":"))
    state_total = estimate_raw_tokens(raw, len(messages)) + image_tokens
    if run_images is None:
        return state_total
    request_extra = estimate_model_request_tokens(messages, run_images.attached) - estimate_messages_tokens(messages)
    return state_total + request_extra


def prompt_with_skills(system_prompt: str, snapshots: list[dict[str, str]]) -> str:
    if not snapshots:
        return system_prompt
    blocks = "\n\n".join(
        f"## {snapshot['name']}\n{snapshot['content']}" for snapshot in snapshots
    )
    return f"{system_prompt}\n\n<selected_skills>\n{blocks}\n</selected_skills>"


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
        self.image_limits = ImageResourceLimits()
        self.material_uploads = MaterialUploadService(session_factory, self.image_limits)
        self.material_contents = MaterialContentService(session_factory, self.image_limits)
        self.run_images = RunImageResolver(self.image_limits)
        self.run_materials = RunMaterialResolver(self.image_limits)
        self.run_material_history = RunMaterialHistoryRepository()
        self.material_groups = MaterialGroupService(session_factory)
        self.material_snapshots = MaterialSnapshotVerifier()
        self.contexts = ContextService(session_factory, checkpointer, app_config)
        from backend.app.desktop.session_patrol.deployment import PatrolDeploymentCoordinator
        from backend.app.desktop.session_patrol.sources import PatrolSourceResolver
        self.session_patrol = PatrolDeploymentCoordinator(self)
        self.patrol_sources = PatrolSourceResolver(self)
        self.context_patrol = ContextPatrolService(
            session_factory, self.contexts, checkpointer, store, bridge, run_manager, app_config
        )
        self.run_lifecycle = RunLifecycleFinalizer(session_factory, checkpointer)
        self._run_admission = RunAdmissionService()
        self._run_dispatch_repository = RunDispatchRepository()
        self._run_dispatch_recovery = RunDispatchRecovery(session_factory)
        self._run_dispatch_wakeup = asyncio.Event()
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


        self.agent_collab = AgentCollab(session_factory, swarm_launcher=self._auto_wake_swarm)

        self.memory = MemoryService(session_factory, app_config, self._memory_context_messages)

        self._sync_tasks: set[asyncio.Task] = set()
        self._watcher: asyncio.Task | None = None
        self._material_watch_failures: set[str] = set()

    async def start(self) -> None:
        dispatch_recovery = await self._run_dispatch_recovery.reconcile()
        await self.run_lifecycle.reconcile_active(
            "桌面后端重启，原运行无法继续",
            exclude_run_ids=set(dispatch_recovery.safe_run_ids),
        )
        await self.context_patrol.start()
        self._watcher = asyncio.create_task(self._watch_materials())
        self._run_dispatch_task = asyncio.create_task(self._run_dispatch_loop(), name="desktop-main-run-dispatch")
        self._run_dispatch_wakeup.set()

    async def close(self) -> None:
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

    def notify_run_dispatch(self) -> None:
        self._run_dispatch_wakeup.set()

    async def _run_dispatch_loop(self) -> None:
        while True:
            self._run_dispatch_wakeup.clear()
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
        resources = RunExecutionResources(
            bridge=self.bridge,
            run_manager=self.run_manager,
            checkpointer=self.checkpointer,
            store=self.store,
            app_config=self.app_config,
        )
        async with self.session_factory() as session:
            run = await session.get(DesktopRun, assembly.run_id)
            if run is None:
                raise LookupError(f"Run 不存在: {assembly.run_id}")
            loop_id = run.loop_id
        if loop_id:
            from backend.app.desktop.agent_loop.run_execution import LoopRunExecutionBoundary

            return await LoopRunExecutionBoundary(self.session_factory).start(
                assembly, resources, execute_prepared_run
            )
        return await execute_prepared_run(
            assembly.body, assembly.thread_id, resources, assembly.agent_factory
        )

    async def assemble_run(self, run_id: str) -> RunExecutionAssembly:
        loop_id = None
        async with self.session_factory() as session:
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
            if run.status != "pending" or execution.get("agent_role") not in {"main", "patrol"}:
                raise RuntimeError(f"Run 不可由 durable dispatch 装配: {run.run_id}@{run.status}")
            workspace_path = str((run.workspace_anchor or {}).get("workspace_path") or workspace.path)
            slot_id = (run.workspace_anchor or {}).get("slot_id")
            if run.directive_id and not slot_id:
                raise RuntimeError(f"Loop directive Run 缺少计划 workspace slot: {run.run_id}")
            if slot_id:
                from backend.app.desktop.workspace_coordination.models import WorkspaceSlot

                slot = await session.get(WorkspaceSlot, slot_id)
                if slot is not None:
                    workspace_path = slot.root_path
            loop_id = run.loop_id
            if loop_id:
                from backend.app.desktop.agent_loop.run_execution import LoopRunExecutionBoundary

                await LoopRunExecutionBoundary(self.session_factory).validate(run_id)
            prepared = await self._prepare(
                run,
                str(run.execution_thread_id or task.thread_id),
                str((run.workspace_anchor or {}).get("workspace_id") or workspace.workspace_id),
                workspace_path,
                list(run.input_messages or []),
                str(execution["base_prompt"]),
                equipment,
                run.checkpoint_ns or "",
                execution["agent_role"],
                execution.get("checkpoint_id"),
                bool(execution.get("allow_global_config")),
            )
            if "resume_payload" in execution:
                prepared.body.input = None
                prepared.body.resume = execution["resume_payload"]
        if loop_id:
            from backend.app.desktop.agent_loop.dispatch import LoopRunWorkspaceBinder

            await LoopRunWorkspaceBinder(self.session_factory).bind(
                run_id=run_id,
                loop_id=loop_id,
                body=prepared.body,
                slot_id=slot_id,
                directive_id=run.directive_id,
            )
        if prepared.agent_factory is None:
            raise RuntimeError(f"Run 装配未生成 Agent factory: {run_id}")
        return RunExecutionAssembly(run_id=run_id, body=prepared.body, thread_id=prepared.thread_id, agent_factory=prepared.agent_factory)

    async def equipment(self) -> dict[str, Any]:
        from backend.app.desktop.model_settings import catalog_snapshot

        try:
            raw = json.loads(Path("extensions_config.json").read_text(encoding="utf-8"))
            skills = sorted(name for name, cfg in raw.get("skills", {}).items() if cfg.get("enabled"))
        except (OSError, json.JSONDecodeError):
            skills = []
        return {

            "models": catalog_snapshot(self.app_config),
            "skills": skills,
            "permissions": ["read", "write", "host_command"],
            "tools": await self._equipment_tools(),
        }

    async def run_by_idempotency(self, idempotency_key: str) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            run = await session.scalar(select(DesktopRun).where(DesktopRun.idempotency_key == idempotency_key))
            if run is None:
                return None
            dispatch = await session.scalar(select(RunDispatch).where(RunDispatch.run_id == run.run_id))
            return self._run_payload_with_dispatch(run, dispatch)

    async def get_run_payload(self, run_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            run = await session.get(DesktopRun, run_id)
            if run is None:
                raise HTTPException(404, "运行不存在")
            dispatch = await session.scalar(select(RunDispatch).where(RunDispatch.run_id == run_id))
            return self._run_payload_with_dispatch(run, dispatch)

    async def _equipment_tools(self) -> list[dict[str, Any]]:


        from focus.tools.interfaces import ToolInfo
        from focus.tools.tools import get_available_tools

        reg = await get_available_tools()
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for info in reg:
            if info.name in seen:
                continue
            seen.add(info.name)
            result.append({
                "name": info.name,
                "label": info.label if isinstance(info, ToolInfo) else info.name,
                "source": info.source if isinstance(info, ToolInfo) else "builtin",
            })
        return result

    async def create_workspace(self, path: str, display_name: str | None = None) -> dict[str, Any]:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise HTTPException(422, "工作区必须是已存在的本地文件夹")
        managed_app = os.getenv("FOCUS_MANAGED_APP")
        if managed_app:
            managed_root = Path(managed_app).expanduser().resolve()
            if resolved == managed_root or managed_root in resolved.parents:
                raise HTTPException(422, "工作区不能位于 Focus 托管程序目录内")
        normalized = os.path.normpath(str(resolved))
        async with self.session_factory() as session:
            existing = await session.scalar(select(DesktopWorkspace).where(DesktopWorkspace.path == normalized))
            if existing:
                return self._workspace_payload(existing)
            workspace = DesktopWorkspace(
                workspace_id=new_id(), path=normalized, display_name=display_name or resolved.name or normalized
            )
            session.add(workspace)
            await session.commit()
            return self._workspace_payload(workspace)

    async def create_thread(
        self, workspace_id: str, thread_id: str | None, title: str,
        access_mode: str | None = None,
    ) -> dict[str, Any]:
        async with self.session_factory() as session:
            workspace = await session.get(DesktopWorkspace, workspace_id)
            if not workspace:
                raise HTTPException(404, "工作区不存在")
            identity = thread_id or new_id()
            existing = await session.scalar(
                select(DesktopThread).where(
                    DesktopThread.workspace_id == workspace_id, DesktopThread.thread_id == identity
                )
            )
            if existing:
                return await self._task_payload(session, existing, workspace)
            task = DesktopThread(
                task_id=new_id(), workspace_id=workspace_id, thread_id=identity, title=title,
                ui_state={"access_mode": str(_resolve_access_mode(access_mode))},
            )
            session.add(task)
            await session.commit()
            return await self._task_payload(session, task, workspace)

    async def list_tasks(self) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(DesktopThread, DesktopWorkspace)
                    .join(DesktopWorkspace, DesktopThread.workspace_id == DesktopWorkspace.workspace_id)
                    .order_by(DesktopThread.created_at)
                )
            ).all()
            return [await self._task_payload(session, task, workspace) for task, workspace in rows]

    async def get_task(self, task_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            task, workspace = await self._get_task_entities(session, task_id)
            payload = await self._task_payload(session, task, workspace)
        snapshot = await self.contexts.live_conversation(task_id)
        payload["messages"] = snapshot["messages"]
        payload["context"] = await self.contexts.get(task_id)
        return payload

    async def _memory_context_messages(self, context_id: str) -> list[dict[str, Any]]:

        snapshot = await self.contexts.snapshot(context_id)
        return snapshot["messages"]

    async def list_task_skills(self, task_id: str) -> list[dict[str, str]]:
        async with self.session_factory() as session:
            _, workspace = await self._get_task_entities(session, task_id)
        return [
            {"name": skill.name, "description": skill.description}
            for skill in build_task_skill_catalog(workspace.path).values()
        ]

    async def save_ui_state(self, task_id: str, ui_state: dict[str, Any]) -> None:
        async with self.session_factory() as session:
            task = await session.scalar(
                select(DesktopThread).where(DesktopThread.task_id == task_id).with_for_update()
            )
            if not task:
                raise HTTPException(404, "任务不存在")
            persisted = dict(ui_state)
            persisted.pop("access_mode", None)
            standing_mode = (task.ui_state or {}).get("access_mode")
            if standing_mode is not None:
                persisted["access_mode"] = standing_mode
            runtime_equipment = (task.ui_state or {}).get(
                _MAIN_RUNTIME_EQUIPMENT_KEY
            )
            if runtime_equipment is not None:
                persisted[_MAIN_RUNTIME_EQUIPMENT_KEY] = runtime_equipment
            task.ui_state = persisted
            await session.commit()

    async def set_session_access_mode(self, task_id: str, access_mode: str) -> None:
        mode = AccessMode(access_mode)
        async with self.session_factory() as session:
            task = await session.scalar(
                select(DesktopThread).where(DesktopThread.task_id == task_id).with_for_update()
            )
            if task is None:
                raise HTTPException(404, "任务不存在")
            task.ui_state = {**(task.ui_state or {}), "access_mode": str(mode)}
            await session.commit()

    async def get_checkpoint_messages(self, thread_id: str, checkpoint_ns: str) -> list[dict[str, Any]]:
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}}
        checkpoint = await self.checkpointer.aget_tuple(config)
        if checkpoint is None:
            return []
        values = checkpoint.checkpoint.get("channel_values", {})
        return [serialize_message(message) for message in values.get("messages", [])]

    async def quick_compression_preview(
        self, task_id: str, keyword: str, case_sensitive: bool = False
    ) -> dict[str, Any]:


        async with self.session_factory() as session:
            task, _ = await self._get_task_entities(session, task_id)
            await self.contexts.ensure_runnable(session, task_id)
            current_revision = await ContextRevisionRepository().current(
                session, task_id
            )
            execution_thread_id = (
                current_revision.ref.execution_thread_id
                if current_revision is not None
                else task.thread_id
            )
            execution_checkpoint_ns = (
                current_revision.ref.checkpoint_ns
                if current_revision is not None
                else ""
            )
        messages = await self.get_checkpoint_messages(
            execution_thread_id, execution_checkpoint_ns
        )
        hit_ids = hit_keyword_message_ids(messages, keyword, case_sensitive)
        hit_set = set(hit_ids)
        hit_messages = [message for message in messages if message.get("id") in hit_set]
        return {"keyword": keyword, "hit_ids": hit_ids, "hit_messages": hit_messages}

    async def quick_compression_apply(self, task_id: str, ranges: list[dict[str, Any]], scrub_terms: list[str] | None = None) -> dict[str, Any]:


        async with self.session_factory() as session:
            task, _ = await self._get_task_entities(session, task_id)
            current_revision = await ContextRevisionRepository().current(
                session, task_id
            ) if hasattr(self, "contexts") else None
            execution_thread_id = (
                current_revision.ref.execution_thread_id
                if current_revision is not None
                else task.thread_id
            )
            execution_checkpoint_ns = (
                current_revision.ref.checkpoint_ns
                if current_revision is not None
                else ""
            )
            active = await session.scalar(
                select(DesktopRun.run_id)
                .where(
                    DesktopRun.task_id == task.task_id,
                    DesktopRun.agent_id == f"main:{task.task_id}",
                    DesktopRun.status.in_(["pending", "running"]),
                )
                .limit(1)
            )
            if active:
                raise HTTPException(
                    409,
                    {
                        "code": "main_run_active",
                        "message": "主 Agent 正在运行，请先等待其结束或取消后再执行快捷压缩",
                    },
                )
        messages = await self.get_checkpoint_messages(
            execution_thread_id, execution_checkpoint_ns
        )
        base_messages = deserialize_messages(messages)

        current_ids = {m.id for m in base_messages if m.id}
        missing = [
            sid for r in ranges for sid in (r.get("source_ids") or [])
            if sid not in current_ids
        ]
        if missing:
            raise HTTPException(
                409,
                {
                    "code": "context_changed",
                    "message": "上下文已更新（部分消息已被折叠进压缩块），请刷新压缩面板后重试",
                },
            )
        normalized, error = validate_apply_decision(
            {"type": "compression", "decision": "apply", "ranges": ranges},
            base_messages,
        )
        if error:
            raise HTTPException(422, f"快捷压缩范围非法: {error}")
        update = apply_compression_ranges(base_messages, normalized)
        config = {"configurable": {"thread_id": execution_thread_id}}
        graph = await make_lead_agent(
            tools=[], system_prompt="", middlewares=[], app_config=self.app_config
        )
        graph.checkpointer = (
            NamespacedCheckpointer(self.checkpointer, execution_checkpoint_ns)
            if execution_checkpoint_ns
            else self.checkpointer
        )
        updated_config = await graph.aupdate_state(config, update)
        if hasattr(self, "contexts"):
            checkpoint_id = (
                updated_config.get("configurable", {}).get("checkpoint_id")
                if isinstance(updated_config, dict)
                else None
            )
            if not checkpoint_id:
                raise RuntimeError("快捷压缩写入后未返回 checkpoint_id")
            revision_origin = (
                ContextRevisionOriginKind.COMPRESSION_RESTORE
                if any(item.get("restore") for item in normalized)
                else ContextRevisionOriginKind.COMPRESSION
            )
            await self.contexts.evolution.publish_checkpoint(
                task_id,
                checkpoint_id,
                revision_origin,
                origin_id=checkpoint_id,
            )
        await self.context_patrol.notify_stable_context_checkpoint(task_id)
        return {
            "messages": await self.get_checkpoint_messages(
                execution_thread_id, execution_checkpoint_ns
            )
        }

    async def open_draft(self, task_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            task, _ = await self._get_task_entities(session, task_id)
            existing = await session.scalar(
                select(PatrolDraft)
                .where(PatrolDraft.task_id == task_id, PatrolDraft.status == "editing")
                .order_by(PatrolDraft.updated_at.desc())
            )
            if existing:
                payload = self._draft_payload(existing)
                if existing.mode == "context_curator":
                    payload["root_context_id"] = await self.contexts.resolve_chat_root(task_id)
                return payload
            from backend.app.desktop.context_evolution.repository import ContextRevisionRepository
            from backend.app.desktop.context_evolution.reader import ContextRevisionReader
            from focus.history import serialize_history_message, legacy_to_items, semantic_policy, content_hash
            from backend.app.desktop.session_patrol.sources import portable_record
            repository = ContextRevisionRepository()
            revision = await repository.current(session, task_id)
            checkpoint_id = revision.ref.checkpoint_id if revision else None
            if revision:
                view = await ContextRevisionReader(repository, self.checkpointer).read(session, revision.ref, "execution")
                messages = list(view.messages)
            else:
                config = {"configurable": {"thread_id": task.thread_id, "checkpoint_ns": ""}}
                checkpoint = await self.checkpointer.aget_tuple(config)
                checkpoint_id = checkpoint.config["configurable"].get("checkpoint_id") if checkpoint else None
                messages = [serialize_history_message(message) for message in checkpoint.checkpoint.get("channel_values", {}).get("messages", [])] if checkpoint else []
            frozen_sources = {}
            portable = []
            for record in messages:
                items = legacy_to_items([record])
                if all(semantic_policy(item) in {"exclude", "reference_only"} for item in items):
                    continue
                value = portable_record(record)
                source_ref = new_id()
                source = revision.ref.model_dump(mode="json") if revision else {"context_id": task_id, "execution_thread_id": task.thread_id, "checkpoint_ns": "", "checkpoint_id": checkpoint_id, "legacy": True}
                frozen_sources[source_ref] = {"record": record, "source": {**source, "message_id": record.get("id")},
                    "entry_hash": content_hash({key: value.get(key) for key in ("role", "content", "tool_calls", "tool_call_id", "name", "status")}), "historical_runtime": False}
                value.update(source_ref=source_ref, source_hash=content_hash(record))
                portable.append(value)
            messages = normalize_json_storage_value(portable)
            default_model = self.app_config.resolve_default_model_name()
            equipment = {
                "model_name": default_model,
                "skills": [],
                "permissions": ["read"],
                "access_mode": str(AccessMode.WORKSPACE),
            }
            draft = PatrolDraft(
                draft_id=new_id(),
                task_id=task_id,
                history_messages=messages,
                frozen_sources=frozen_sources,
                source_checkpoint_id=checkpoint_id,
                equipment=equipment,
                token_estimate=estimate_tokens("", messages, ""),
            )
            session.add(draft)
            await session.commit()
            return self._draft_payload(draft)

    async def update_draft(self, draft_id: str, body: DraftUpdate) -> dict[str, Any]:
        async with self.session_factory() as session:
            from backend.app.desktop.session_patrol.repository import DraftRepository
            draft = await DraftRepository().save(session, draft_id, body.authoring_document, expected_revision=body.draft_revision)
            if not draft or draft.status != "editing":
                raise HTTPException(404, "可编辑草稿不存在")
            task_row, workspace = await self._get_task_entities(session, draft.task_id)
            equipment = normalize_json_storage_value(
                self._normalize_equipment(body.equipment)
            )
            if body.mode == "context_curator":
                forbidden = set(equipment.get("permissions") or []) & {"write", "host_command"}
                if forbidden:
                    raise HTTPException(422, "Context 策展模式禁止 write 与 host_command 权限")
                if equipment.get("skills"):
                    raise HTTPException(422, "Context 策展模式不装配技能或通用工具")
                policy = ContextCurationPolicy.model_validate(body.curation_policy)
                root_context_id = await self._prepare_context_curator_draft(
                    draft, equipment.get("model_name"), policy
                )
                await session.commit()
                payload = self._draft_payload(draft)
                payload["root_context_id"] = root_context_id
                return payload

            if body.authoring_document is not None:
                draft.equipment = equipment
                draft.mode = "standard"
                await session.commit()
                return self._draft_payload(draft)
            safe_system_prompt = normalize_json_storage_value(body.system_prompt)
            safe_history_messages = normalize_json_storage_value(body.history_messages)
            safe_final_human_message = normalize_json_storage_value(body.final_human_message)
            snapshots, _ = resolve_task_skills(
                build_task_skill_catalog(workspace.path), equipment["skills"]
            )
            estimate = estimate_tokens(
                prompt_with_skills(safe_system_prompt, snapshots),
                safe_history_messages,
                safe_final_human_message,
            )
            draft.system_prompt = safe_system_prompt
            draft.history_messages = safe_history_messages
            draft.final_human_message = safe_final_human_message
            draft.equipment = equipment
            draft.mode = "standard"
            draft.curation_policy = {}
            draft.token_estimate = estimate
            await session.commit()
            return self._draft_payload(draft)

    async def quick_deploy_context_curator(
        self, task_id: str, deployment_id: str
    ) -> PreparedRun:

        async with self.session_factory() as session:
            existing = await session.scalar(
                select(DesktopRun).where(DesktopRun.deployment_id == deployment_id)
            )
            if existing:
                return PreparedRun(
                    body=RunCreateRequest(input={"messages": []}),
                    thread_id="",
                    agent_factory=None,
                    payload=self._run_payload(existing),
                )
            await self._get_task_entities(session, task_id)
            draft = PatrolDraft(
                draft_id=new_id(),
                task_id=task_id,
                status="editing",
                mode="context_curator",
                system_prompt="",
                history_messages=[],
                final_human_message="",
                equipment={},
                curation_policy={},
                token_estimate=0,
            )
            await self._prepare_context_curator_draft(
                draft,
                self._default_curation_model_name(),
                ContextCurationPolicy(),
            )
            session.add(draft)
            await session.commit()
            draft_id = draft.draft_id
        return await self.deploy(draft_id, deployment_id)

    async def _prepare_context_curator_draft(
        self,
        draft: PatrolDraft,
        model_name: str | None,
        policy: ContextCurationPolicy,
    ) -> str:

        root_context_id = await self.contexts.resolve_chat_root(draft.task_id)
        checkpoint_id = await self.contexts.current_checkpoint_id(root_context_id)
        if checkpoint_id is None:
            raise HTTPException(409, "根 Context 尚无稳定 checkpoint")
        snapshot = await self.contexts.semantic_snapshot(root_context_id, checkpoint_id)
        try:
            self.context_patrol.engine.validate_model(model_name)
        except CurationEngineError as exc:
            raise HTTPException(422, {
                "code": "curation_model_incompatible",
                "message": str(exc),
            }) from exc
        source = CurationSourceProjector().project(checkpoint_id, snapshot["messages"])
        curation_input = build_curation_input(source, 0, policy, [])
        estimate = estimate_curation_tokens(curation_input)
        model = self.app_config.get_model(model_name or self.app_config.resolve_default_model_name())
        self._validate_model_window(model_name, estimate + model.curation_max_output_tokens)
        draft.mode = "context_curator"
        draft.curation_policy = normalize_json_storage_value(policy.model_dump(mode="json"))
        draft.system_prompt = ""
        draft.history_messages = []
        draft.final_human_message = ""
        draft.equipment = {
            "model_name": model_name,
            "skills": [],
            "permissions": ["read"],

            "access_mode": str(AccessMode.WORKSPACE),
        }
        draft.source_checkpoint_id = checkpoint_id
        draft.token_estimate = estimate
        return root_context_id

    def _default_curation_model_name(self) -> str:
        configured = [model for model in self.app_config.models if model.curation_default]
        if not configured:
            raise HTTPException(422, {
                "code": "curation_default_model_missing",
                "message": "尚未配置 Context 策展默认模型",
            })
        model = configured[0]
        try:
            self.context_patrol.engine.validate_model(model.name)
        except CurationEngineError as exc:
            raise HTTPException(422, {
                "code": "curation_default_model_incompatible",
                "message": str(exc),
            }) from exc
        return model.name

    async def deploy(self, draft_id: str, deployment_id: str, preview_token: str | None = None) -> PreparedRun:
        async with self.session_factory() as session:
            draft_mode = await session.scalar(
                select(PatrolDraft.mode).where(PatrolDraft.draft_id == draft_id)
            )
        if draft_mode == "context_curator":
            run = await self.context_patrol.deploy(draft_id, deployment_id)
            return PreparedRun(
                body=RunCreateRequest(input={"messages": []}),
                thread_id="",
                agent_factory=None,
                payload=self._run_payload(run),
            )
        return await self.session_patrol.deploy(draft_id, deployment_id, preview_token)

    def _require_run_admission(self) -> None:
        if not getattr(self.app_config, "context_run_admission", True):
            raise HTTPException(503, "新 Run admission 已暂停；现有 V1/V2 历史可读，恢复前需确认兼容 execution 分支")

    async def start_main_run(
        self, task_id: str, message: str | list[dict[str, Any]], model_name: str | None,
        permissions: list[str], skills: list[str], spatial_focus: dict[str, Any] | None = None,
        memory_ids: list[str] | None = None,
        material_inputs: list[RunMaterialRequest] | None = None,
        attached_material_ids: list[str] | None = None,
        must_view_material_ids: list[str] | None = None,
        access_mode: str | None = None,
        run_identity: dict[str, Any] | None = None,
        execution_workspace_path: str | None = None,
        execution_slot_id: str | None = None,
        admit_only: bool = False,
    ) -> PreparedRun:
        identity = run_identity or {}
        if identity.get("directive_id") and (not identity.get("loop_id") or not execution_slot_id):
            raise ValueError("Loop directive Run 必须携带 Loop identity 与计划 workspace slot")
        async with self.session_factory() as session:
            idempotency_key = identity.get("idempotency_key")
            if idempotency_key:
                existing = await session.scalar(select(DesktopRun).where(DesktopRun.idempotency_key == idempotency_key))
                if existing is not None:
                    return PreparedRun(
                        body=RunCreateRequest(input={"messages": []}, context={"run_id": existing.run_id}),
                        thread_id=str(existing.execution_thread_id or ""),
                        agent_factory=None,
                        payload=self._run_payload(existing),
                    )
            task_row, workspace = await self._execution_entities(session, task_id)
            if identity.get("loop_id") and not execution_slot_id:
                from backend.app.desktop.agent_loop.models import AgentLoop
                from backend.app.desktop.workspace_coordination.models import WorkspaceSlot

                loop = await session.get(AgentLoop, identity["loop_id"])
                if loop is None or loop.workspace_id != workspace.workspace_id:
                    raise LookupError("Loop Run workspace 身份不匹配")
                slot = await session.scalar(select(WorkspaceSlot).where(
                    WorkspaceSlot.workspace_id == workspace.workspace_id,
                    WorkspaceSlot.kind == "authoritative",
                    WorkspaceSlot.lifecycle == "active",
                ))
                if slot is None:
                    raise LookupError("Loop Run 缺少可用权威 workspace slot")
                execution_slot_id = slot.slot_id
                execution_workspace_path = slot.root_path
            await self.contexts.ensure_runnable(session, task_id)
            current_revision = await ContextRevisionRepository().current(session, task_id)
            revision_ref = current_revision.ref if current_revision is not None else None
            execution_thread_id = (
                revision_ref.execution_thread_id if revision_ref is not None else task_row.thread_id
            )
            execution_checkpoint_ns = (
                revision_ref.checkpoint_ns if revision_ref is not None else ""
            )
            authoritative_checkpoint_id = (
                revision_ref.checkpoint_id if revision_ref is not None else None
            )
            recovery = await self._commitment_recovery_payload(session, task_row)
            if recovery is not None:
                await self._project_loop_pending_decision(identity, "commitment", recovery)
                raise HTTPException(
                    409,
                    {
                        "code": "commitment_review_pending",
                        "recovery": recovery["status"],
                        "message": "存在尚未处理的承诺审批，请先继续审批或显式放弃旧流程",
                    },
                )
            compression_recovery = await compression_recovery_payload(
                session, task_row, self.checkpointer
            )
            if compression_recovery is not None:
                await self._project_loop_pending_decision(identity, "compression", compression_recovery)
                raise HTTPException(
                    409,
                    {
                        "code": "compression_request_pending",
                        "recovery": compression_recovery["status"],
                        "message": "存在待确认的压缩请求，请先完成压缩或取消后继续",
                    },
                )
            must_view_recovery = await must_view_recovery_payload(
                session, task_row, self.checkpointer
            )
            if must_view_recovery is not None:
                await self._project_loop_pending_decision(identity, "must_view", must_view_recovery)
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
                session, task_row, self.checkpointer, APPROVAL_TYPE
            )
            if access_recovery is not None:
                await self._project_loop_pending_decision(identity, "access_approval", access_recovery)
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
            snapshots = self._freeze_skills(workspace.path, skills)
            run_id = str(identity.get("run_id") or new_id())
            message_id = str(identity.get("message_id") or new_id())
            run_materials = await self.run_materials.resolve(
                session,
                task_id,
                workspace.path,
                run_id,
                message_id,
                material_inputs,
                attached_material_ids,
                must_view_material_ids or [],
            )
            equipment = {
                "model_name": model_name,
                "skills": list(dict.fromkeys(skills)),
                "skill_snapshots": snapshots,
                "permissions": permissions,
                "access_mode": str(_resolve_access_mode(access_mode)),
                "spatial_focus": spatial_focus,
                **run_materials.to_equipment(),
            }
            history = await self.get_checkpoint_messages(
                execution_thread_id, execution_checkpoint_ns
            )
            current_message = RunMaterialMessageProjector.project(message, run_materials)
            checkpoint_id = authoritative_checkpoint_id or await select_checkpoint_base(
                self.checkpointer, execution_thread_id, execution_checkpoint_ns
            )
            is_assembly = task_row.thread_id == _ASSEMBLY_THREAD_ID
            base_prompt = _ASSEMBLY_SYSTEM_PROMPT if is_assembly else _MAIN_SYSTEM_PROMPT
            equipment["memory_snapshots"] = await self.memory.freeze_selection(memory_ids)
            equipment[_RUN_DISPATCH_EQUIPMENT_KEY] = {
                "agent_role": "main",
                "base_prompt": base_prompt,
                "checkpoint_id": checkpoint_id,
                "allow_global_config": is_assembly,
            }
            self._validate_model_window(
                model_name,
                estimate_tokens(_MAIN_SYSTEM_PROMPT, [*history, current_message], "", run_materials.images),
            )
            self._require_run_admission()
            run = DesktopRun(
                run_id=run_id, task_id=task_id, agent_id=f"main:{task_id}", kind="main", status="pending",
                input_messages=[current_message], model_name=model_name,
                origin=str(identity.get("origin") or "direct_user"), execution_thread_id=execution_thread_id,
                checkpoint_ns=execution_checkpoint_ns, context_revision_id=task_row.current_revision_id,
                context_checkpoint_id=authoritative_checkpoint_id,
                origin_message_id=message_id, equipment=equipment,
                directive_id=identity.get("directive_id"), loop_id=identity.get("loop_id"),
                user_intent_id=identity.get("user_intent_id"),
                round_id=identity.get("round_id"), action_id=identity.get("action_id"),
                idempotency_key=identity.get("idempotency_key"),
                workspace_anchor={
                    "workspace_id": workspace.workspace_id,
                    "workspace_path": execution_path,
                    **({"slot_id": execution_slot_id} if execution_slot_id else {}),
                },
            )
            try:
                admission = await getattr(self, "_run_admission", RunAdmissionService()).admit(session, run)
            except RunAdmissionConflict as exc:
                raise HTTPException(409, {"code": "main_run_active", "message": str(exc)}) from exc
            if not admission.created:
                await session.rollback()
                return PreparedRun(
                    body=RunCreateRequest(input={"messages": []}, context={"run_id": admission.run.run_id}),
                    thread_id=str(admission.run.execution_thread_id or ""),
                    agent_factory=None,
                    payload=self._run_payload(admission.run),
                )
            self.run_material_history.add(session, run, run_materials)
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
                body=RunCreateRequest(input={"messages": []}, context={"run_id": run.run_id}),
                thread_id=thread_id,
                agent_factory=None,
                payload={
                    **self._run_payload(run),
                    "dispatch": {"dispatch_id": admission.dispatch.dispatch_id, "status": admission.dispatch.status, "attempt": admission.dispatch.attempt},
                },
            )
        prepared = await self._prepare(
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
            allow_global_config=is_assembly,
        )
        prepared.payload["dispatch"] = {"dispatch_id": admission.dispatch.dispatch_id, "status": admission.dispatch.status, "attempt": admission.dispatch.attempt}
        return prepared

    async def _project_loop_pending_decision(
        self,
        identity: dict[str, Any],
        kind: str,
        recovery: dict[str, Any],
    ) -> None:
        loop_id = identity.get("loop_id")
        projector = getattr(self, "pending_decision_projector", None)
        if loop_id and projector is not None:
            await projector.project(
                str(loop_id),
                {"type": kind, "recovery": recovery},
            )

    async def ensure_assembly_task(self) -> dict[str, Any]:


        async with self.session_factory() as session:
            workspace = await self._ensure_assembly_workspace(session)
            thread = await self._ensure_assembly_thread(session, workspace)
            await session.commit()
            return await self._task_payload(session, thread, workspace)

    async def _ensure_assembly_workspace(self, session: AsyncSession) -> DesktopWorkspace:

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
                workspace_id=new_id(), path=path, display_name=_ASSEMBLY_WORKSPACE_DISPLAY
            )
            session.add(workspace)
            await session.flush()
        return workspace

    async def _ensure_assembly_thread(self, session: AsyncSession, workspace: DesktopWorkspace) -> DesktopThread:

        thread = await session.scalar(
            select(DesktopThread).where(
                DesktopThread.workspace_id == workspace.workspace_id,
                DesktopThread.thread_id == _ASSEMBLY_THREAD_ID,
            )
        )
        if thread is None:
            thread = DesktopThread(
                task_id=new_id(), workspace_id=workspace.workspace_id,
                thread_id=_ASSEMBLY_THREAD_ID, title="无工作区模式",
            )
            session.add(thread)
            await session.flush()
        return thread

    async def resume_run(
        self,
        thread_id: str,
        resume: dict[str, Any],
        run_identity: dict[str, Any] | None = None,
    ) -> PreparedRun:


        async with self.session_factory() as session:
            task = await session.scalar(
                select(DesktopThread).where(DesktopThread.thread_id == thread_id)
            )
            if not task:
                raise _identity_unregistered(f"会话 {thread_id}")

            recovery = await self._commitment_recovery_payload(session, task)
            if recovery is not None:
                self._require_resumable(recovery, "commitment_review")
                return await self._prepare_main_resume(
                    session, task, await self._resume_workspace(session, task), resume, run_identity
                )

            must_view_recovery = await must_view_recovery_payload(
                session, task, self.checkpointer
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
                session, task, self.checkpointer
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
                    session, task, await self._resume_workspace(session, task), resume, run_identity
                )

            access_recovery = await main_pending_interrupt(
                session, task, self.checkpointer, APPROVAL_TYPE
            )
            if access_recovery is not None:
                if not isinstance(resume, dict) or resume.get("decision") not in _ACCESS_DECISIONS:
                    raise HTTPException(
                        409,
                        {
                            "code": "access_review_pending",
                            "message": "存在待批准的本机资源访问，请以批准、拒绝或取消载荷恢复",
                        },
                    )
                self._require_resumable(access_recovery, "access_review")
                return await self._prepare_main_resume(
                    session, task, await self._resume_workspace(session, task), resume, run_identity
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
                "message": (
                    "该人工决定正在处理，请等待当前运行结束"
                    if processing
                    else "父图已无法恢复该人工决定，请先显式放弃旧流程并重开"
                ),
            },
        )

    @staticmethod
    async def _resume_workspace(session: AsyncSession, task: DesktopThread) -> DesktopWorkspace:

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


        interrupted_run = await latest_main_run(session, task)
        execution_identity = identity_from_main_run(task, interrupted_run)
        equipment = dict(interrupted_run.equipment if interrupted_run is not None and interrupted_run.equipment else
                         (task.ui_state or {}).get(_MAIN_RUNTIME_EQUIPMENT_KEY) or {})
        if not equipment:


            skills = self._normalize_skill_names((task.ui_state or {}).get("skills"))
            equipment = {
                "model_name": None,
                "skills": skills,
                "skill_snapshots": self._freeze_skills(workspace.path, skills),
                "permissions": ["read"],
            }
        run_materials = RunMaterialInputs.from_equipment(equipment)
        if any(item.digest != "legacy" for item in run_materials.attached):
            run_materials = await self.material_snapshots.verify(session, run_materials, workspace.path)
        equipment.pop("must_view_materials", None)
        equipment.pop("run_image_inputs", None)
        equipment.update(run_materials.to_equipment())
        identity = dict(run_identity or {})
        requested_run_id = str(identity.get("run_id") or new_id())
        run = await session.get(DesktopRun, requested_run_id) if identity.get("run_id") else None
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
            context_checkpoint_id=(
                interrupted_run.context_checkpoint_id if interrupted_run else None
            ),
            origin_message_id=(
                interrupted_run.origin_message_id
                if interrupted_run is not None
                else run_materials.origin_message_id
            ),
            equipment=equipment,
            workspace_anchor={
                "workspace_id": workspace.workspace_id,
                "workspace_path": workspace.path,
                **({"compression_resolution_id": identity["resolution_id"]} if identity.get("resolution_id") else {}),
            },
        )
        session.add(run)
        await session.commit()
        projection = MaterialContextProjector.project(run_materials, workspace.path)
        material_context = projection.policy_text
        memory_base_prompt = _MAIN_SYSTEM_PROMPT
        required = list(run_materials.images.required)
        if required:
            material_context += _must_view_prompt(
                [
                    {"material_id": item.material_id, "relative_path": item.relative_path}
                    for item in required
                ]
            )
        factory = self._build_agent_factory(
            task.task_id, run.agent_id, workspace.path, equipment, memory_base_prompt,
            material_context, "main",
        )
        context = self._governed_context(
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
            extras={
                "skills": equipment.get("skills") or [],
            },
        )
        if run.origin == "direct_user":
            self._attach_session_mode_resolver(context, run, equipment)

        project_run_material_context(
            context,
            run_materials,
            self._model_supports_image_input(equipment.get("model_name")),
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
            payload=self._run_payload(run),
        )

    @staticmethod
    def _execution_workspace_path(value: str) -> str:
        path = Path(value).resolve(strict=True)
        if not path.is_dir():
            raise HTTPException(422, "执行 Workspace Slot 路径不是目录")
        return str(path)

    async def abandon_commitment(self, thread_id: str) -> dict[str, Any]:

        async with self.session_factory() as session:
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
        await self.checkpointer.adelete_thread(
            commitment_subgraph_thread_id(execution_identity.thread_id)
        )
        return {"ok": True, "thread_id": thread_id}

    async def retry_agent(self, agent_id: str) -> PreparedRun:
        return await self.session_patrol.restart(agent_id)

    async def continue_agent(self, agent_id: str, message: str, run_id: str | None = None, checkpoint_id: str | None = None) -> PreparedRun:
        return await self.session_patrol.continue_from(agent_id, message, source_run_id=run_id, checkpoint_id=checkpoint_id)

    async def cancel_run(self, run_id: str) -> dict[str, Any]:

        self.run_manager.cancel(run_id)
        async with self.session_factory() as session:
            run = await session.get(DesktopRun, run_id)
            if not run:
                raise HTTPException(404, "运行不存在")
            if run.status not in _TERMINAL_STATUSES:
                run.status = "interrupted"
                await session.commit()
            return self._run_payload(run)

    async def list_agents(self, task_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            agents = (
                await session.scalars(
                    select(PatrolAgent).where(PatrolAgent.task_id == task_id).order_by(PatrolAgent.created_at)
                )
            ).all()
            result = []
            for agent in agents:
                latest = await session.scalar(
                    select(DesktopRun)
                    .where(DesktopRun.agent_id == agent.agent_id)
                    .order_by(DesktopRun.created_at.desc())
                )
                result.append({
                    "agent_id": agent.agent_id,
                    "checkpoint_ns": agent.checkpoint_ns,
                    "mode": agent.mode,
                    "curation_policy": agent.curation_policy,
                    "permissions": agent.equipment.get("permissions", ["read"]),
                    "created_at": agent.created_at.isoformat() if agent.created_at else None,
                    "latest_run": self._run_payload(latest) if latest else None,
                })
        for item in result:
            if item["mode"] == "context_curator":
                detail = await self.context_patrol.detail(item["agent_id"], limit=1)
                item.update({
                    "control_state": detail["control_state"],
                    "health_state": detail["health_state"],
                    "root_context": detail["root_context"],
                    "managed_context": detail["managed_context"],
                    "observed_checkpoint_id": detail["observed_checkpoint_id"],
                    "desired_checkpoint_id": detail["desired_checkpoint_id"],
                    "prepared_checkpoint_id": detail["prepared_checkpoint_id"],
                    "published_checkpoint_id": detail["published_checkpoint_id"],
                    "latest_revision": detail["latest_revision"],
                    "last_error": detail["last_error"],
                })
        return result

    async def agent_history(self, agent_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            agent = await session.get(PatrolAgent, agent_id)
            if not agent:
                raise HTTPException(404, "小兵不存在")
            task = await session.get(DesktopThread, agent.task_id)
            mode = agent.mode
        if mode == "context_curator":
            return await self.context_patrol.history(agent_id)
        async with self.session_factory() as session:
            latest = await session.scalar(select(DesktopRun).where(DesktopRun.agent_id == agent_id).order_by(DesktopRun.created_at.desc()))
            execution_thread = latest.execution_thread_id if latest and latest.execution_thread_id else task.thread_id
            execution_namespace = latest.checkpoint_ns if latest else agent.checkpoint_ns
        return await self.get_checkpoint_messages(execution_thread, execution_namespace)

    async def get_run(self, run_id: str) -> DesktopRun:
        async with self.session_factory() as session:
            run = await session.get(DesktopRun, run_id)
            if not run:
                raise HTTPException(404, "运行不存在")
            return run

    async def enroll_material(self, task_id: str, body: MaterialCreate) -> dict[str, Any]:
        async with self.session_factory() as session:
            task, workspace = await self._get_task_entities(session, task_id)
            path = self._resolve_workspace_path(workspace.path, body.path)
            if not path.is_file():
                raise HTTPException(422, "材料必须是工作区内已存在的文件")
            relative = path.relative_to(Path(workspace.path)).as_posix()
            existing = await session.scalar(
                select(DesktopMaterial).where(
                    DesktopMaterial.task_id == task_id, DesktopMaterial.relative_path == relative
                )
            )
            if existing:
                return self._material_payload(existing, workspace.path)
            if body.retention == "irreplaceable":
                await self._ensure_git(Path(workspace.path), body.confirm_git_init)
            material = DesktopMaterial(
                material_id=new_id(), task_id=task.task_id, relative_path=relative,
                reading_mode=body.reading_mode, instruction_mode=body.instruction_mode,
                retention=body.retention, digest=self._digest(path),
                git_ref=f"refs/focus/materials/{new_id()}",
            )
            session.add(material)
            await session.flush()
            if material.retention == "irreplaceable":
                await self._save_material_version(session, material, workspace.path, "enroll")
            await session.commit()
            return self._material_payload(material, workspace.path)

    async def list_materials(self, task_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            _, workspace = await self._get_task_entities(session, task_id)
            materials = (
                await session.scalars(
                    select(DesktopMaterial).where(DesktopMaterial.task_id == task_id).order_by(DesktopMaterial.created_at)
                )
            ).all()
            payloads = [self._material_payload(item, workspace.path) for item in materials]
        groups = await self.material_groups.list(task_id)
        memberships = {
            membership["material_id"]: membership
            for group in groups
            for membership in group["memberships"]
        }
        history = await self.list_material_history(task_id)
        first_use: dict[str, dict[str, Any]] = {}
        for item in history:
            first_use.setdefault(item["material_id"], item)
        for payload in payloads:
            membership = memberships.get(payload["material_id"])
            source = first_use.get(payload["material_id"])
            payload["custom_group_id"] = membership["group_id"] if membership else None
            payload["custom_position"] = membership["position"] if membership else None
            payload["first_run_id"] = source["run_id"] if source else None
            payload["first_run_created_at"] = source["created_at"] if source else None
        return payloads

    async def list_material_history(
        self, task_id: str, material_id: str | None = None
    ) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            await self._get_task_entities(session, task_id)
            if material_id is not None:
                material = await session.get(DesktopMaterial, material_id)
                if material is not None and material.task_id != task_id:
                    raise HTTPException(404, "材料不存在或不属于当前任务")
                historical_task_id = await session.scalar(
                    select(RunMaterialBinding.task_id).where(
                        RunMaterialBinding.material_id_snapshot == material_id
                    ).limit(1)
                )
                if historical_task_id is not None and historical_task_id != task_id:
                    raise HTTPException(404, "材料历史不存在或不属于当前任务")
            return await self.run_material_history.list_for_task(session, task_id, material_id)

    async def update_material(self, material_id: str, body: MaterialUpdate) -> dict[str, Any]:
        async with self.session_factory() as session:
            material = await session.get(DesktopMaterial, material_id)
            if not material:
                raise HTTPException(404, "材料不存在")
            task, workspace = await self._get_task_entities(session, material.task_id)
            if body.retention == "irreplaceable" and material.retention != "irreplaceable":
                await self._ensure_git(Path(workspace.path), body.confirm_git_init)
                material.retention = body.retention
                await self._save_material_version(session, material, workspace.path, "protect")
            material.reading_mode = body.reading_mode
            material.instruction_mode = body.instruction_mode
            material.retention = body.retention
            await session.commit()
            return self._material_payload(material, workspace.path)

    async def clear_material(self, material_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            material, workspace = await self._get_material_entities(session, material_id)
            if material.retention != "irreplaceable":
                raise HTTPException(409, "仅不可遗失材料使用置空操作")
            path = Path(workspace.path, *Path(material.relative_path).parts)
            path.write_bytes(b"")
            await self._save_material_version(session, material, workspace.path, "clear")
            material.needs_confirmation = False
            await session.commit()
            return self._material_payload(material, workspace.path)

    async def delete_material(self, material_id: str) -> None:
        async with self.session_factory() as session:
            material, workspace = await self._get_material_entities(session, material_id)
            if material.retention == "irreplaceable":
                raise HTTPException(409, "不可遗失材料不能删除，请置空或先解除保护")
            path = Path(workspace.path, *Path(material.relative_path).parts)
            if path.exists():
                path.unlink()
            await session.delete(material)
            await session.commit()

    async def material_versions(self, material_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            if not await session.get(DesktopMaterial, material_id):
                raise HTTPException(404, "材料不存在")
            versions = (
                await session.scalars(
                    select(MaterialVersion)
                    .where(MaterialVersion.material_id == material_id)
                    .order_by(MaterialVersion.created_at.desc())
                )
            ).all()
            return [self._version_payload(version) for version in versions]

    async def restore_material(self, material_id: str, version_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            material, workspace = await self._get_material_entities(session, material_id)
            version = await session.get(MaterialVersion, version_id)
            if not version or version.material_id != material_id:
                raise HTTPException(404, "材料版本不存在")
            content = await self._git_bytes(Path(workspace.path), "cat-file", "blob", version.object_id)
            path = Path(workspace.path, *Path(material.relative_path).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            await self._save_material_version(session, material, workspace.path, "restore")
            material.needs_confirmation = False
            await session.commit()
            return self._material_payload(material, workspace.path)

    async def _prepare(
        self, run: DesktopRun, thread_id: str, workspace_id: str, workspace_path: str,
        messages: list[dict[str, Any]], base_prompt: str, equipment: dict[str, Any],
        checkpoint_ns: str, agent_role: str, checkpoint_id: str | None = None,
        allow_global_config: bool = False,
    ) -> PreparedRun:


        run_materials = RunMaterialInputs.from_equipment(equipment)
        if agent_role == "main":
            if any(item.digest != "legacy" for item in run_materials.attached):
                async with self.session_factory() as session:
                    run_materials = await self.material_snapshots.verify(session, run_materials, workspace_path)
                equipment = {**equipment, **run_materials.to_equipment()}
            projection = MaterialContextProjector.project(run_materials, workspace_path)
            material_context = projection.policy_text
        else:
            if agent_role == "patrol" and "_patrol_material_policy" in equipment:
                material_context = equipment["_patrol_material_policy"]
            else:
                material_context, _ = await self._material_context(run.task_id)
        image_inputs = run_materials.images
        required = list(image_inputs.required)
        if required:
            material_context = material_context + _must_view_prompt(
                [
                    {"material_id": item.material_id, "relative_path": item.relative_path}
                    for item in required
                ]
            )
        factory = self._build_agent_factory(
            run.task_id, run.agent_id, workspace_path, equipment, base_prompt,
            material_context, agent_role,
            preparation_observer=self.session_patrol.preparation_guard(equipment) if agent_role == "patrol" else None,
        )
        context = self._governed_context(
            thread_id=thread_id,
            run=run,
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            permissions=list(equipment.get("permissions") or ["read"]),
            access_mode=equipment.get("access_mode"),
            checkpoint_ns=checkpoint_ns,
            agent_role=agent_role,
            model_name=equipment.get("model_name") or run.model_name,
            allow_global_config=allow_global_config,
            extras={
                "skills": equipment.get("skills") or [],
                **({"checkpoint_id": checkpoint_id} if checkpoint_id is not None else {}),
            },
        )
        if agent_role == "main" and run.origin == "direct_user":
            self._attach_session_mode_resolver(context, run, equipment)

        project_run_material_context(
            context,
            run_materials,
            self._model_supports_image_input(equipment.get("model_name") or run.model_name),
        )
        body = RunCreateRequest(
            input={"messages": bind_run_inputs(run, messages)},
            context=context,
            stream_mode=["messages-tuple", "values"],
        )
        return PreparedRun(body=body, thread_id=thread_id, agent_factory=factory, payload=self._run_payload(run))

    def _attach_session_mode_resolver(
        self, context: dict[str, Any], run: DesktopRun, equipment: dict[str, Any],
    ) -> None:
        security = security_context_of(context)
        context["security_context"] = replace(
            security,
            extras={
                **security.extras,
                "session_mode_resolver": self._session_mode_resolver(
                    run.task_id, _resolve_access_mode(equipment.get("access_mode")),
                ),
            },
        )

    def _session_mode_resolver(self, task_id: str, initial: AccessMode):
        async def current() -> AccessMode:
            async with self.session_factory() as session:
                task = await session.get(DesktopThread, task_id)
                if task is None:
                    raise RuntimeError(f"会话已不存在: {task_id}")
                saved = (task.ui_state or {}).get("access_mode")
                return _resolve_access_mode(saved) if saved else initial

        return current

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


        profile = ExecutionProfile(
            authorization=AuthorizationIdentity(
                workspace=Path(workspace_path),
                roots=workspace_roots(Path(workspace_path), allow_global_config=allow_global_config),
                permissions=tuple(permissions),
                access_mode=AccessMode(str(access_mode)) if access_mode else AccessMode.READ_ONLY,
                agent_role=agent_role,
            ),
            routing=RoutingIdentity(
                thread_id=thread_id,
                workspace_id=workspace_id,
                agent_id=run.agent_id,
                task_id=run.task_id,
                checkpoint_ns=checkpoint_ns,
                run_id=run.run_id,
            ),
            model_name=model_name,
        )
        runtime_extras = {**extras, "context_revision_ref": {"revision_id": run.context_revision_id,
                          "checkpoint_id": run.context_checkpoint_id, "context_id": run.task_id}}
        runtime_extras["origin_message_id"] = run.origin_message_id
        if hasattr(self, "session_factory"):
            runtime_extras["tool_execution_ledger"] = ToolExecutionLedger(self.session_factory)
        return assemble_run_context(profile, runtime_extras, dispatch_hints={"swarm_depth": swarm_depth})

    def _build_agent_factory(
        self, task_id: str, agent_id: str, workspace_path: str, equipment: dict[str, Any],
        base_prompt: str, material_context: str, agent_role: str,
        preparation_observer=None,
    ) -> Callable[[], Awaitable[CompiledStateGraph]]:


        permissions = equipment.get("permissions") or ["read"]
        model_name = equipment.get("model_name")
        collab_tools = self.agent_collab.build_collab_tools(agent_role)
        include_mailbox = agent_role in ("main", "teammate", "worker")

        async def factory() -> CompiledStateGraph:
            tools = select_workspace_tools(permissions)

            if agent_role != "patrol":
                tools = [*tools, web_search, web_fetch]
            if agent_role == "main":


                pooled = await get_available_tools()
                pool_tools = execution_pool_tools(pooled, include_builtin=False)
                tools = [*tools, *self._build_patrol_reader_tools(task_id),
                         *self._build_swarm_reader_tools(task_id),
                         build_spawn_agent_tool(), *self._build_swarm_tools(),
                         *pool_tools]
            tools = [*tools, *collab_tools]
            catalog = build_task_skill_catalog(workspace_path)


            if agent_role == "main":
                task_skill_names = frozenset(catalog)
                middlewares = None
            else:
                task_skill_names = None
                middlewares = []
            additional_middlewares = [build_tool_error_middleware()]
            if agent_role == "main":
                image_inputs = RunMaterialInputs.from_equipment(equipment).images
                additional_middlewares.append(build_image_attachment_middleware())
                if image_inputs.required_ids:
                    additional_middlewares.append(build_must_view_completion_middleware())


                    tools = [*tools, report_must_view_images]
            if agent_role == "main" and self.app_config.compression.enabled:
                from focus.agents.compression.gate import build_compression_gate

                additional_middlewares.append(
                    build_compression_gate(
                        context_window=self._compression_context_window(model_name),
                        threshold_ratio=self.app_config.compression.threshold_ratio,
                    )
                )
            return await make_lead_agent(
                model_name=model_name,
                tools=tools,
                system_prompt=base_prompt,
                middlewares=middlewares,
                additional_middlewares=additional_middlewares,
                app_config=self.app_config,
                middleware_skill_names=task_skill_names,
                frozen_contexts=desktop_contexts(equipment, material_context),
                world_skill_catalog=skill_catalog_records(catalog),
                inbox_middleware=DurableInboxMiddleware(AgentInbox(self.session_factory, self.checkpointer)) if include_mailbox else None,
                attempt_middleware=ModelAttemptMiddleware(ModelAttemptJournal(self.session_factory, self.checkpointer)),
                preparation_observer=preparation_observer,
            )

        return factory

    def _model_supports_image_input(self, model_name: str | None) -> bool:
        try:
            model = self.app_config.get_model(
                model_name or self.app_config.resolve_default_model_name()
            )
        except (KeyError, ValueError):
            return False
        return bool(model.supports_image_input)

    def _compression_context_window(self, model_name: str | None) -> int | None:

        try:
            model = self.app_config.get_model(
                model_name or self.app_config.resolve_default_model_name()
            )
        except (KeyError, ValueError):
            return None
        return model.context_window


    _TEAMMATE_PROMPT = (
        "你是 Focus 的 Teammate，隶属于主 Agent 领导的团队。独立完成任务，"
        "任务完成后必须调用 send_message 向主 Agent 汇报结果；"
        "重大计划先用 request_plan_approval 请主 Agent 批准；"
        "如需结束工作可 request_shutdown。"
    )
    _WORKER_PROMPT = (
        "你是 Focus 的 Worker，服务于主 Agent（Coordinator）的任务板。"
        "用 claim_task 认领 pending 任务并执行，完成后用 complete_task 提交结果；"
        "任务完成后调用 send_message 向主 Agent 汇报。"
    )

    def _build_swarm_tools(self) -> list[BaseTool]:


        @tool
        async def spawn_teammate(task: str, runtime: ToolRuntime[dict], system_prompt: str | None = None) -> str:
            """派生一个持续存在的 Teammate 独立执行任务（后台运行），返回其 agent_id；它完成工作后会经消息向你汇报。"""
            return await self._spawn_swarm(task, system_prompt, "teammate", runtime)

        @tool
        async def spawn_worker(task: str, runtime: ToolRuntime[dict], system_prompt: str | None = None) -> str:
            """派生一个持续存在的 Worker（后台运行），返回其 agent_id；它可认领并完成任务板任务。"""
            return await self._spawn_swarm(task, system_prompt, "worker", runtime)

        @tool
        async def wake_agent(agent_id: str, message: str, runtime: ToolRuntime[dict]) -> str:
            """唤醒一个常驻 Agent（teammate/worker）并下达新指令；其历史与权限自动延续，可继续工作。"""
            context = runtime.context
            if not isinstance(context, dict) or not context.get("workspace"):
                raise RuntimeError("缺少工作区上下文: runtime.context['workspace']")
            return await self._wake_swarm(agent_id, message, context)

        @tool
        async def wait_for_swarm(
            agent_ids: list[str], runtime: ToolRuntime[dict], timeout_seconds: int = 30
        ) -> str:
            """有界等待 Teammate/Worker 状态变化，返回运行、任务板和主 Agent 未读消息快照。"""
            context = runtime.context
            if not isinstance(context, dict) or not context.get("task_id"):
                raise RuntimeError("缺少协作上下文: runtime.context['task_id']")
            task_id = context["task_id"]
            snapshot = await self._wait_for_swarm(task_id, agent_ids, timeout_seconds)
            return json.dumps(snapshot, ensure_ascii=False)

        return [
            *declare_all_effects([spawn_teammate, spawn_worker, wake_agent], DELEGATED_EXECUTION_EFFECT),
            declare_effect(wait_for_swarm, NO_LOCAL_EFFECT),
        ]

    async def _wait_for_swarm(
        self, task_id: str, agent_ids: list[str], timeout_seconds: int
    ) -> dict[str, Any]:

        if not agent_ids:
            raise ValueError("agent_ids 不能为空")
        if not 1 <= timeout_seconds <= 30:
            raise ValueError("timeout_seconds 必须在 1..30 之间")
        targets = list(dict.fromkeys(agent_ids))
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(SwarmAgent).where(SwarmAgent.agent_id.in_(targets))
                )
            ).scalars().all()
        valid = {
            row.agent_id
            for row in rows
            if row.task_id == task_id
            and row.role in ("teammate", "worker")
            and row.status == "active"
        }
        if valid != set(targets):
            raise ValueError("Agent 不存在、不属于当前任务或已停止")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        baseline = await self._swarm_snapshot(task_id, targets)
        if not self._snapshot_is_busy(baseline):
            return {"timed_out": False, **baseline}
        baseline_key = json.dumps(baseline, ensure_ascii=False, sort_keys=True)
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return {"timed_out": True, **await self._swarm_snapshot(task_id, targets)}
            await asyncio.sleep(min(0.1, remaining))
            current = await self._swarm_snapshot(task_id, targets)
            if (
                not self._snapshot_is_busy(current)
                or json.dumps(current, ensure_ascii=False, sort_keys=True) != baseline_key
            ):
                return {"timed_out": False, **current}

    async def _swarm_snapshot(self, task_id: str, agent_ids: list[str]) -> dict[str, Any]:

        async with self.session_factory() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            runs = (
                await session.execute(
                    select(DesktopRun)
                    .where(DesktopRun.agent_id.in_(agent_ids))
                    .order_by(DesktopRun.created_at.desc(), DesktopRun.run_id.desc())
                )
            ).scalars().all()
            board_tasks = (
                await session.execute(
                    select(AgentBoardTask)
                    .where(AgentBoardTask.thread_task_id == task_id)
                    .order_by(AgentBoardTask.created_at, AgentBoardTask.board_task_id)
                )
            ).scalars().all()
            messages = (
                await session.execute(
                    select(AgentMessage)
                    .where(
                        AgentMessage.task_id == task_id,
                        AgentMessage.to_agent == f"main:{task_id}",
                        AgentMessage.read_at.is_(None),
                    )
                    .order_by(AgentMessage.created_at, AgentMessage.message_id)
                )
            ).scalars().all()

        latest: dict[str, DesktopRun] = {}
        for row in runs:
            latest.setdefault(row.agent_id, row)
        return {
            "agents": [
                {
                    "agent_id": agent_id,
                    "run_id": latest[agent_id].run_id if agent_id in latest else None,
                    "status": latest[agent_id].status if agent_id in latest else None,
                }
                for agent_id in agent_ids
            ],
            "board_tasks": [
                {
                    "board_task_id": row.board_task_id,
                    "status": row.status,
                    "claimed_by": row.claimed_by,
                    "description": row.description,
                    "requirements": row.requirements,
                    "result": row.result,
                }
                for row in board_tasks
            ],
            "unread_messages": [
                {
                    "message_id": row.message_id,
                    "from_agent": row.from_agent,
                    "kind": row.kind,
                    "content": row.content,
                }
                for row in messages
            ],
        }

    @staticmethod
    def _snapshot_is_busy(snapshot: dict[str, Any]) -> bool:
        return any(
            agent["status"] in ("pending", "running")
            for agent in snapshot["agents"]
        )

    async def _spawn_swarm(
        self, task: str, system_prompt: str | None, role: str, runtime: Any
    ) -> str:


        parent = security_context_of(runtime.context)
        task_id = runtime.context.get("task_id")
        workspace_id = parent.routing.workspace_id
        if not task_id:
            raise RuntimeError("缺少协作上下文: runtime.context['task_id']（任务身份）")
        if not workspace_id:
            raise RuntimeError("缺少协作上下文: security_context.routing.workspace_id（工作区身份）")

        agent_id = new_id()
        await self.agent_collab.create_swarm_agent(
            agent_id, task_id, role, list(parent.authorization.permissions),
            access_mode=str(parent.authorization.access_mode),
        )
        equipment = {
            "model_name": parent.model_name,
            "skills": [],
            "skill_snapshots": [],
            "permissions": list(parent.authorization.permissions),
            "access_mode": str(parent.authorization.access_mode),
        }
        prompt = system_prompt or (self._TEAMMATE_PROMPT if role == "teammate" else self._WORKER_PROMPT)
        run_id = await self._launch_swarm_run(
            task_id, agent_id, role, task, prompt, workspace_id,
            str(parent.authorization.workspace), equipment,
        )
        logger.info("持久 Agent 已派生: agent_id='%s' role=%s run_id='%s'", agent_id, role, run_id)
        return agent_id

    async def _wake_swarm(self, agent_id: str, message: str, context: dict[str, Any]) -> str:


        agent_row = await self.agent_collab.get_swarm_agent(agent_id)
        if agent_row is None:
            raise _identity_unregistered(f"派生执行主体 {agent_id}")
        if agent_row.status == "stopped":
            raise HTTPException(409, "该 Agent 已停止")
        parent = security_context_of(context)
        equipment = {
            "model_name": parent.model_name,
            "skills": [],
            "skill_snapshots": [],
            "permissions": list(agent_row.permissions or ["read"]),
            "access_mode": agent_row.access_mode,
        }
        prompt = self._TEAMMATE_PROMPT if agent_row.role == "teammate" else self._WORKER_PROMPT
        run_id = await self._launch_swarm_run(
            agent_row.task_id, agent_id, agent_row.role, message, prompt,
            parent.routing.workspace_id, str(parent.authorization.workspace), equipment,
        )
        logger.info("持久 Agent 已唤醒: agent_id='%s' run_id='%s'", agent_id, run_id)
        return run_id

    async def _auto_wake_swarm(self, agent_id: str, message: str, depth: int) -> None:


        try:
            agent_row = await self.agent_collab.get_swarm_agent(agent_id)
            if agent_row is None or agent_row.status == "stopped":
                return
            async with self.session_factory() as session:
                busy = await session.scalar(
                    select(DesktopRun.run_id)
                    .where(
                        DesktopRun.agent_id == agent_id,
                        DesktopRun.status.in_(["pending", "running"]),
                    )
                    .limit(1)
                )
                if busy:
                    return
                task_row = await session.get(DesktopThread, agent_row.task_id)
                if not task_row:
                    return
                workspace_row = await session.get(DesktopWorkspace, task_row.workspace_id)
                if not workspace_row:
                    return
            equipment = {
                "model_name": None,
                "skills": [],
                "skill_snapshots": [],
                "permissions": list(agent_row.permissions or ["read"]),
            }
            prompt = self._TEAMMATE_PROMPT if agent_row.role == "teammate" else self._WORKER_PROMPT
            await self._launch_swarm_run(
                agent_row.task_id, agent_id, agent_row.role, message, prompt,
                workspace_row.workspace_id, workspace_row.path, equipment, swarm_depth=depth,
            )
            logger.info("自动唤醒已触发: agent_id='%s' depth=%s", agent_id, depth)
        except Exception:
            logger.warning("自动唤醒失败: agent_id=%s", agent_id, exc_info=True)

    async def _launch_swarm_run(
        self, task_id: str, agent_id: str, role: str, task_text: str,
        system_prompt: str, workspace_id: str, workspace_path: str, equipment: dict[str, Any],
        swarm_depth: int = 0,
    ) -> str:


        if await self.agent_collab.is_agent_stopped(agent_id):
            raise HTTPException(409, "该 Agent 已停止")
        async with self.session_factory() as session:
            task_row = await session.get(DesktopThread, task_id)
            if not task_row:
                raise _identity_unregistered(f"任务 {task_id}")
            agent_row = await session.get(SwarmAgent, agent_id)
            if agent_row is None:
                raise _identity_unregistered(f"派生执行主体 {agent_id}")

            checkpoint_ns = agent_row.checkpoint_ns
            thread_id = task_row.thread_id
            self._require_run_admission()
            run = DesktopRun(
                run_id=new_id(), task_id=task_id, agent_id=agent_id, kind=role,
                status="pending", input_messages=[{"role": "human", "content": task_text}],
                model_name=equipment.get("model_name"),
                origin="swarm", execution_thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                context_revision_id=task_row.current_revision_id,
                equipment=equipment,
                workspace_anchor={
                    "workspace_id": workspace_id,
                    "workspace_path": workspace_path,
                },
            )
            session.add(run)
            await session.commit()

        checkpoint_id = await select_checkpoint_base(
            self.checkpointer, thread_id, checkpoint_ns,
        )
        factory = self._build_agent_factory(
            task_id, agent_id, workspace_path, equipment, system_prompt, "", role,
        )
        langgraph_context = self._governed_context(
            thread_id=thread_id,
            run=run,
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            permissions=list(equipment.get("permissions") or ["read"]),
            access_mode=equipment.get("access_mode"),
            checkpoint_ns=checkpoint_ns,
            agent_role=role,
            model_name=equipment.get("model_name") or run.model_name,
            allow_global_config=False,
            extras={
                "skills": equipment.get("skills") or [],
                "checkpoint_id": checkpoint_id,
                "app_config": self.app_config,
            },
            swarm_depth=swarm_depth,
        )
        record = await execute_prepared_run(
            RunCreateRequest(
                input={"messages": [{"role": "human", "content": task_text}]},
                context=langgraph_context,
                stream_mode=["values"],
            ),
            thread_id,
            RunExecutionResources(
                bridge=self.bridge,
                run_manager=self.run_manager,
                checkpointer=self.checkpointer,
                store=self.store,
                app_config=self.app_config,
            ),
            factory,
            runner=run_agent,
        )
        self.attach_run_sync(record)
        return run.run_id

    def attach_run_sync(self, record: RunRecord) -> None:

        task = asyncio.create_task(self._sync_run_status(record))
        self._sync_tasks.add(task)

    async def _sync_run_status(self, record: RunRecord) -> None:
        try:
            await record.task
        except asyncio.CancelledError:
            pass
        finally:
            try:
                activity_task = getattr(record, "loop_activity_task", None)
                if activity_task is not None:
                    try:
                        await activity_task
                    except Exception:
                        logger.exception("Loop Run 活动事件提交失败: run_id=%s", record.run_id)
                if not hasattr(self, "run_lifecycle") and hasattr(self, "_set_run_status"):
                    kind, task_id = await self._set_run_status(
                        record.run_id,
                        record.status.value,
                        record.error,
                        record.prompt_input_tokens,
                        record.prompt_cache_hit_tokens,
                    )
                    if kind == "main":
                        await self.contexts.evolution.publish_latest_checkpoint(
                            task_id,
                            ContextRevisionOriginKind.RUN_SETTLED,
                            origin_id=record.run_id,
                        )
                        await self.context_patrol.notify_stable_context_checkpoint(task_id)
                    return
                settlement = await self.run_lifecycle.finalize(record)
                async with self.session_factory.begin() as session:
                    await self._run_dispatch_repository.settle_by_run(session, record.run_id)
                if settlement.kind == "main":
                    try:
                        await self.context_patrol.notify_stable_context_checkpoint(
                            settlement.task_id
                        )
                    except Exception:
                        logger.warning(
                            "登记稳定 Context checkpoint 失败: context_id=%s",
                            settlement.task_id,
                            exc_info=True,
                        )
            except Exception:
                logger.exception("收敛 Run 终态失败: run_id=%s", record.run_id)
            finally:
                self._sync_tasks.discard(asyncio.current_task())

    async def _material_context(self, task_id: str) -> tuple[str, str]:


        async with self.session_factory() as session:
            _, workspace = await self._get_task_entities(session, task_id)
            materials = (
                await session.scalars(
                    select(DesktopMaterial)
                    .where(DesktopMaterial.task_id == task_id)
                    .order_by(DesktopMaterial.created_at)
                )
            ).all()
        lines = []
        upload_names: list[str] = []
        for material in materials:
            lines.append(
                _material_policy_line(
                    resolve_material_path(workspace.path, material.relative_path),
                    material.reading_mode,
                    material.instruction_mode,
                )
            )
            upload_names.append(material.relative_path)
        uploads_tag = (
            "<current_uploads>\n" + "\n".join(upload_names) + "\n</current_uploads>"
            if upload_names
            else ""
        )
        return "\n".join(lines), uploads_tag

    def _build_patrol_reader_tools(self, task_id: str) -> list[BaseTool]:
        @tool
        async def list_patrol_agents() -> str:
            """列出当前任务中用户投放的小兵及其最新已提交运行状态。"""
            return json.dumps(await self.list_agents(task_id), ensure_ascii=False)

        @tool
        async def read_patrol_agent_history(agent_id: str) -> str:
            """读取当前任务指定小兵已提交的完整消息历史，不包含草稿和实时 token。"""
            agents = await self.list_agents(task_id)
            if agent_id not in {item["agent_id"] for item in agents}:
                raise ValueError("该小兵不属于当前任务")
            return json.dumps(await self.agent_history(agent_id), ensure_ascii=False)

        return declare_all_effects([list_patrol_agents, read_patrol_agent_history], NO_LOCAL_EFFECT)

    def _build_swarm_reader_tools(self, task_id: str) -> list[BaseTool]:
        @tool
        async def read_swarm_agent_history(agent_id: str) -> str:
            """读取当前任务指定 Teammate/Worker 已提交的完整消息历史，不读取小兵。"""
            agent = await self.agent_collab.get_swarm_agent(agent_id)
            if agent is None or agent.task_id != task_id:
                raise ValueError("该协作 Agent 不属于当前任务")
            thread_id = await self._task_thread_id(task_id)
            messages = await self.get_checkpoint_messages(thread_id, agent.checkpoint_ns)
            return json.dumps(messages, ensure_ascii=False)

        return declare_all_effects([read_swarm_agent_history], NO_LOCAL_EFFECT)

    async def _task_thread_id(self, task_id: str) -> str:
        async with self.session_factory() as session:
            task = await session.get(DesktopThread, task_id)
            if task is None:
                raise ValueError("任务不存在")
            return task.thread_id

    async def _validate_attachments(
        self, session: AsyncSession, task_id: str, messages: list[dict[str, Any]]
    ) -> None:
        _, workspace = await self._get_task_entities(session, task_id)
        for message in messages:
            for item in message.get("files", []):
                path = item.get("path") if isinstance(item, dict) else item
                if not path or not self._resolve_workspace_path(workspace.path, path).exists():
                    raise HTTPException(422, f"附件引用失效: {path}")

    def _require_model(self, model_name: str | None):


        try:
            return self.app_config.get_model(
                model_name or self.app_config.resolve_default_model_name()
            )
        except ValueError as exc:
            raise HTTPException(422, {"code": "model_entry_missing", "message": str(exc)}) from exc
        except KeyError as exc:
            from focus.config.app_config import available_model_names

            raise HTTPException(
                422,
                {
                    "code": "model_entry_missing",
                    "model_name": model_name,
                    "message": (
                        f"模型条目已不存在: '{model_name}'，请在界面重新选择模型；"
                        f"可用条目: {available_model_names(self.app_config)}"
                    ),
                },
            ) from exc

    def _validate_model_window(self, model_name: str | None, estimate: int) -> None:
        model = self._require_model(model_name)
        if model.context_window is not None and estimate > model.context_window:
            raise HTTPException(422, {"code": "context_window_exceeded", "estimate": estimate, "limit": model.context_window})

    @staticmethod
    def _normalize_skill_names(value: Any) -> list[str]:
        if value is None or value == "auto":
            return []
        if not isinstance(value, list) or any(not isinstance(name, str) for name in value):
            raise HTTPException(422, {"code": "invalid_skills"})
        return list(dict.fromkeys(value))

    def _freeze_skills(self, workspace: str | Path, selected: Any) -> list[dict[str, str]]:
        snapshots, unavailable = resolve_task_skills(
            build_task_skill_catalog(workspace), self._normalize_skill_names(selected)
        )
        if unavailable:
            raise HTTPException(422, {"code": "skill_unavailable", "names": unavailable})
        return snapshots

    def _normalize_equipment(self, equipment: dict[str, Any]) -> dict[str, Any]:
        model_name = equipment.get("model_name") or self.app_config.resolve_default_model_name()
        self._require_model(model_name)
        raw_permissions = equipment.get("permissions")
        permissions = list(dict.fromkeys(["read"] if raw_permissions is None else raw_permissions))
        invalid = set(permissions) - {"read", "write", "host_command"}
        if invalid:
            raise HTTPException(422, f"未知权限: {', '.join(sorted(invalid))}")
        return {
            "model_name": model_name,
            "skills": self._normalize_skill_names(equipment.get("skills")),
            "permissions": permissions,
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
        return task, workspace

    async def _execution_entities(
        self, session: AsyncSession, task_id: str
    ) -> tuple[DesktopThread, DesktopWorkspace]:

        try:
            return await self._get_task_entities(session, task_id)
        except HTTPException as exc:
            raise _identity_unregistered(f"任务 {task_id}") from exc

    async def _get_material_entities(
        self, session: AsyncSession, material_id: str
    ) -> tuple[DesktopMaterial, DesktopWorkspace]:
        material = await session.get(DesktopMaterial, material_id)
        if not material:
            raise HTTPException(404, "材料不存在")
        _, workspace = await self._get_task_entities(session, material.task_id)
        return material, workspace

    @staticmethod
    def _resolve_workspace_path(root: str | Path, value: str) -> Path:
        workspace = Path(root).resolve()
        candidate = Path(value).expanduser()
        target = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
        if target != workspace and workspace not in target.parents:
            raise HTTPException(403, "路径不属于当前工作区")
        return target

    @staticmethod
    def _digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    async def _ensure_git(self, workspace: Path, confirmed: bool) -> None:
        if (workspace / ".git").exists():
            return
        if not confirmed:
            raise HTTPException(409, {"code": "git_init_required", "path": str(workspace)})
        await self._git(workspace, "init")

    async def _save_material_version(
        self, session: AsyncSession, material: DesktopMaterial, workspace_path: str, source: str
    ) -> MaterialVersion:
        workspace = Path(workspace_path)
        path = Path(workspace, *Path(material.relative_path).parts)
        digest = self._digest(path)
        object_id = (await self._git(workspace, "hash-object", "-w", "--", str(path))).strip()
        tree_line = f"100644 blob {object_id}\tcontent\n"
        tree_id = (await self._git(workspace, "mktree", stdin=tree_line)).strip()
        parent = None
        try:
            parent = (await self._git(workspace, "rev-parse", "--verify", material.git_ref)).strip()
        except RuntimeError:
            pass
        args = ["commit-tree", tree_id, "-m", f"Focus material {material.material_id}: {source}"]
        if parent:
            args[2:2] = ["-p", parent]
        commit_id = (await self._git(workspace, *args)).strip()
        await self._git(workspace, "update-ref", material.git_ref, commit_id)
        version = MaterialVersion(
            version_id=new_id(), material_id=material.material_id, commit_id=commit_id,
            object_id=object_id, digest=digest, source=source,
        )
        material.digest = digest
        session.add(version)
        return version

    async def _git(self, workspace: Path, *args: str, stdin: str | None = None) -> str:
        def run() -> str:
            env = os.environ.copy()
            env.update({
                "GIT_AUTHOR_NAME": "Focus", "GIT_AUTHOR_EMAIL": "focus@localhost",
                "GIT_COMMITTER_NAME": "Focus", "GIT_COMMITTER_EMAIL": "focus@localhost",
            })
            result = subprocess.run(
                ["git", "-C", str(workspace), *args], input=stdin, capture_output=True,
                text=True, timeout=30, shell=False, env=env,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
            return result.stdout
        return await asyncio.to_thread(run)

    async def _git_bytes(self, workspace: Path, *args: str) -> bytes:
        def run() -> bytes:
            result = subprocess.run(
                ["git", "-C", str(workspace), *args], capture_output=True,
                timeout=30, shell=False,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.decode(errors="replace"))
            return result.stdout
        return await asyncio.to_thread(run)

    async def _watch_materials(self) -> None:
        while True:
            try:
                await asyncio.sleep(2)
                async with self.session_factory() as session:
                    rows = (
                        await session.execute(
                            select(DesktopMaterial, DesktopThread, DesktopWorkspace)
                            .join(DesktopThread, DesktopMaterial.task_id == DesktopThread.task_id)
                            .join(DesktopWorkspace, DesktopThread.workspace_id == DesktopWorkspace.workspace_id)
                            .where(DesktopMaterial.retention == "irreplaceable")
                        )
                    ).all()
                    changed = False
                    for material, _, workspace in rows:
                        try:
                            path = Path(workspace.path, *Path(material.relative_path).parts)
                            if not path.exists():
                                latest = await session.scalar(
                                    select(MaterialVersion)
                                    .where(MaterialVersion.material_id == material.material_id)
                                    .order_by(MaterialVersion.created_at.desc())
                                )
                                if latest:
                                    path.parent.mkdir(parents=True, exist_ok=True)
                                    path.write_bytes(await self._git_bytes(Path(workspace.path), "cat-file", "blob", latest.object_id))
                                    material.digest = self._digest(path)
                                    material.needs_confirmation = True
                                    changed = True
                            else:
                                digest = self._digest(path)
                                if material.digest and digest != material.digest:
                                    await self._save_material_version(session, material, workspace.path, "external")
                                    changed = True
                            self._material_watch_failures.discard(material.material_id)
                        except asyncio.CancelledError:
                            raise
                        except Exception:

                            failures = getattr(self, "_material_watch_failures", None)
                            if failures is None:
                                failures = self._material_watch_failures = set()
                            if material.material_id not in failures:
                                logger.warning(
                                    "材料监测失败，跳过该材料: material_id=%s",
                                    material.material_id,
                                    exc_info=True,
                                )
                                failures.add(material.material_id)
                            continue
                    if changed:
                        await session.commit()
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(3)

    @staticmethod
    def _workspace_payload(workspace: DesktopWorkspace) -> dict[str, Any]:
        return {"workspace_id": workspace.workspace_id, "path": workspace.path, "display_name": workspace.display_name}

    async def _task_payload(
        self, session: AsyncSession, task: DesktopThread, workspace: DesktopWorkspace
    ) -> dict[str, Any]:
        active = await session.scalar(
            select(DesktopRun)
            .where(
                DesktopRun.task_id == task.task_id,
                DesktopRun.agent_id == f"main:{task.task_id}",
                DesktopRun.status.in_(["pending", "running"]),
            )
            .order_by(DesktopRun.created_at.desc())
        )
        latest_direct_user = await session.scalar(
            select(DesktopRun)
            .where(
                DesktopRun.task_id == task.task_id,
                DesktopRun.agent_id == f"main:{task.task_id}",
                DesktopRun.origin == "direct_user",
            )
            .order_by(DesktopRun.created_at.desc(), DesktopRun.run_id.desc())
        )
        ui_state = dict(task.ui_state or {})
        ui_state.pop(_MAIN_RUNTIME_EQUIPMENT_KEY, None)
        recovery = await self._commitment_recovery_payload(session, task)
        compression_recovery = await compression_recovery_payload(
            session, task, self.checkpointer
        )
        must_view_recovery = await must_view_recovery_payload(
            session, task, self.checkpointer
        )
        access_recovery = await main_pending_interrupt(
            session, task, self.checkpointer, APPROVAL_TYPE
        )
        return {
            "task_id": task.task_id, "workspace_id": task.workspace_id, "workspace_path": workspace.path,
            "workspace_name": workspace.display_name, "thread_id": task.thread_id, "title": task.title,
            "harness_mode": "assembly" if task.thread_id == _ASSEMBLY_THREAD_ID else "workspace",
            "lifecycle": (
                "deleted" if task.deleted_at is not None
                else "archived" if task.archived_at is not None
                else "active"
            ),
            "ui_state": ui_state,
            "active_run": self._run_payload(active) if active else None,
            "latest_direct_user_run": self._run_payload(latest_direct_user) if latest_direct_user else None,
            "pending_commitment_review": (
                recovery["review"] if recovery and recovery["status"] == "resumable" else None
            ),
            "commitment_recovery": recovery,
            "pending_compression": (
                compression_recovery["request"]
                if compression_recovery and compression_recovery["status"] in ("resumable", "orphaned")
                else None
            ),
            "compression_recovery": compression_recovery,
            "pending_must_view_report": (
                must_view_recovery["request"]
                if must_view_recovery and must_view_recovery["status"] in ("resumable", "orphaned")
                else None
            ),
            "must_view_recovery": must_view_recovery,
            "pending_access_review": (
                access_recovery["request"]
                if access_recovery and access_recovery["status"] in ("resumable", "orphaned")
                else None
            ),
            "access_recovery": access_recovery,
        }

    async def _commitment_recovery_payload(
        self, session: AsyncSession, task: DesktopThread
    ) -> dict[str, Any] | None:

        latest = await latest_main_run(session, task)
        execution_identity = identity_from_main_run(task, latest)
        config = {
            "configurable": {
                "thread_id": commitment_subgraph_thread_id(
                    execution_identity.thread_id
                ),
            }
        }
        try:
            checkpoint = await self.checkpointer.aget_tuple(config)
        except Exception:
            logger.warning(
                "读取承诺子图 checkpoint 失败: thread_id=%s",
                execution_identity.thread_id,
                exc_info=True,
            )
            return None
        if checkpoint is None:
            return None
        values = dict(checkpoint.checkpoint.get("channel_values", {}))
        try:
            stage = int(values.get("awaiting_human") or 0)
        except (TypeError, ValueError):
            return None
        if stage < 1 or stage > 9:
            return None
        if latest is not None and latest.status in {"pending", "running"}:
            status = "processing"
        elif latest is not None and latest.status == "interrupted":
            status = "resumable"
        else:
            status = "orphaned"
        source_text = str(values.get("source_text") or "").strip()
        review = _checkpoint_commitment_review(checkpoint, stage)
        return {
            "status": status,
            "stage": stage,
            "review": review or _human_payload(stage, values),
            "restart_message": f"/commit {source_text}" if source_text else "/commit ",
        }

    def _draft_payload(self, draft: PatrolDraft) -> dict[str, Any]:
        from backend.app.desktop.session_patrol.repository import DraftRepository
        default_policy = ContextCurationPolicy().model_dump(mode="json")
        equipment = {
            **draft.equipment,
            "skills": DesktopService._normalize_skill_names(draft.equipment.get("skills")),
        }
        equipment.pop("skill_snapshots", None)
        return {
            "draft_id": draft.draft_id, "task_id": draft.task_id, "status": draft.status,
            "authoring_document": DraftRepository.document(draft).model_dump(mode="json"),
            "draft_revision": draft.draft_revision, "history_schema_version": 2 if draft.authoring_document is not None else 1,
            "mode": draft.mode,
            "curation_policy": draft.curation_policy or (
                default_policy if draft.mode == "context_curator" else {}
            ),
            "default_curation_policy": default_policy,
            "default_curation_model_name": next(
                (
                    model.name
                    for model in self.app_config.models
                    if model.curation_default
                ),
                None,
            ),
            "system_prompt": draft.system_prompt, "history_messages": draft.history_messages,
            "final_human_message": draft.final_human_message, "equipment": equipment,
            "source_checkpoint_id": draft.source_checkpoint_id, "token_estimate": draft.token_estimate,
        }

    @staticmethod
    def _run_payload(run: DesktopRun) -> dict[str, Any]:
        return {
            "run_id": run.run_id, "task_id": run.task_id, "agent_id": run.agent_id,
            "deployment_id": run.deployment_id, "kind": run.kind, "status": run.status,
            "model_name": run.model_name, "error": run.error,
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
    def _run_payload_with_dispatch(cls, run: DesktopRun, dispatch: RunDispatch | None) -> dict[str, Any]:
        payload = cls._run_payload(run)
        payload["dispatch"] = None if dispatch is None else {
            "dispatch_id": dispatch.dispatch_id,
            "status": dispatch.status,
            "attempt": dispatch.attempt,
            "error": dispatch.error,
            "claimed_by": dispatch.claimed_by,
            "fencing_token": dispatch.fencing_token,
        }
        return payload

    @staticmethod
    def _material_payload(material: DesktopMaterial, workspace_path: str) -> dict[str, Any]:
        path = resolve_material_path(workspace_path, material.relative_path)
        return {
            "material_id": material.material_id, "task_id": material.task_id,
            "path": str(path),
            "relative_path": material.relative_path, "reading_mode": material.reading_mode,
            "instruction_mode": material.instruction_mode, "retention": material.retention,
            "digest": material.digest, "git_ref": material.git_ref,
            "needs_confirmation": material.needs_confirmation,
            "is_image": is_image_name(material.relative_path),
            "material_kind": MaterialKindClassifier.classify(material.relative_path),
            "size_bytes": path.stat().st_size if path.is_file() else 0,
        }

    async def store_uploaded_material(
        self, task_id: str, upload: UploadFile
    ) -> dict[str, Any]:
        stored = await self.material_uploads.store(task_id, upload)
        return self._material_payload(stored.material, stored.workspace_path)

    async def resolve_material_content(
        self, task_id: str, material_id: str
    ) -> ResolvedMaterialContent:
        return await self.material_contents.resolve(task_id, material_id)

    async def validate_image_material(self, task_id: str, material_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            _, workspace = await self._get_task_entities(session, task_id)
            inputs = await self.run_images.resolve(
                session, task_id, workspace.path, [material_id], []
            )
        return inputs.attached[0].to_json()

    @staticmethod
    def _version_payload(version: MaterialVersion) -> dict[str, Any]:
        return {
            "version_id": version.version_id, "commit_id": version.commit_id,
            "object_id": version.object_id, "digest": version.digest, "source": version.source,
            "created_at": version.created_at.isoformat() if version.created_at else None,
        }
