"""
本文件对外提供 DesktopService，集中实现桌面工作区、草稿、小兵运行与材料版本业务。

输入为已初始化的 PostgreSQL session factory、LangGraph checkpointer/store、StreamBridge、
RunManager 和 AppConfig；输出为供 routes.py 调用的异步业务方法以及 PreparedRun（统一
编排入口 start_run 的输入 + agent_factory 闭包）。
具体工作流为：登记真实宿主机工作区与线程，复制已提交 checkpoint 形成冻结草稿，
准备无沙箱工作区 Agent 的装配参数（经统一执行链路 worker.run_agent 执行，
独立 checkpoint namespace 隔离小兵），并用 Git 隐藏引用保护不可遗失材料。
示例：`service = DesktopService(...); await service.open_draft(task_id)`。
"""

from __future__ import annotations

import asyncio
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
from langchain_core.tools import BaseTool, tool
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.models import (
    DesktopMaterial,
    DesktopRun,
    DesktopThread,
    DesktopWorkspace,
    DraftUpdate,
    MaterialCreate,
    MaterialUpdate,
    MaterialVersion,
    PatrolAgent,
    PatrolDraft,
)
from backend.app.desktop.skills import build_task_skill_catalog, resolve_task_skills
from backend.app.gateway.routers.thread_runs import RunCreateRequest
from focus.agents.commitment.middleware import commitment_subgraph_thread_id
from focus.agents.commitment.workflow import _human_payload
from focus.agents.lead import make_lead_agent
from focus.config.app_config import AppConfig
from focus.runtime.runs.events import (
    serialize_message,
    validate_messages,
)
from focus.runtime.runs.manager import RunManager, RunRecord
from focus.runtime.stream_bridge.base import StreamBridge
from focus.tools.builtins.workspace_tools import select_workspace_tools

logger = logging.getLogger(__name__)

