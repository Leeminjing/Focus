"""
本文件对外提供 DesktopService，聚合桌面工作区、Context、普通 Patrol、Context 策展 Patrol、
材料版本与 Claude Code 三套 subagent 机制（树形 spawn / Swarm / Coordinator）业务。

输入为已初始化的 PostgreSQL session factory、LangGraph checkpointer/store、StreamBridge、
RunManager 和 AppConfig；输出为供 routes.py 调用的异步业务方法以及 PreparedRun（统一
编排入口 start_run 的输入 + agent_factory 闭包）。
具体工作流为：登记真实宿主机工作区与线程，复制已提交 checkpoint 形成冻结草稿，
准备无沙箱工作区 Agent 的装配参数（经统一执行链路 worker.run_agent 执行，
独立 checkpoint namespace 隔离），并用 Git 隐藏引用保护不可遗失材料。
四套机制装配边界经 agent_role 区分：main（spawn 三件套 + 协作工具 + Mailbox 注入
+ 联网工具 web_search/web_fetch + MCP 远端工具 + 压缩门）、teammate/worker（持久派生
Agent，协作工具 + Mailbox 注入 + 联网工具）、patrol（小兵机制，工作区工具仅，无联网
无 MCP）。持久派生（spawn_teammate/spawn_worker）创建 SwarmAgent 身份并经
_launch_swarm_run 启动独立命名空间的后台 run；工具错误 middleware 保证可恢复调用闭合，
主任务运行前的 checkpoint preflight 可从最近合法祖先恢复受损历史；主 Agent 中断恢复
经 resume_run 按载荷分派承诺层与压缩流程。
示例：`service = DesktopService(...); await service.open_draft(task_id)`。
"""

from __future__ import annotations

import asyncio
from contextvars import Context
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Any, Awaitable, Callable
import uuid

from fastapi import HTTPException
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
from backend.app.desktop.context_service import ContextService
from backend.app.desktop.context_patrol_service import ContextPatrolService
from backend.app.desktop.context_curator import (
    CurationEngineError,
    CurationSourceProjector,
    build_curation_input,
    estimate_curation_tokens,
)
from backend.app.desktop.memory import MemoryService, _MAIN_RUNTIME_MEMORY_KEY
from backend.app.desktop.models import (
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
    PatrolAgent,
    PatrolDraft,
    SwarmAgent,
)
from backend.app.desktop.skills import build_task_skill_catalog, resolve_task_skills
from backend.app.desktop.storage_values import normalize_json_storage_value
from backend.app.desktop.tool_error_provider import build_tool_error_middleware
from focus.runtime.checkpointer.namespaced import NamespacedCheckpointer
from focus.runtime.runs.schemas import DisconnectMode
from focus.runtime.runs.worker import run_agent
from focus.tools.builtins.spawn_agent_tool import build_spawn_agent_tool
from backend.app.gateway.routers.thread_runs import RunCreateRequest
from focus.agents.commitment.middleware import commitment_subgraph_thread_id
from focus.agents.commitment.workflow import _human_payload
from focus.agents.compression import apply_compression_ranges, hit_keyword_message_ids
from focus.agents.compression.schemas import validate_apply_decision
from backend.app.desktop.material_files import (
    EmptyUpload,
    OversizedImage,
    guard_upload,
    prepare_attachment_target,
    resolve_material_path,
)
from focus.agents.must_view import MUST_VIEW_CONTEXT_KEY, build_must_view_middleware
from focus.images import image_mime_from_name, is_image_name
from focus.messages import (
    estimate_images_tokens,
    estimate_raw_tokens,
    strip_image_payloads,
)
from focus.agents.lead import make_lead_agent
from focus.config.app_config import AppConfig
from focus.runtime.runs.events import (
    deserialize_messages,
    serialize_message,
    validate_messages,
)
from focus.runtime.runs.limits import DEFAULT_AGENT_RECURSION_LIMIT
from focus.runtime.runs.manager import RunManager, RunRecord
from focus.runtime.stream_bridge.base import StreamBridge
from focus.tools import get_available_tools
from focus.tools.builtins.web_tools import web_fetch, web_search
from focus.tools.builtins.workspace_tools import select_workspace_tools
from backend.app.desktop.prompts import (
    MAIN_SYSTEM_PROMPT as _MAIN_SYSTEM_PROMPT,
    ASSEMBLY_SYSTEM_PROMPT as _ASSEMBLY_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)

_MAIN_RUNTIME_EQUIPMENT_KEY = "_main_run_equipment"
_TERMINAL_STATUSES = frozenset({"success", "error", "interrupted"})


_ASSEMBLY_WORKSPACE_DISPLAY = "无工作区模式"
_ASSEMBLY_THREAD_ID = "focus-assembly"


def _spatial_focus_prompt(focus: dict[str, Any] | None) -> str:
    """把插件提供的当前空间焦点注入本轮系统提示，不改写用户原始消息。"""
    if not focus:
        return ""
    required = ("spatial_id", "content_ref", "page", "x", "y", "kind", "status")
    if any(key not in focus for key in required):
        return ""
    payload = {key: focus[key] for key in required}
    return (
        "\n\n<current_spatial_focus>\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n"
        "用户刚刚在内容查看器中明确选择了这个空间锚点。"
        "对‘这里/这个/这一块/刚才那里’等指代，必须优先围绕该坐标解释，"
        "不得重新猜测或迁移坐标。\n"
        "</current_spatial_focus>"
    )


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
    """读取 LangGraph 已持久化的最新承诺 interrupt 原始载荷。"""
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