_MAIN_SYSTEM_PROMPT ="""你是 Focus 的本地主 Agent。当前工作目录是真实宿主机工作区。
使用已提供的工具完成用户任务；严格服从平台授予的工具权限，不要把当前环境描述为沙箱。"""
_MAIN_RUNTIME_EQUIPMENT_KEY = "_main_run_equipment"
_TERMINAL_STATUSES = frozenset({"success", "error", "interrupted"})


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
    raw = system_prompt + final_message + json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    cjk = sum(1 for char in raw if "\u3400" <= char <= "\u9fff")
    other = sum(1 for char in raw if not char.isspace() and not ("\u3400" <= char <= "\u9fff"))
    return cjk + (other + 3) // 4 + 12 * (len(messages) + 2)


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
        # 仅持有 DB 终态同步任务（运行注册表/取消由 RunManager 负责）
        self._sync_tasks: set[asyncio.Task] = set()
        self._watcher: asyncio.Task | None = None

    async def start(self) -> None:
        async with self.session_factory() as session:
            await session.execute(
                update(DesktopRun)
                .where(DesktopRun.status.in_(["pending", "running"]))
                .values(status="interrupted", error="桌面后端重启，原运行无法继续")
            )
            await session.commit()
        self._watcher = asyncio.create_task(self._watch_materials())

    async def close(self) -> None:
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
                {"name": model.name, "display_name": model.display_name, "context_window": model.context_window}
                for model in self.app_config.models
            ],
            "tools": ["read_file", "list_files", "write_file", "powershell"],
            "skills": skills,
            "permissions": ["read", "write", "host_command"],
        }

    async def create_workspace(self, path: str, display_name: str | None = None) -> dict[str, Any]:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise HTTPException(422, "工作区必须是已存在的本地文件夹")
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
        payload["messages"] = await self.get_checkpoint_messages(task.thread_id, "")
        return payload

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

    async def open_draft(self, task_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            task, _ = await self._get_task_entities(session, task_id)
            existing = await session.scalar(
                select(PatrolDraft)
                .where(PatrolDraft.task_id == task_id, PatrolDraft.status == "editing")
                .order_by(PatrolDraft.updated_at.desc())
            )
            if existing:
                return self._draft_payload(existing)
            config = {"configurable": {"thread_id": task.thread_id, "checkpoint_ns": ""}}
            checkpoint = await self.checkpointer.aget_tuple(config)
            messages: list[dict[str, Any]] = []
            checkpoint_id = None
            if checkpoint:
                checkpoint_id = checkpoint.config.get("configurable", {}).get("checkpoint_id")
                values = checkpoint.checkpoint.get("channel_values", {})
                messages = [serialize_message(message) for message in values.get("messages", [])]
            default_model = self.app_config.models[0].name if self.app_config.models else None
            equipment = {
                "model_name": default_model,
                "tools": "auto",
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
        validate_messages(body.history_messages)
        async with self.session_factory() as session:
            draft = await session.get(PatrolDraft, draft_id)
            if not draft or draft.status != "editing":
                raise HTTPException(404, "可编辑草稿不存在")
            _, workspace = await self._get_task_entities(session, draft.task_id)
            equipment = self._normalize_equipment(body.equipment)
            snapshots, _ = resolve_task_skills(
                build_task_skill_catalog(workspace.path), equipment["skills"]
            )
            estimate = estimate_tokens(
                prompt_with_skills(body.system_prompt, snapshots),
                body.history_messages,
                body.final_human_message,
            )
            draft.system_prompt = body.system_prompt
            draft.history_messages = body.history_messages
            draft.final_human_message = body.final_human_message
            draft.equipment = equipment
            draft.token_estimate = estimate
            await session.commit()
            return self._draft_payload(draft)

    async def deploy(self, draft_id: str, deployment_id: str) -> PreparedRun:
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
            draft.system_prompt, agent.equipment, agent.checkpoint_ns, False,
        )

    async def start_main_run(
        self, task_id: str, message: str, model_name: str | None,
        permissions: list[str], skills: list[str],
    ) -> PreparedRun:
        async with self.session_factory() as session:
            task_row, workspace = await self._get_task_entities(session, task_id)
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
            snapshots = self._freeze_skills(workspace.path, skills)
            equipment = {
                "model_name": model_name,
                "tools": "auto",
                "skills": list(dict.fromkeys(skills)),
                "skill_snapshots": snapshots,
                "permissions": permissions,
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
            await session.commit()
            thread_id = task_row.thread_id
            workspace_id = workspace.workspace_id
            workspace_path = workspace.path
        return await self._prepare(
            run, thread_id, workspace_id, workspace_path, run.input_messages,
            _MAIN_SYSTEM_PROMPT, equipment, "", True,
        )

    async def resume_run(self, thread_id: str, resume: dict[str, Any]) -> PreparedRun:
        """承诺层人工确认恢复：以相同 thread_id resume 父图，resume 载荷经 start_run 翻译为 Command(resume=...)。

        输入:
            thread_id: str — 桌面任务登记的唯一 thread 标识
            resume: dict — 人工决定 {decision: approve|revise, feedback?, replacement?}

        输出:
            PreparedRun — 携带 RunCreateRequest(resume=...) 与主 Agent 装配闭包
        """
        async with self.session_factory() as session:
            task = await session.scalar(
                select(DesktopThread).where(DesktopThread.thread_id == thread_id)
            )
            if not task:
                raise HTTPException(404, "任务不存在")
            recovery = await self._commitment_recovery_payload(session, task)
            if recovery is None:
                raise HTTPException(409, "无可恢复的承诺流程")
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
            equipment = dict(
                (task.ui_state or {}).get(_MAIN_RUNTIME_EQUIPMENT_KEY) or {}
            )
            if not equipment:
                # 兼容修复前已进入 interrupt 的任务：尽量恢复 UI 中仍可获得的技能，
                # 其余字段沿用旧行为的默认值。
                skills = self._normalize_skill_names((task.ui_state or {}).get("skills"))
                equipment = {
                    "model_name": None,
                    "tools": "auto",
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
            task_id = task.task_id
            workspace_id = workspace.workspace_id
            workspace_path = workspace.path
        material_context, uploads_tag = await self._material_context(task_id)
        factory = self._build_agent_factory(
            task_id, workspace_path, equipment, _MAIN_SYSTEM_PROMPT,
            material_context, True,
        )
        body = RunCreateRequest(
            input=None,
            resume=resume,
            context={
                "model_name": equipment.get("model_name"),
                "workspace_id": workspace_id,
                "agent_id": run.agent_id,
                "permissions": equipment.get("permissions") or ["read"],
                "skills": equipment.get("skills") or [],
                "workspace": workspace_path,
                "uploads": uploads_tag,
                "checkpoint_ns": "",
                "run_id": run.run_id,
            },
            stream_mode=["messages-tuple", "values"],
        )
        return PreparedRun(
            body=body,
            thread_id=thread_id,
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
            agent.system_prompt, agent.equipment, agent.checkpoint_ns, False,
        )

    async def continue_agent(self, agent_id: str, message: str) -> PreparedRun:
        async with self.session_factory() as session:
            agent = await session.get(PatrolAgent, agent_id)
            if not agent:
                raise HTTPException(404, "小兵不存在")
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
            agent.system_prompt, agent.equipment, agent.checkpoint_ns, False,
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
                    "permissions": agent.equipment.get("permissions", ["read"]),
                    "created_at": agent.created_at.isoformat() if agent.created_at else None,
                    "latest_run": self._run_payload(latest) if latest else None,
                })
            return result

    async def agent_history(self, agent_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            agent = await session.get(PatrolAgent, agent_id)
            if not agent:
                raise HTTPException(404, "小兵不存在")
            task = await session.get(DesktopThread, agent.task_id)
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
        checkpoint_ns: str, with_patrol_readers: bool,
    ) -> PreparedRun:
        """组装统一编排入口的输入：RunCreateRequest（input/context/stream_mode）+ agent_factory 闭包。

        工作流:
            (1) 查询任务材料策略，拼入运行 prompt 基础
            (2) 构建 agent_factory 闭包（工作区内置工具按权限过滤 + 小兵读取工具 + 技能快照）
            (3) context 携带 workspace/workspace_id/agent_id/permissions/skills/checkpoint_ns，
                由 services.start_run 透传给 worker 与工具（ToolRuntime）
        """
        material_context, uploads_tag = await self._material_context(run.task_id)
        factory = self._build_agent_factory(
            run.task_id, workspace_path, equipment, base_prompt,
            material_context, with_patrol_readers,
        )
        body = RunCreateRequest(
            input={"messages": messages},
            context={
                "model_name": equipment.get("model_name") or run.model_name,
                "workspace_id": workspace_id,
                "agent_id": run.agent_id,
                "permissions": equipment.get("permissions") or ["read"],
                "skills": equipment.get("skills") or [],
                "workspace": workspace_path,
                "uploads": uploads_tag,
                "checkpoint_ns": checkpoint_ns,
                "run_id": run.run_id,
            },
            stream_mode=["messages-tuple", "values"],
        )
        return PreparedRun(body=body, thread_id=thread_id, agent_factory=factory, payload=self._run_payload(run))

    def _build_agent_factory(
        self, task_id: str, workspace_path: str, equipment: dict[str, Any],
        base_prompt: str, material_context: str, with_patrol_readers: bool,
    ) -> Callable[[], Awaitable[CompiledStateGraph]]:
        """构建 agent_factory 闭包：工作区内置工具（权限过滤）+ 小兵读取工具 + prompt 注入。

        输入:
            task_id: str — 任务 ID（小兵读取工具按任务过滤）
            workspace_path: str — 真实工作区路径（经 ToolRuntime context 注入工具）
            equipment: dict — 模型/权限/技能快照（skill_snapshots）
            base_prompt: str — 主 prompt（主 Agent 固定模板 / 小兵草稿 system_prompt）
            material_context: str — 材料策略文本（可为空）
            with_patrol_readers: bool — 主 Agent 是否携带小兵读取工具

        输出:
            Callable — async 闭包，await 后返回 CompiledStateGraph
        """
        permissions = equipment.get("permissions") or ["read"]
        snapshots = equipment.get("skill_snapshots") or []
        model_name = equipment.get("model_name")

        async def factory() -> CompiledStateGraph:
            tools = select_workspace_tools(permissions)
            if with_patrol_readers:
                tools = [*tools, *self._build_patrol_reader_tools(task_id)]
            prompt = prompt_with_skills(base_prompt, snapshots)
            if material_context:
                prompt = f"{prompt}\n\n<focus_material_policies>\n{material_context}\n</focus_material_policies>"
            # 承诺层装配：主 Agent 经共享 builder（按 commitment.enabled 条件装配，
            # skill_names 取任务技能 catalog 全量用于触发剥离）；小兵不装配承诺层。
            if with_patrol_readers:
                task_skill_names = frozenset(build_task_skill_catalog(workspace_path))
                middlewares = None
            else:
                task_skill_names = None
                middlewares = []
            return await make_lead_agent(
                model_name=model_name,
                tools=tools,
                system_prompt=prompt,
                middlewares=middlewares,
                app_config=self.app_config,
                middleware_skill_names=task_skill_names,
            )

        return factory

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
                await self._set_run_status(record.run_id, record.status.value, record.error)
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

    async def _set_run_status(self, run_id: str, status: str, error: str | None = None) -> None:
        async with self.session_factory() as session:
            run = await session.get(DesktopRun, run_id)
            if run:
                run.status = status
                run.error = error
                await session.commit()

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
        model = self.app_config.get_model(model_name) if model_name else self.app_config.models[0]
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
        model_name = equipment.get("model_name") or self.app_config.models[0].name
        self.app_config.get_model(model_name)
        raw_permissions = equipment.get("permissions")
        permissions = list(dict.fromkeys(["read"] if raw_permissions is None else raw_permissions))
        invalid = set(permissions) - {"read", "write", "host_command"}
        if invalid:
            raise HTTPException(422, f"未知权限: {', '.join(sorted(invalid))}")
        selected_tools = equipment.get("tools", "auto")
        if selected_tools != "auto" and not isinstance(selected_tools, list):
            raise HTTPException(422, "tools 必须是 auto 或工具名称数组")
        unknown_tools = set(selected_tools if isinstance(selected_tools, list) else ()) - {
            "read_file", "list_files", "write_file", "powershell"
        }
        if unknown_tools:
            raise HTTPException(422, f"未知工具: {', '.join(sorted(unknown_tools))}")
        return {
            "model_name": model_name,
            "tools": selected_tools,
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
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            # 单个材料失败（如工作区目录已删除的遗留材料）不阻塞其他材料
                            logger.warning("材料监测失败，跳过该材料: material_id=%s", material.material_id, exc_info=True)
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
        return {
            "task_id": task.task_id, "workspace_id": task.workspace_id, "workspace_path": workspace.path,
            "workspace_name": workspace.display_name, "thread_id": task.thread_id, "title": task.title,
            "ui_state": ui_state, "active_run": self._run_payload(active) if active else None,
            "pending_commitment_review": (
                recovery["review"] if recovery and recovery["status"] == "resumable" else None
            ),
            "commitment_recovery": recovery,
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

    @staticmethod
    def _draft_payload(draft: PatrolDraft) -> dict[str, Any]:
        equipment = {
            **draft.equipment,
            "skills": DesktopService._normalize_skill_names(draft.equipment.get("skills")),
        }
        equipment.pop("skill_snapshots", None)
        return {
            "draft_id": draft.draft_id, "task_id": draft.task_id, "status": draft.status,
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
        }

    @staticmethod
    def _material_payload(material: DesktopMaterial, workspace_path: str) -> dict[str, Any]:
        return {
            "material_id": material.material_id, "task_id": material.task_id,
            "path": str(Path(workspace_path, *Path(material.relative_path).parts)),
            "relative_path": material.relative_path, "reading_mode": material.reading_mode,
            "instruction_mode": material.instruction_mode, "retention": material.retention,
            "digest": material.digest, "git_ref": material.git_ref,
            "needs_confirmation": material.needs_confirmation,
        }

    @staticmethod
    def _version_payload(version: MaterialVersion) -> dict[str, Any]:
        return {
            "version_id": version.version_id, "commit_id": version.commit_id,
            "object_id": version.object_id, "digest": version.digest, "source": version.source,
            "created_at": version.created_at.isoformat() if version.created_at else None,
        }