def estimate_tokens(system_prompt: str, messages: list[dict[str, Any]], final_message: str) -> int:
    image_tokens = estimate_images_tokens(messages)
    counted = strip_image_payloads(messages) if image_tokens else messages
    raw = system_prompt + final_message + json.dumps(counted, ensure_ascii=False, separators=(",", ":"))
    return estimate_raw_tokens(raw, len(messages)) + image_tokens


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
        self.contexts = ContextService(session_factory, checkpointer, app_config)
        self.context_patrol = ContextPatrolService(
            session_factory, self.contexts, checkpointer, store, bridge, run_manager, app_config
        )
        # Agent 协作（Mailbox 消息 / 任务板）：工具构建与未读消息回合注入；
        # swarm_launcher 注入消息驱动自动唤醒（send_message/publish_task 落表后触发目标 run）
        self.agent_collab = AgentCollab(session_factory, swarm_launcher=self._auto_wake_swarm)
        # 全局记忆库：CRUD + 来源解析 + 压缩总结 + 注入块（context_reader 复用 contexts.snapshot）
        self.memory = MemoryService(session_factory, app_config, self._memory_context_messages)
        # 仅持有 DB 终态同步任务（运行注册表/取消由 RunManager 负责）
        self._sync_tasks: set[asyncio.Task] = set()
        self._watcher: asyncio.Task | None = None
        self._material_watch_failures: set[str] = set()

    async def start(self) -> None:
        async with self.session_factory() as session:
            await session.execute(
                update(DesktopRun)
                .where(DesktopRun.status.in_(["pending", "running"]))
                .values(status="interrupted", error="桌面后端重启，原运行无法继续")
            )
            await session.commit()
        await self.context_patrol.start()
        self._watcher = asyncio.create_task(self._watch_materials())

    async def close(self) -> None:
        await self.context_patrol.close()
        background = list(self._sync_tasks)
        if self._watcher:
            self._watcher.cancel()
            background.append(self._watcher)
        for task in background:
            if not task.done():
                task.cancel()
        await asyncio.gather(*background, return_exceptions=True)

    async def equipment(self) -> dict[str, Any]:
        try:
            raw = json.loads(Path("extensions_config.json").read_text(encoding="utf-8"))
            skills = sorted(name for name, cfg in raw.get("skills", {}).items() if cfg.get("enabled"))
        except (OSError, json.JSONDecodeError):
            skills = []
        return {
            "models": [
                {
                    "name": model.name,
                    "display_name": model.display_name,
                    "context_window": model.context_window,
                    "curation_default": model.curation_default,
                }
                for model in self.app_config.models
            ],
            "skills": skills,
            "permissions": ["read", "write", "host_command"],
            "tools": await self._equipment_tools(),
        }

    async def _equipment_tools(self) -> list[dict[str, Any]]:
        """返回装备信息中的工具清单（含 name/label/source），供前端区分内置与自定义工具。

        工作流:
            (1) 经 get_available_tools() 聚合全部可用工具（builtin/custom/mcp/plugin）
            (2) 每个 ToolInfo 转 {name, label, source}
            (3) 按 name 去重、保持顺序返回
        """
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

    async def create_thread(self, workspace_id: str, thread_id: str | None, title: str) -> dict[str, Any]:
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
            task = DesktopThread(task_id=new_id(), workspace_id=workspace_id, thread_id=identity, title=title)
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
        snapshot = await self.contexts.snapshot(task_id)
        payload["messages"] = snapshot["messages"]
        payload["context"] = await self.contexts.get(task_id)
        return payload

    async def _memory_context_messages(self, context_id: str) -> list[dict[str, Any]]:
        """记忆来源解析器：给定 context_id（根/派生 context 的 task_id），返回其 serialized messages。"""
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
            task = await session.get(DesktopThread, task_id)
            if not task:
                raise HTTPException(404, "任务不存在")
            persisted = dict(ui_state)
            runtime_equipment = (task.ui_state or {}).get(
                _MAIN_RUNTIME_EQUIPMENT_KEY
            )
            if runtime_equipment is not None:
                persisted[_MAIN_RUNTIME_EQUIPMENT_KEY] = runtime_equipment
            task.ui_state = persisted
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
        """关键字快捷压缩预览：机械命中主图消息中含该词的消息。

        输入:
            task_id: str — 桌面任务 id
            keyword: str — 要命中的关键词
            case_sensitive: bool — 是否区分大小写

        输出:
            dict — {"keyword", "hit_ids": [命中消息 id], "hit_messages": [命中消息快照]}
            纯机械命中，不改动任何消息。
        """
        async with self.session_factory() as session:
            task, _ = await self._get_task_entities(session, task_id)
        messages = await self.get_checkpoint_messages(task.thread_id, "")
        hit_ids = hit_keyword_message_ids(messages, keyword, case_sensitive)
        hit_set = set(hit_ids)
        hit_messages = [message for message in messages if message.get("id") in hit_set]
        return {"keyword": keyword, "hit_ids": hit_ids, "hit_messages": hit_messages}

    async def quick_compression_apply(self, task_id: str, ranges: list[dict[str, Any]], scrub_terms: list[str] | None = None) -> dict[str, Any]:
        """关键字快捷压缩应用：校验范围后写回主图 checkpoint。

        输入:
            task_id: str — 桌面任务 id
            ranges: list[dict] — 既有压缩语义范围（{source_ids, replacement|restore|delete}）
            scrub_terms: list[str] | None — 摘要阶段使用的禁用词；保留用于接口兼容，不改写来源

        输出:
            dict — 应用后的主图消息快照；主 Agent 运行中或范围非法时抛 HTTPException

        具体工作流:
            (1) 主 Agent 空闲校验（存在 pending/running main run → 409）
            (2) 读主图 checkpoint 消息 → deserialize → validate_apply_decision 校验范围
            (3) apply_compression_ranges 以原始消息编译新 messages，完整来源只保存在块元数据中
                → graph.aupdate_state 写回；模型调用前由压缩门剥离该元数据
            (4) 返回写回后的消息快照（含压缩块/墓碑元数据）
        """
        async with self.session_factory() as session:
            task, _ = await self._get_task_entities(session, task_id)
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
        messages = await self.get_checkpoint_messages(task.thread_id, "")
        base_messages = deserialize_messages(messages)
        # 状态一致性：范围引用的 source_ids 必须是当前顶层消息，否则面板快照已过期（上下文已变化）
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
        config = {"configurable": {"thread_id": task.thread_id, "checkpoint_ns": ""}}
        graph = await make_lead_agent(
            tools=[], system_prompt="", middlewares=[], app_config=self.app_config
        )
        graph.checkpointer = self.checkpointer
        await graph.aupdate_state(config, update)
        await self.context_patrol.notify_stable_context_checkpoint(task_id)
        return {"messages": await self.get_checkpoint_messages(task.thread_id, "")}

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
            config = {"configurable": {"thread_id": task.thread_id, "checkpoint_ns": ""}}
            checkpoint = await self.checkpointer.aget_tuple(config)
            messages: list[dict[str, Any]] = []
            checkpoint_id = None
            if checkpoint:
                checkpoint_id = checkpoint.config.get("configurable", {}).get("checkpoint_id")
                values = checkpoint.checkpoint.get("channel_values", {})
                messages = normalize_json_storage_value(
                    [serialize_message(message) for message in values.get("messages", [])]
                )
            default_model = self.app_config.resolve_default_model_name()
            equipment = {
                "model_name": default_model,
                "skills": [],
                "permissions": ["read"],
            }
            draft = PatrolDraft(
                draft_id=new_id(),
                task_id=task_id,
                history_messages=messages,
                source_checkpoint_id=checkpoint_id,
                equipment=equipment,
                token_estimate=estimate_tokens("", messages, ""),
            )
            session.add(draft)
            await session.commit()
            return self._draft_payload(draft)

    async def update_draft(self, draft_id: str, body: DraftUpdate) -> dict[str, Any]:
        async with self.session_factory() as session:
            draft = await session.get(PatrolDraft, draft_id)
            if not draft or draft.status != "editing":
                raise HTTPException(404, "可编辑草稿不存在")
            _, workspace = await self._get_task_entities(session, draft.task_id)
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

            safe_system_prompt = normalize_json_storage_value(body.system_prompt)
            safe_history_messages = normalize_json_storage_value(body.history_messages)
            safe_final_human_message = normalize_json_storage_value(body.final_human_message)
            validate_messages(safe_history_messages)
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
        """用领域默认策略创建并投放一份独立策展草稿，不覆盖用户正在编辑的普通草稿。"""
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
        """校验来源与模型窗口，并把草稿规范化为唯一的 Context 策展形态。"""
        root_context_id = await self.contexts.resolve_chat_root(draft.task_id)
        checkpoint_id = await self.contexts.current_checkpoint_id(root_context_id)
        if checkpoint_id is None:
            raise HTTPException(409, "根 Context 尚无稳定 checkpoint")
        snapshot = await self.contexts.snapshot(root_context_id, checkpoint_id)
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

    async def deploy(self, draft_id: str, deployment_id: str) -> PreparedRun:
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
        async with self.session_factory() as session:
            existing = await session.scalar(select(DesktopRun).where(DesktopRun.deployment_id == deployment_id))
            if existing:
                return PreparedRun(
                    body=RunCreateRequest(input={"messages": []}), thread_id="",
                    agent_factory=None, payload=self._run_payload(existing),
                )
            draft = await session.get(PatrolDraft, draft_id)
            if not draft or draft.status != "editing":
                raise HTTPException(404, "可投放草稿不存在")
            if not draft.final_human_message.strip():
                raise HTTPException(422, "最后一条 HumanMessage 不能为空")
            validate_messages(draft.history_messages)
            _, workspace = await self._get_task_entities(session, draft.task_id)
            snapshots = self._freeze_skills(workspace.path, draft.equipment.get("skills", []))
            equipment = {**draft.equipment, "skill_snapshots": snapshots}
            estimate = estimate_tokens(
                prompt_with_skills(draft.system_prompt, snapshots),
                draft.history_messages,
                draft.final_human_message,
            )
            self._validate_model_window(equipment.get("model_name"), estimate)
            await self._validate_attachments(session, draft.task_id, draft.history_messages)
            agent_id = new_id()
            frozen = [*draft.history_messages, {"role": "human", "content": draft.final_human_message}]
            agent = PatrolAgent(
                agent_id=agent_id,
                task_id=draft.task_id,
                checkpoint_ns=f"patrol:{agent_id}",
                system_prompt=draft.system_prompt,
                frozen_messages=frozen,
                equipment=equipment,
                source_checkpoint_id=draft.source_checkpoint_id,
                mode="standard",
                curation_policy={},
            )
            run = DesktopRun(
                run_id=new_id(), task_id=draft.task_id, agent_id=agent_id, deployment_id=deployment_id,
                kind="patrol", status="pending", input_messages=frozen,
                model_name=equipment.get("model_name"),
            )
            draft.status = "deployed"
            session.add_all([agent, run])
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                winner = await session.scalar(select(DesktopRun).where(DesktopRun.deployment_id == deployment_id))
                if winner:
                    return PreparedRun(
                        body=RunCreateRequest(input={"messages": []}), thread_id="",
                        agent_factory=None, payload=self._run_payload(winner),
                    )
                raise
            task_row = await session.get(DesktopThread, draft.task_id)
            thread_id = task_row.thread_id
            workspace_id = workspace.workspace_id
            workspace_path = workspace.path
        return await self._prepare(
            run, thread_id, workspace_id, workspace_path, frozen,
            draft.system_prompt, agent.equipment, agent.checkpoint_ns, "patrol",
        )

    async def start_main_run(
        self, task_id: str, message: str | list[dict[str, Any]], model_name: str | None,
        permissions: list[str], skills: list[str], spatial_focus: dict[str, Any] | None = None,
        memory_ids: list[str] | None = None,
        must_view_material_ids: list[str] | None = None,
    ) -> PreparedRun:
        async with self.session_factory() as session:
            task_row, workspace = await self._get_task_entities(session, task_id)
            authoritative_checkpoint_id = await self.contexts.ensure_runnable(session, task_id)
            recovery = await self._commitment_recovery_payload(session, task_row)
            if recovery is not None:
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
                raise HTTPException(
                    409,
                    {
                        "code": "compression_request_pending",
                        "recovery": compression_recovery["status"],
                        "message": "存在待确认的压缩请求，请先完成压缩或取消后继续",
                    },
                )
            snapshots = self._freeze_skills(workspace.path, skills)
            must_view = await self._resolve_must_view_materials(
                session, task_id, workspace.path, must_view_material_ids or []
            )
            equipment = {
                "model_name": model_name,
                "skills": list(dict.fromkeys(skills)),
                "skill_snapshots": snapshots,
                "permissions": permissions,
                "must_view_materials": must_view,
            }
            run = DesktopRun(
                run_id=new_id(), task_id=task_id, agent_id=f"main:{task_id}", kind="main", status="pending",
                input_messages=[{"role": "human", "content": message}], model_name=model_name,
            )
            session.add(run)
            task_row.ui_state = {
                **(task_row.ui_state or {}),
                _MAIN_RUNTIME_EQUIPMENT_KEY: equipment,
            }
            if memory_ids is not None:
                state = dict(task_row.ui_state or {})
                state[_MAIN_RUNTIME_MEMORY_KEY] = list(dict.fromkeys(memory_ids))
                task_row.ui_state = state
            await session.commit()
            thread_id = task_row.thread_id
            workspace_id = workspace.workspace_id
            workspace_path = workspace.path
        checkpoint_id = authoritative_checkpoint_id or await select_checkpoint_base(
            self.checkpointer, thread_id
        )
        is_assembly = task_row.thread_id == _ASSEMBLY_THREAD_ID
        base_prompt = (_ASSEMBLY_SYSTEM_PROMPT if is_assembly else _MAIN_SYSTEM_PROMPT) + _spatial_focus_prompt(spatial_focus)
        base_prompt = await self._apply_memory_block(base_prompt, memory_ids)
        return await self._prepare(
            run, thread_id, workspace_id, workspace_path, run.input_messages,
            base_prompt, equipment, "", "main", checkpoint_id, allow_global_config=is_assembly,
        )

    async def ensure_assembly_task(self) -> dict[str, Any]:
        """确保「无工作区模式」保留工作区与其任务存在，返回该任务 payload。

        无工作区模式被建模为保留工作区（路径 = 全局配置家目录 ~/.focus）下的普通任务，
        由此复用任务页全套能力（派生 context / 草稿 / 材料 / 小兵 / 技能）。
        """
        async with self.session_factory() as session:
            workspace = await self._ensure_assembly_workspace(session)
            thread = await self._ensure_assembly_thread(session, workspace)
            await session.commit()
            return await self._task_payload(session, thread, workspace)

    async def _ensure_assembly_workspace(self, session: AsyncSession) -> DesktopWorkspace:
        """获取/创建装配保留 workspace（路径 = 全局配置家目录 ~/.focus），并尝试确保该目录存在。"""
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
        """获取/创建装配会话线程（单一稳定 thread，历史累积）。"""
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

    async def resume_run(self, thread_id: str, resume: dict[str, Any]) -> PreparedRun:
        """主 Agent 中断恢复：按 resume 载荷分派承诺层或压缩流程。

        输入:
            thread_id: str — 桌面任务登记的唯一 thread 标识
            resume: dict — 承诺层 {decision: approve|revise, feedback?, replacement?}
                或压缩 {"type": "compression", "decision": apply|cancel, ...}

        输出:
            PreparedRun — 携带 RunCreateRequest(resume=...) 与主 Agent 装配闭包

        工作流:
            (1) 先按承诺子图 checkpoint 探测承诺审批；命中走承诺校验原路
            (2) 否则按主图 checkpoint 探测压缩请求；命中且载荷为 compression 类型时走压缩校验
            (3) 皆无可恢复时 409；校验通过后经 _prepare_main_resume 组装主 Agent resume run
        """
        async with self.session_factory() as session:
            task = await session.scalar(
                select(DesktopThread).where(DesktopThread.thread_id == thread_id)
            )
            if not task:
                raise HTTPException(404, "任务不存在")
            recovery = await self._commitment_recovery_payload(session, task)
            if recovery is None:
                compression_recovery = await compression_recovery_payload(
                    session, task, self.checkpointer
                )
                if (
                    compression_recovery is None
                    or not isinstance(resume, dict)
                    or resume.get("type") != "compression"
                ):
                    raise HTTPException(409, "无可恢复的承诺流程")
                if compression_recovery["status"] == "processing":
                    raise HTTPException(
                        409,
                        {
                            "code": "compression_request_processing",
                            "message": "压缩请求正在处理，请等待当前运行结束",
                        },
                    )
                workspace = await session.get(DesktopWorkspace, task.workspace_id)
                if not workspace:
                    raise HTTPException(404, "工作区不存在")
                return await self._prepare_main_resume(session, task, workspace, resume)
            if recovery["status"] != "resumable":
                code = (
                    "commitment_review_processing"
                    if recovery["status"] == "processing"
                    else "commitment_review_orphaned"
                )
                message = (
                    "承诺审批正在处理，请等待当前运行结束"
                    if recovery["status"] == "processing"
                    else "父图已无法恢复旧承诺审批，请先显式放弃旧流程并重开"
                )
                raise HTTPException(
                    409,
                    {
                        "code": code,
                        "message": message,
                    },
                )
            workspace = await session.get(DesktopWorkspace, task.workspace_id)
            if not workspace:
                raise HTTPException(404, "工作区不存在")
            return await self._prepare_main_resume(session, task, workspace, resume)

    async def _prepare_main_resume(
        self,
        session: AsyncSession,
        task: DesktopThread,
        workspace: DesktopWorkspace,
        resume: dict[str, Any],
    ) -> PreparedRun:
        """组装主 Agent resume run 的公共尾部：equipment 沿用、新建 DesktopRun、
        agent_factory 与 RunCreateRequest(resume=...)。"""
        equipment = dict(
            (task.ui_state or {}).get(_MAIN_RUNTIME_EQUIPMENT_KEY) or {}
        )
        if not equipment:
            # 兼容修复前已进入 interrupt 的任务：尽量恢复 UI 中仍可获得的技能，
            # 其余字段沿用旧行为的默认值。
            skills = self._normalize_skill_names((task.ui_state or {}).get("skills"))
            equipment = {
                "model_name": None,
                "skills": skills,
                "skill_snapshots": self._freeze_skills(workspace.path, skills),
                "permissions": ["read"],
            }
        run = DesktopRun(
            run_id=new_id(),
            task_id=task.task_id,
            agent_id=f"main:{task.task_id}",
            kind="main",
            status="pending",
            input_messages=[],
            model_name=equipment.get("model_name"),
        )
        session.add(run)
        await session.commit()
        material_context, uploads_tag = await self._material_context(task.task_id)
        resume_memory_ids = list(
            (task.ui_state or {}).get(_MAIN_RUNTIME_MEMORY_KEY) or []
        )
        memory_base_prompt = await self._apply_memory_block(
            _MAIN_SYSTEM_PROMPT, resume_memory_ids
        )
        factory = self._build_agent_factory(
            task.task_id, run.agent_id, workspace.path, equipment, memory_base_prompt,
            material_context, "main",
        )
        body = RunCreateRequest(
            input=None,
            resume=resume,
            context={
                "model_name": equipment.get("model_name"),
                "workspace_id": workspace.workspace_id,
                "agent_id": run.agent_id,
                "task_id": task.task_id,
                "permissions": equipment.get("permissions") or ["read"],
                "skills": equipment.get("skills") or [],
                "workspace": workspace.path,
                "uploads": uploads_tag,
                "checkpoint_ns": "",
                "run_id": run.run_id,
            },
            stream_mode=["messages-tuple", "values"],
        )
        return PreparedRun(
            body=body,
            thread_id=task.thread_id,
            agent_factory=factory,
            payload=self._run_payload(run),
        )

    async def abandon_commitment(self, thread_id: str) -> dict[str, Any]:
        """显式废弃待确认承诺子图；只删除派生 thread checkpoint。"""
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
        await self.checkpointer.adelete_thread(
            commitment_subgraph_thread_id(thread_id)
        )
        return {"ok": True, "thread_id": thread_id}

    async def retry_agent(self, agent_id: str) -> PreparedRun:
        async with self.session_factory() as session:
            agent = await session.get(PatrolAgent, agent_id)
            if not agent:
                raise HTTPException(404, "小兵不存在")
            if agent.mode == "context_curator":
                raise HTTPException(409, "Context 策展 Patrol 由根 checkpoint 自动触发，不能手工重试")
            run = DesktopRun(
                run_id=new_id(), task_id=agent.task_id, agent_id=agent.agent_id, kind="patrol",
                status="pending", input_messages=agent.frozen_messages,
                model_name=agent.equipment.get("model_name"),
            )
            session.add(run)
            await session.commit()
            task_row = await session.get(DesktopThread, agent.task_id)
            workspace_row = await session.get(DesktopWorkspace, task_row.workspace_id)
            thread_id = task_row.thread_id
            workspace_id = workspace_row.workspace_id
            workspace_path = workspace_row.path
        return await self._prepare(
            run, thread_id, workspace_id, workspace_path, agent.frozen_messages,
            agent.system_prompt, agent.equipment, agent.checkpoint_ns, "patrol",
        )

    async def continue_agent(self, agent_id: str, message: str) -> PreparedRun:
        async with self.session_factory() as session:
            agent = await session.get(PatrolAgent, agent_id)
            if not agent:
                raise HTTPException(404, "小兵不存在")
            if agent.mode == "context_curator":
                raise HTTPException(409, "Context 策展 Patrol 不接受普通继续消息")
            input_messages = [{"role": "human", "content": message}]
            run = DesktopRun(
                run_id=new_id(), task_id=agent.task_id, agent_id=agent.agent_id, kind="patrol",
                status="pending", input_messages=input_messages, model_name=agent.equipment.get("model_name"),
            )
            session.add(run)
            await session.commit()
            task_row = await session.get(DesktopThread, agent.task_id)
            workspace_row = await session.get(DesktopWorkspace, task_row.workspace_id)
            thread_id = task_row.thread_id
            workspace_id = workspace_row.workspace_id
            workspace_path = workspace_row.path
        return await self._prepare(
            run, thread_id, workspace_id, workspace_path, input_messages,
            agent.system_prompt, agent.equipment, agent.checkpoint_ns, "patrol",
        )

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        # 软硬双通道取消由 RunManager 负责（abort_event + task.cancel）
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
        return await self.get_checkpoint_messages(task.thread_id, agent.checkpoint_ns)

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
            return [self._material_payload(item, workspace.path) for item in materials]

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
        """组装统一编排入口的输入：RunCreateRequest（input/context/stream_mode）+ agent_factory 闭包。

        工作流:
            (1) 查询任务材料策略，拼入运行 prompt 基础
            (2) 构建 agent_factory 闭包（工作区内置工具按权限过滤 + 角色协作工具 + 技能快照）
            (3) context 携带 workspace/workspace_id/agent_id/task_id/permissions/skills/checkpoint_ns，
                由 services.start_run 透传给 worker 与工具（ToolRuntime）
        """
        material_context, uploads_tag = await self._material_context(run.task_id)
        factory = self._build_agent_factory(
            run.task_id, run.agent_id, workspace_path, equipment, base_prompt,
            material_context, agent_role,
        )
        context = {
            "model_name": equipment.get("model_name") or run.model_name,
            "workspace_id": workspace_id,
            "agent_id": run.agent_id,
            "task_id": run.task_id,
            "permissions": equipment.get("permissions") or ["read"],
            "skills": equipment.get("skills") or [],
            "workspace": workspace_path,
            "checkpoint_ns": checkpoint_ns,
            "run_id": run.run_id,
        }
        if checkpoint_id is not None:
            context["checkpoint_id"] = checkpoint_id
        context["uploads"] = uploads_tag
        context[MUST_VIEW_CONTEXT_KEY] = equipment.get("must_view_materials") or []
        if allow_global_config:
            context["allow_global_config"] = True
        body = RunCreateRequest(
            input={"messages": messages},
            context=context,
            stream_mode=["messages-tuple", "values"],
        )
        return PreparedRun(body=body, thread_id=thread_id, agent_factory=factory, payload=self._run_payload(run))

    def _build_agent_factory(
        self, task_id: str, agent_id: str, workspace_path: str, equipment: dict[str, Any],
        base_prompt: str, material_context: str, agent_role: str,
    ) -> Callable[[], Awaitable[CompiledStateGraph]]:
        """构建 agent_factory 闭包：工作区内置工具（权限过滤）+ 角色协作工具 + Mailbox 注入。

        输入:
            task_id: str — 任务 ID（小兵读取工具与协作工具按任务过滤）
            agent_id: str — 当前 run 的 agent 标识（协作消息 from/to 依据）
            workspace_path: str — 真实工作区路径（经 ToolRuntime context 注入工具）
            equipment: dict — 模型/权限/技能快照（skill_snapshots）
            base_prompt: str — 主 prompt（主 Agent 固定模板 / 协作 Agent 提示词）
            material_context: str — 材料策略文本（可为空）
            agent_role: str — "main" / "teammate" / "worker" / "patrol"（四套机制装配边界：
                              patrol 为小兵机制，装配工作区工具仅，无协作工具、无 Mailbox 注入）

        输出:
            Callable — async 闭包，await 后返回 CompiledStateGraph

        工作流:
            (1) 按角色汇工具：main 携带小兵读取 + spawn 三件套 + 协作工具；teammate/worker 携带协作工具；
                patrol 仅工作区工具（纯净）
            (2) prompt 注入链：技能快照 → 材料策略 → Mailbox 未读消息（仅 main/teammate/worker，回合边界）
        """
        permissions = equipment.get("permissions") or ["read"]
        snapshots = equipment.get("skill_snapshots") or []
        model_name = equipment.get("model_name")
        collab_tools = self.agent_collab.build_collab_tools(agent_role)
        include_mailbox = agent_role in ("main", "teammate", "worker")

        async def factory() -> CompiledStateGraph:
            tools = select_workspace_tools(permissions)
            # 联网工具（web_search/web_fetch）装配给 main/teammate/worker，patrol 保持纯净
            if agent_role != "patrol":
                tools = [*tools, web_search, web_fetch]
            if agent_role == "main":
                # 系统级可注册工具池（自定义/mcp/插件）仅 main 装配，fail-soft 降级为空。
                # 内置工具已由上方 select_workspace_tools + web 按权限/角色装配，此处排除 source=="builtin" 以去重。
                from focus.tools.interfaces import ToolInfo

                pooled = await get_available_tools()
                pool_tools = [
                    t.tool() if isinstance(t, ToolInfo) else t
                    for t in pooled
                    if not (isinstance(t, ToolInfo) and t.source == "builtin")
                ]
                tools = [*tools, *self._build_patrol_reader_tools(task_id),
                         *self._build_swarm_reader_tools(task_id),
                         build_spawn_agent_tool(), *self._build_swarm_tools(),
                         *pool_tools]
            tools = [*tools, *collab_tools]
            prompt = prompt_with_skills(base_prompt, snapshots)
            if material_context:
                prompt = f"{prompt}\n\n<focus_material_policies>\n{material_context}\n</focus_material_policies>"
            if include_mailbox:
                mailbox_block = await self.agent_collab.load_unread_messages(agent_id, task_id)
                if mailbox_block:
                    prompt = f"{prompt}\n\n{mailbox_block}"
            # 承诺层装配：主 Agent 经共享 builder（按 commitment.enabled 条件装配，
            # skill_names 取任务技能 catalog 全量用于触发剥离）；其他角色不装配承诺层。
            if agent_role == "main":
                task_skill_names = frozenset(build_task_skill_catalog(workspace_path))
                middlewares = None
            else:
                task_skill_names = None
                middlewares = []
            # 压缩门与必需图片注入：仅主 Agent、按角色装配（patrol/swarm 不装配）
            additional_middlewares = [build_tool_error_middleware()]
            if agent_role == "main":
                additional_middlewares.append(build_must_view_middleware())
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
                system_prompt=prompt,
                middlewares=middlewares,
                additional_middlewares=additional_middlewares,
                app_config=self.app_config,
                middleware_skill_names=task_skill_names,
            )

        return factory

    def _compression_context_window(self, model_name: str | None) -> int | None:
        """按运行模型取上下文窗口；模型未知时返回 None（压缩门恒放行）。"""
        try:
            model = self.app_config.get_model(
                model_name or self.app_config.resolve_default_model_name()
            )
        except (KeyError, ValueError):
            return None
        return model.context_window

    async def _apply_memory_block(self, base_prompt: str, memory_ids: list[str] | None) -> str:
        """在 base_prompt 末尾注入 `<memory>` 块；无选中记忆时原样返回。"""
        if not memory_ids:
            return base_prompt
        block = await self.memory.build_memory_block(memory_ids)
        if not block:
            return base_prompt
        return f"{base_prompt}\n\n{block}"

    # === 机制③④：持久派生 spawn（teammate/worker）===

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
        """构建持久派生、唤醒与有界等待工具（仅 main 装配）。"""

        @tool
        async def spawn_teammate(task: str, runtime: ToolRuntime, system_prompt: str | None = None) -> str:
            """派生一个持续存在的 Teammate 独立执行任务（后台运行），返回其 agent_id；它完成工作后会经消息向你汇报。"""
            return await self._spawn_swarm(task, system_prompt, "teammate", runtime)

        @tool
        async def spawn_worker(task: str, runtime: ToolRuntime, system_prompt: str | None = None) -> str:
            """派生一个持续存在的 Worker（后台运行），返回其 agent_id；它可认领并完成任务板任务。"""
            return await self._spawn_swarm(task, system_prompt, "worker", runtime)

        @tool
        async def wake_agent(agent_id: str, message: str, runtime: ToolRuntime) -> str:
            """唤醒一个常驻 Agent（teammate/worker）并下达新指令；其历史与权限自动延续，可继续工作。"""
            context = runtime.context
            if not isinstance(context, dict) or not context.get("workspace"):
                raise RuntimeError("缺少工作区上下文: runtime.context['workspace']")
            return await self._wake_swarm(agent_id, message, context)

        @tool
        async def wait_for_swarm(
            agent_ids: list[str], runtime: ToolRuntime, timeout_seconds: int = 30
        ) -> str:
            """有界等待 Teammate/Worker 状态变化，返回运行、任务板和主 Agent 未读消息快照。"""
            context = runtime.context
            if not isinstance(context, dict) or not context.get("task_id"):
                raise RuntimeError("缺少协作上下文: runtime.context['task_id']")
            task_id = context["task_id"]
            snapshot = await self._wait_for_swarm(task_id, agent_ids, timeout_seconds)
            return json.dumps(snapshot, ensure_ascii=False)

        return [spawn_teammate, spawn_worker, wake_agent, wait_for_swarm]

    async def _wait_for_swarm(
        self, task_id: str, agent_ids: list[str], timeout_seconds: int
    ) -> dict[str, Any]:
        """等待协作状态变化；读取快照不消费发给 main 的未读消息。"""
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
        """组装非消费式协调快照。"""
        async with self.session_factory() as session:
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
            # 运行状态最后读取，避免状态与消息在相邻查询间提交时返回过时的 busy 状态。
            runs = (
                await session.execute(
                    select(DesktopRun)
                    .where(DesktopRun.agent_id.in_(agent_ids))
                    .order_by(DesktopRun.created_at.desc(), DesktopRun.run_id.desc())
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
        """创建持久 Agent 身份并启动后台 run。

        输入:
            task: str — 子任务描述（作为 HumanMessage 输入）
            system_prompt: str | None — 自定义提示词，None 时用角色默认模板
            role: str — "teammate" 或 "worker"
            runtime: ToolRuntime — 提供 workspace/permissions/model_name 上下文

        输出:
            str — 新 Agent 的 agent_id
        """
        context = runtime.context
        if not isinstance(context, dict) or not context.get("workspace"):
            raise RuntimeError("缺少工作区上下文: runtime.context['workspace']")
        permissions = list(context.get("permissions") or ["read"])
        model_name = context.get("model_name")
        task_id = context.get("task_id")
        workspace_id = context.get("workspace_id")
        workspace_path = context.get("workspace")
        if not task_id or not workspace_id:
            raise RuntimeError("缺少协作上下文: runtime.context['task_id'] / ['workspace_id']")

        agent_id = new_id()
        await self.agent_collab.create_swarm_agent(agent_id, task_id, role, permissions)
        equipment = {
            "model_name": model_name,
            "skills": [],
            "skill_snapshots": [],
            "permissions": permissions,
        }
        prompt = system_prompt or (self._TEAMMATE_PROMPT if role == "teammate" else self._WORKER_PROMPT)
        run_id = await self._launch_swarm_run(
            task_id, agent_id, role, task, prompt, workspace_id, workspace_path, equipment,
        )
        logger.info("持久 Agent 已派生: agent_id='%s' role=%s run_id='%s'", agent_id, role, run_id)
        return agent_id

    async def _wake_swarm(self, agent_id: str, message: str, context: dict[str, Any]) -> str:
        """唤醒常驻 Agent：校验身份与状态后，以新消息为输入触发其一轮新 run。

        输入:
            agent_id: str — 目标 teammate/worker 的 agent_id
            message: str — 新指令（作为 HumanMessage 输入，历史经 checkpoint 自动恢复）
            context: dict — 主 Agent 的 runtime.context（workspace_id/workspace/model_name）

        输出:
            str — 新 run 的 run_id

        工作流:
            (1) 校验持久 Agent 存在且未 stopped（409）
            (2) equipment 沿用 spawn 时持久化的 permissions（不放大）
            (3) 复用 _launch_swarm_run（同一 checkpoint 命名空间 → 多轮历史连贯）
        """
        agent_row = await self.agent_collab.get_swarm_agent(agent_id)
        if agent_row is None:
            raise HTTPException(404, "该 Agent 不存在")
        if agent_row.status == "stopped":
            raise HTTPException(409, "该 Agent 已停止")
        equipment = {
            "model_name": context.get("model_name"),
            "skills": [],
            "skill_snapshots": [],
            "permissions": list(agent_row.permissions or ["read"]),
        }
        prompt = self._TEAMMATE_PROMPT if agent_row.role == "teammate" else self._WORKER_PROMPT
        run_id = await self._launch_swarm_run(
            agent_row.task_id, agent_id, agent_row.role, message, prompt,
            context.get("workspace_id"), context.get("workspace"), equipment,
        )
        logger.info("持久 Agent 已唤醒: agent_id='%s' run_id='%s'", agent_id, run_id)
        return run_id

    async def _auto_wake_swarm(self, agent_id: str, message: str, depth: int) -> None:
        """消息驱动的自动唤醒（AgentCollab.swarm_launcher 注入）：查身份/查忙后触发目标 run。

        输入:
            agent_id: str — 消息目标（teammate/worker）
            message: str — 触发 run 的输入消息
            depth: int — 目标 run 的 swarm_depth（来源 depth+1）

        工作流:
            (1) 目标不存在或已 stopped → 跳过（仅落表）
            (2) 目标已有 pending/running run → 跳过（避免并发堆积，未读消息由该 run 回合注入消费）
            (3) 查 thread/workspace 后复用 _launch_swarm_run（depth 传入 context）
        """
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
        """经统一链路启动持久 Agent 的后台 run（独立 checkpoint 命名空间）。

        输入:
            task_id / agent_id / role / task_text / system_prompt — 派生参数
            workspace_id / workspace_path — 工作区定位
            equipment — 模型/权限/技能装备
            swarm_depth — 自动唤醒链深度（消息触发为来源+1；wake/spawn 为 0，用于防环截断）

        输出:
            str — 创建的 run_id

        工作流:
            (1) stopped 检查（关机后拒绝新 run）
            (2) 登记 DesktopRun 与 RunRecord，组装 run_agent 参数（参考 services.start_run）
            (3) 按角色构建 agent_factory（含 Mailbox 注入），NamespacedCheckpointer 隔离命名空间
        """
        if await self.agent_collab.is_agent_stopped(agent_id):
            raise HTTPException(409, "该 Agent 已停止")
        async with self.session_factory() as session:
            task_row = await session.get(DesktopThread, task_id)
            if not task_row:
                raise HTTPException(404, "任务不存在")
            thread_id = task_row.thread_id
            run = DesktopRun(
                run_id=new_id(), task_id=task_id, agent_id=agent_id, kind=role,
                status="pending", input_messages=[{"role": "human", "content": task_text}],
                model_name=equipment.get("model_name"),
            )
            session.add(run)
            await session.commit()

        checkpoint_ns = f"swarm:{agent_id}"
        checkpoint_id = await select_checkpoint_base(
            self.checkpointer, thread_id, checkpoint_ns,
        )
        factory = self._build_agent_factory(
            task_id, agent_id, workspace_path, equipment, system_prompt, "", role,
        )
        graph_input = {"messages": [HumanMessage(content=task_text)]}
        runnable_config = {
            "max_concurrency": None,
            "recursion_limit": DEFAULT_AGENT_RECURSION_LIMIT,
            "metadata": {"run_id": run.run_id},
            "configurable": {"thread_id": thread_id, "run_id": run.run_id, "checkpoint_ns": checkpoint_ns},
        }
        if checkpoint_id is not None:
            runnable_config["configurable"]["checkpoint_id"] = checkpoint_id
        langgraph_context = {
            "model_name": equipment.get("model_name") or run.model_name,
            "workspace_id": workspace_id,
            "agent_id": agent_id,
            "task_id": task_id,
            "permissions": equipment.get("permissions") or ["read"],
            "skills": equipment.get("skills") or [],
            "workspace": workspace_path,
            "checkpoint_ns": checkpoint_ns,
            "run_id": run.run_id,
            "swarm_depth": swarm_depth,
            "app_config": self.app_config,
            "user_id": None,
        }
        checkpointer = NamespacedCheckpointer(self.checkpointer, checkpoint_ns)
        record = self.run_manager.create(
            thread_id=thread_id, run_id=run.run_id,
            on_disconnect=DisconnectMode.cancel, model_name=equipment.get("model_name"),
        )
        task = asyncio.create_task(
            run_agent(
                record=record, bridge=self.bridge, run_manager=self.run_manager,
                app_config=self.app_config, graph_input=graph_input,
                runnable_config=runnable_config, stream_modes=["values"],
                langgraph_context=langgraph_context, agent_factory=factory,
                checkpointer=checkpointer, store=self.store,
            ),
            context=Context(),
        )
        record.task = task
        self.attach_run_sync(record)
        return run.run_id

    def attach_run_sync(self, record: RunRecord) -> None:
        """挂载 DB 终态同步薄任务：worker 结束后把 RunRecord 终态写入 desktop_runs。"""
        task = asyncio.create_task(self._sync_run_status(record))
        self._sync_tasks.add(task)

    async def _sync_run_status(self, record: RunRecord) -> None:
        try:
            await record.task
        except asyncio.CancelledError:
            pass  # RunManager.cancel 已置 interrupted
        finally:
            try:
                synced = await self._set_run_status(
                    record.run_id,
                    record.status.value,
                    record.error,
                    record.prompt_input_tokens,
                    record.prompt_cache_hit_tokens,
                )
                if synced is not None and synced[0] == "main":
                    try:
                        await self.context_patrol.notify_stable_context_checkpoint(synced[1])
                    except Exception:
                        logger.warning(
                            "登记稳定 Context checkpoint 失败: context_id=%s",
                            synced[1],
                            exc_info=True,
                        )
            finally:
                self._sync_tasks.discard(asyncio.current_task())

    async def _material_context(self, task_id: str) -> tuple[str, str]:
        """返回 (材料策略文本, 上传清单标签)。

        上传清单以 <current_uploads> 标签形式返回（承诺层阶段4 据此核对文件名），
        经 run context 的 uploads 字段显式传给承诺子图，不依赖 lead 历史。
        """
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
            reading = "优先完整阅读" if material.reading_mode == "full" else "优先粗略阅读，需要时仍可完整读取"
            instruction = "严格遵守" if material.instruction_mode == "strict" else "仅供参考"
            lines.append(
                f"- {Path(workspace.path, *Path(material.relative_path).parts)} | {reading} | {instruction}"
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

        return [list_patrol_agents, read_patrol_agent_history]

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

        return [read_swarm_agent_history]

    async def _task_thread_id(self, task_id: str) -> str:
        async with self.session_factory() as session:
            task = await session.get(DesktopThread, task_id)
            if task is None:
                raise ValueError("任务不存在")
            return task.thread_id

    async def _set_run_status(
        self,
        run_id: str,
        status: str,
        error: str | None = None,
        prompt_input_tokens: int = 0,
        prompt_cache_hit_tokens: int = 0,
    ) -> tuple[str, str] | None:
        async with self.session_factory() as session:
            run = await session.get(DesktopRun, run_id)
            if run:
                run.status = status
                run.error = error
                run.prompt_input_tokens = prompt_input_tokens
                run.prompt_cache_hit_tokens = prompt_cache_hit_tokens
                await session.commit()
                return run.kind, run.task_id
        return None

    async def _validate_attachments(
        self, session: AsyncSession, task_id: str, messages: list[dict[str, Any]]
    ) -> None:
        _, workspace = await self._get_task_entities(session, task_id)
        for message in messages:
            for item in message.get("files", []):
                path = item.get("path") if isinstance(item, dict) else item
                if not path or not self._resolve_workspace_path(workspace.path, path).exists():
                    raise HTTPException(422, f"附件引用失效: {path}")

    def _validate_model_window(self, model_name: str | None, estimate: int) -> None:
        model = self.app_config.get_model(model_name or self.app_config.resolve_default_model_name())
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
        self.app_config.get_model(model_name)
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
                            # 单个材料失败（如工作区目录已删除的遗留材料）不阻塞其他材料
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
        ui_state = dict(task.ui_state or {})
        ui_state.pop(_MAIN_RUNTIME_EQUIPMENT_KEY, None)
        recovery = await self._commitment_recovery_payload(session, task)
        compression_recovery = await compression_recovery_payload(
            session, task, self.checkpointer
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
            "ui_state": ui_state, "active_run": self._run_payload(active) if active else None,
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
        }

    async def _commitment_recovery_payload(
        self, session: AsyncSession, task: DesktopThread
    ) -> dict[str, Any] | None:
        """从承诺子图 checkpoint 投影桌面审批恢复状态。"""
        config = {
            "configurable": {
                "thread_id": commitment_subgraph_thread_id(task.thread_id),
            }
        }
        try:
            checkpoint = await self.checkpointer.aget_tuple(config)
        except Exception:
            logger.warning(
                "读取承诺子图 checkpoint 失败: thread_id=%s",
                task.thread_id,
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
        latest = await session.scalar(
            select(DesktopRun)
            .where(
                DesktopRun.task_id == task.task_id,
                DesktopRun.agent_id == f"main:{task.task_id}",
            )
            .order_by(DesktopRun.created_at.desc())
        )
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
        default_policy = ContextCurationPolicy().model_dump(mode="json")
        equipment = {
            **draft.equipment,
            "skills": DesktopService._normalize_skill_names(draft.equipment.get("skills")),
        }
        equipment.pop("skill_snapshots", None)
        return {
            "draft_id": draft.draft_id, "task_id": draft.task_id, "status": draft.status,
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
            "prompt_input_tokens": run.prompt_input_tokens,
            "prompt_cache_hit_tokens": run.prompt_cache_hit_tokens,
        }

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
            "size_bytes": path.stat().st_size if path.is_file() else 0,
        }

    async def store_uploaded_material(
        self, task_id: str, filename: str, data: bytes
    ) -> dict[str, Any]:
        """把上传/粘贴的文件写入工作区专用附件目录并登记为材料。

        原图按原分辨率保存（缩放只发生在送模注入时），因此这里只把住原图体积上限：
        超出即拒绝，不落盘、不留半成品文件。
        """
        if not data:
            raise HTTPException(422, "上传文件为空")
        name = Path(filename or "upload.bin").name
        try:
            guard_upload(name, data)
        except EmptyUpload as error:
            raise HTTPException(422, str(error)) from error
        except OversizedImage as error:
            raise HTTPException(413, str(error)) from error
        async with self.session_factory() as session:
            _, workspace = await self._get_task_entities(session, task_id)
        try:
            target = prepare_attachment_target(workspace.path, name)
        except OSError as error:
            raise HTTPException(500, f"无法创建材料附件目录: {error}") from error
        try:
            target.write_bytes(data)
        except OSError as error:
            raise HTTPException(500, f"无法写入材料附件: {error}") from error
        return await self.enroll_material(task_id, MaterialCreate(path=str(target)))

    async def read_material_content(self, material_id: str) -> tuple[str, bytes]:
        """读取材料原始字节；返回 (媒体类型, 字节) 供前端直接展示图片。"""
        async with self.session_factory() as session:
            material, workspace = await self._get_material_entities(session, material_id)
        path = resolve_material_path(workspace.path, material.relative_path)
        if not path.is_file():
            raise HTTPException(404, "材料文件不存在")
        media_type = (
            image_mime_from_name(material.relative_path) or "application/octet-stream"
        )
        return media_type, path.read_bytes()

    async def _resolve_must_view_materials(
        self, session: AsyncSession, task_id: str, workspace_path: str, material_ids: list[str]
    ) -> list[dict[str, str]]:
        """把本轮的必需图片标识解析为 [{material_id, relative_path}]。

        勾选的冲突在发起运行前一次性拦下（不存在、非图片、内容为空），
        使运行时只需面对「运行途中文件被抽走」这一种极端情况。
        """
        if not material_ids:
            return []
        unique = list(dict.fromkeys(material_ids))
        rows = (
            await session.scalars(
                select(DesktopMaterial).where(
                    DesktopMaterial.task_id == task_id,
                    DesktopMaterial.material_id.in_(unique),
                )
            )
        ).all()
        by_id = {item.material_id: item for item in rows}
        resolved: list[dict[str, str]] = []
        for material_id in unique:
            material = by_id.get(material_id)
            if material is None:
                raise HTTPException(422, f"「本轮必须看」的材料不存在: {material_id}")
            if not is_image_name(material.relative_path):
                raise HTTPException(
                    422, f"「本轮必须看」只适用于图片材料: {material.relative_path}"
                )
            path = resolve_material_path(workspace_path, material.relative_path)
            if not path.is_file() or path.stat().st_size == 0:
                raise HTTPException(
                    422, f"「本轮必须看」的图片内容为空或文件不存在: {material.relative_path}"
                )
            resolved.append(
                {"material_id": material.material_id, "relative_path": material.relative_path}
            )
        return resolved

    @staticmethod
    def _version_payload(version: MaterialVersion) -> dict[str, Any]:
        return {
            "version_id": version.version_id, "commit_id": version.commit_id,
            "object_id": version.object_id, "digest": version.digest, "source": version.source,
            "created_at": version.created_at.isoformat() if version.created_at else None,
        }
