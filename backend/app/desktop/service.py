"""
本文件对外提供 DesktopService，集中实现桌面工作区、草稿、小兵运行与材料版本业务。

输入为已初始化的 PostgreSQL session factory、LangGraph checkpointer/store、StreamBridge
和 AppConfig；输出为供 routes.py 调用的异步业务方法以及按 run_id 发布的桌面事件。
具体工作流为：登记真实宿主机工作区与线程，复制已提交 checkpoint 形成冻结草稿，
按工具级安全策略组装无沙箱 Agent，在独立 checkpoint namespace 中执行，并用 Git
隐藏引用保护不可遗失材料。示例：`service = DesktopService(...); await service.open_draft(task_id)`。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
from typing import Any
import uuid

from fastapi import HTTPException
from langchain.agents import create_agent
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
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
from focus.config.app_config import AppConfig
from focus.models import create_chat_model
from focus.runtime.stream_bridge.base import StreamBridge
from focus.runtime.stream_bridge.schemas import StreamEvent

logger = logging.getLogger(__name__)

_MAIN_SYSTEM_PROMPT ="""你是 Focus 的本地主 Agent。当前工作目录是真实宿主机工作区。
使用已提供的工具完成用户任务；严格服从平台授予的工具权限，不要把当前环境描述为沙箱。"""
_TERMINAL_STATUSES = frozenset({"success", "error", "interrupted"})


class NamespacedCheckpointer(BaseCheckpointSaver):
    """Keep a root LangGraph in one explicit PostgreSQL checkpoint namespace."""

    def __init__(self, backend: BaseCheckpointSaver, namespace: str) -> None:
        super().__init__(serde=backend.serde)
        self.backend = backend
        self.namespace = namespace

    @property
    def config_specs(self):
        return self.backend.config_specs

    def _stored(self, config):
        result = {**config, "configurable": {**config.get("configurable", {})}}
        result["configurable"]["checkpoint_ns"] = self.namespace
        return result

    @staticmethod
    def _root(config):
        if config is None:
            return None
        result = {**config, "configurable": {**config.get("configurable", {})}}
        result["configurable"]["checkpoint_ns"] = ""
        return result

    def _root_tuple(self, value):
        if value is None:
            return None
        return CheckpointTuple(
            self._root(value.config), value.checkpoint, value.metadata,
            self._root(value.parent_config), value.pending_writes,
        )

    async def aget_tuple(self, config):
        return self._root_tuple(await self.backend.aget_tuple(self._stored(config)))

    async def alist(self, config, *, filter=None, before=None, limit=None):
        stored = self._stored(config) if config is not None else None
        stored_before = self._stored(before) if before is not None else None
        async for value in self.backend.alist(
            stored, filter=filter, before=stored_before, limit=limit
        ):
            yield self._root_tuple(value)

    async def aput(self, config, checkpoint, metadata, new_versions):
        saved = await self.backend.aput(
            self._stored(config), checkpoint, metadata, new_versions
        )
        return self._root(saved)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        await self.backend.aput_writes(
            self._stored(config), writes, task_id, task_path
        )

    async def adelete_thread(self, thread_id):
        await self.backend.adelete_thread(thread_id)

    async def adelete_for_runs(self, run_ids):
        await self.backend.adelete_for_runs(run_ids)

    async def acopy_thread(self, source_thread_id, target_thread_id):
        await self.backend.acopy_thread(source_thread_id, target_thread_id)

    async def aprune(self, thread_ids, *, strategy="keep_latest"):
        await self.backend.aprune(thread_ids, strategy=strategy)

    async def aget_delta_channel_history(self, *, config, channels):
        return await self.backend.aget_delta_channel_history(
            config=self._stored(config), channels=channels
        )

    def get_next_version(self, current, channel):
        return self.backend.get_next_version(current, channel)

    def with_allowlist(self, extra_allowlist):
        return NamespacedCheckpointer(
            self.backend.with_allowlist(extra_allowlist), self.namespace
        )


def new_id() -> str:
    return uuid.uuid4().hex


def estimate_tokens(system_prompt: str, messages: list[dict[str, Any]], final_message: str) -> int:
    raw = system_prompt + final_message + json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    cjk = sum(1 for char in raw if "\u3400" <= char <= "\u9fff")
    other = sum(1 for char in raw if not char.isspace() and not ("\u3400" <= char <= "\u9fff"))
    return cjk + (other + 3) // 4 + 12 * (len(messages) + 2)


def stream_text(content: Any) -> str:
    """Extract visible text from a LangChain message chunk."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def serialize_message(message: BaseMessage) -> dict[str, Any]:
    if isinstance(message, HumanMessage):
        role = "human"
    elif isinstance(message, AIMessage):
        role = "ai"
    elif isinstance(message, SystemMessage):
        role = "system"
    elif isinstance(message, ToolMessage):
        role = "tool"
    else:
        role = message.type
    result: dict[str, Any] = {"role": role, "content": message.content}
    if message.id:
        result["id"] = message.id
    if isinstance(message, AIMessage) and message.tool_calls:
        result["tool_calls"] = message.tool_calls
        result["locked"] = True
    if isinstance(message, ToolMessage):
        result["tool_call_id"] = message.tool_call_id
        result["name"] = message.name
        result["locked"] = True
    files = message.additional_kwargs.get("files") if message.additional_kwargs else None
    if files:
        result["files"] = files
    return result


def validate_messages(messages: list[dict[str, Any]]) -> None:
    pending_calls: dict[str, int] = {}
    resolved_calls: set[str] = set()
    for index, message in enumerate(messages):
        role = message.get("role")
        content = message.get("content", "")
        unresolved = set(pending_calls) - resolved_calls
        if unresolved and role != "tool":
            raise ValueError(
                f"消息 {index + 1} 之前必须紧跟完成工具结果: {', '.join(sorted(unresolved))}"
            )
        if role not in {"human", "user", "ai", "assistant", "system", "tool"}:
            raise ValueError(f"消息 {index + 1} 的角色无效")
        if not isinstance(content, (str, list)):
            raise ValueError(f"消息 {index + 1} 的 content 必须是文本或内容块")
        tool_calls = message.get("tool_calls", [])
        if tool_calls:
            if role not in {"ai", "assistant"}:
                raise ValueError(f"消息 {index + 1} 的 tool_calls 只能属于 AIMessage")
            for call in tool_calls:
                call_id = call.get("id") if isinstance(call, dict) else None
                if not call_id or call_id in pending_calls:
                    raise ValueError(f"消息 {index + 1} 包含无效或重复 tool call id")
                pending_calls[call_id] = index
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not call_id or call_id not in pending_calls or call_id in resolved_calls:
                raise ValueError(f"消息 {index + 1} 的 ToolMessage 没有合法调用方")
            resolved_calls.add(call_id)
    unresolved = set(pending_calls) - resolved_calls
    if unresolved:
        raise ValueError(f"工具调用缺少结果: {', '.join(sorted(unresolved))}")


def deserialize_messages(messages: list[dict[str, Any]]) -> list[BaseMessage]:
    validate_messages(messages)
    result: list[BaseMessage] = []
    for message in messages:
        role = message.get("role")
        kwargs: dict[str, Any] = {}
        if message.get("id"):
            kwargs["id"] = message["id"]
        if message.get("files"):
            kwargs["additional_kwargs"] = {"files": message["files"]}
        if role in {"human", "user"}:
            result.append(HumanMessage(content=message.get("content", ""), **kwargs))
        elif role in {"ai", "assistant"}:
            if message.get("tool_calls"):
                kwargs["tool_calls"] = message["tool_calls"]
            result.append(AIMessage(content=message.get("content", ""), **kwargs))
        elif role == "system":
            result.append(SystemMessage(content=message.get("content", ""), **kwargs))
        else:
            result.append(
                ToolMessage(
                    content=message.get("content", ""),
                    tool_call_id=message["tool_call_id"],
                    name=message.get("name"),
                    **kwargs,
                )
            )
    return result


class DesktopService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        checkpointer: Any,
        store: Any,
        bridge: StreamBridge,
        app_config: AppConfig,
    ) -> None:
        self.session_factory = session_factory
        self.checkpointer = checkpointer
        self.store = store
        self.bridge = bridge
        self.app_config = app_config
        self._tasks: dict[str, asyncio.Task] = {}
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
        background = list(self._tasks.values())
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

    async def save_ui_state(self, task_id: str, ui_state: dict[str, Any]) -> None:
        async with self.session_factory() as session:
            task = await session.get(DesktopThread, task_id)
            if not task:
                raise HTTPException(404, "任务不存在")
            task.ui_state = ui_state
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
                "skills": "auto",
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
        estimate = estimate_tokens(body.system_prompt, body.history_messages, body.final_human_message)
        async with self.session_factory() as session:
            draft = await session.get(PatrolDraft, draft_id)
            if not draft or draft.status != "editing":
                raise HTTPException(404, "可编辑草稿不存在")
            draft.system_prompt = body.system_prompt
            draft.history_messages = body.history_messages
            draft.final_human_message = body.final_human_message
            draft.equipment = self._normalize_equipment(body.equipment)
            draft.token_estimate = estimate
            await session.commit()
            return self._draft_payload(draft)

    async def deploy(self, draft_id: str, deployment_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            existing = await session.scalar(select(DesktopRun).where(DesktopRun.deployment_id == deployment_id))
            if existing:
                return self._run_payload(existing)
            draft = await session.get(PatrolDraft, draft_id)
            if not draft or draft.status != "editing":
                raise HTTPException(404, "可投放草稿不存在")
            if not draft.final_human_message.strip():
                raise HTTPException(422, "最后一条 HumanMessage 不能为空")
            validate_messages(draft.history_messages)
            self._validate_model_window(draft.equipment.get("model_name"), draft.token_estimate)
            await self._validate_attachments(session, draft.task_id, draft.history_messages)
            agent_id = new_id()
            frozen = [*draft.history_messages, {"role": "human", "content": draft.final_human_message}]
            agent = PatrolAgent(
                agent_id=agent_id,
                task_id=draft.task_id,
                checkpoint_ns=f"patrol:{agent_id}",
                system_prompt=draft.system_prompt,
                frozen_messages=frozen,
                equipment=draft.equipment,
                source_checkpoint_id=draft.source_checkpoint_id,
            )
            run = DesktopRun(
                run_id=new_id(), task_id=draft.task_id, agent_id=agent_id, deployment_id=deployment_id,
                kind="patrol", status="pending", input_messages=frozen,
                model_name=draft.equipment.get("model_name"),
            )
            draft.status = "deployed"
            session.add_all([agent, run])
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                winner = await session.scalar(select(DesktopRun).where(DesktopRun.deployment_id == deployment_id))
                if winner:
                    return self._run_payload(winner)
                raise
        await self._schedule_run(run.run_id, agent.system_prompt, agent.checkpoint_ns, frozen, agent.equipment)
        return self._run_payload(run)

    async def start_main_run(
        self, task_id: str, message: str, model_name: str | None, permissions: list[str]
    ) -> dict[str, Any]:
        async with self.session_factory() as session:
            await self._get_task_entities(session, task_id)
            run = DesktopRun(
                run_id=new_id(), task_id=task_id, agent_id=f"main:{task_id}", kind="main", status="pending",
                input_messages=[{"role": "human", "content": message}], model_name=model_name,
            )
            session.add(run)
            await session.commit()
        equipment = {"model_name": model_name, "tools": "auto", "skills": "auto", "permissions": permissions}
        await self._schedule_run(run.run_id, _MAIN_SYSTEM_PROMPT, "", run.input_messages, equipment)
        return self._run_payload(run)

    async def retry_agent(self, agent_id: str) -> dict[str, Any]:
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
        await self._schedule_run(run.run_id, agent.system_prompt, agent.checkpoint_ns, agent.frozen_messages, agent.equipment)
        return self._run_payload(run)

    async def continue_agent(self, agent_id: str, message: str) -> dict[str, Any]:
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
        await self._schedule_run(run.run_id, agent.system_prompt, agent.checkpoint_ns, input_messages, agent.equipment)
        return self._run_payload(run)

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
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

    async def _schedule_run(
        self, run_id: str, system_prompt: str, checkpoint_ns: str,
        messages: list[dict[str, Any]], equipment: dict[str, Any],
    ) -> None:
        task = asyncio.create_task(self._run_agent(run_id, system_prompt, checkpoint_ns, messages, equipment))
        self._tasks[run_id] = task

    async def _run_agent(
        self, run_id: str, system_prompt: str, checkpoint_ns: str,
        messages: list[dict[str, Any]], equipment: dict[str, Any],
    ) -> None:
        run: DesktopRun | None = None
        task: DesktopThread | None = None
        try:
            async with self.session_factory() as session:
                run = await session.get(DesktopRun, run_id)
                if not run:
                    return
                run.status = "running"
                task, workspace = await self._get_task_entities(session, run.task_id)
                await session.commit()
            envelope = self._event_envelope(run, task, "metadata", {"status": "running"})
            self.bridge.publish(run_id, StreamEvent(id="", event="metadata", data=envelope))
            model_name = equipment.get("model_name") or run.model_name
            model = create_chat_model(name=model_name, app_config=self.app_config)
            tools = await self._build_tools(task, workspace, equipment, run.kind == "main")
            material_context = await self._material_context(task.task_id)
            runtime_prompt = system_prompt
            if material_context:
                runtime_prompt += f"\n\n<focus_material_policies>\n{material_context}\n</focus_material_policies>"
            run_checkpointer = NamespacedCheckpointer(self.checkpointer, checkpoint_ns)
            graph = create_agent(
                model=model, tools=tools, system_prompt=runtime_prompt,
                checkpointer=run_checkpointer, store=self.store,
            )
            config = {
                "recursion_limit": 50,
                "configurable": {
                    "thread_id": task.thread_id,
                    "run_id": run_id,
                },
            }
            async for mode, chunk in graph.astream(
                {"messages": deserialize_messages(messages)},
                config=config,
                stream_mode=["messages", "values"],
            ):
                if mode == "messages":
                    message, metadata = chunk
                    content = stream_text(message.content)
                    if not content:
                        continue
                    payload = {
                        "content": content,
                        "message_id": message.id,
                        "node": metadata.get("langgraph_node"),
                    }
                    event_name = "tokens"
                else:
                    payload = self._serialize_value(chunk)
                    event_name = "events"
                envelope = self._event_envelope(run, task, event_name, payload)
                self.bridge.publish(run_id, StreamEvent(id="", event=event_name, data=envelope))
            await self._set_run_status(run_id, "success")
            envelope = self._event_envelope(run, task, "status", {"status": "success"})
            self.bridge.publish(run_id, StreamEvent(id="", event="status", data=envelope))
        except asyncio.CancelledError:
            await self._set_run_status(run_id, "interrupted")
            if run and task:
                envelope = self._event_envelope(run, task, "status", {"status": "interrupted"})
                self.bridge.publish(run_id, StreamEvent(id="", event="status", data=envelope))
        except Exception as exc:
            await self._set_run_status(run_id, "error", str(exc))
            if run and task:
                envelope = self._event_envelope(run, task, "error", {"error": str(exc)})
                self.bridge.publish(run_id, StreamEvent(id="", event="error", data=envelope))
        finally:
            self.bridge.publish_end(run_id)
            self.bridge.cleanup(run_id, delay=300)
            self._tasks.pop(run_id, None)

    async def _build_tools(
        self, task: DesktopThread, workspace: DesktopWorkspace,
        equipment: dict[str, Any], include_patrol_readers: bool,
    ) -> list[BaseTool]:
        root = Path(workspace.path)
        raw_permissions = equipment.get("permissions")
        permissions = frozenset(["read"] if raw_permissions is None else raw_permissions)

        @tool
        def read_file(path: str) -> str:
            """读取当前真实工作区内文件；path 可以是绝对路径或相对工作区路径。"""
            if "read" not in permissions:
                raise PermissionError("当前运行未授权 read")
            target = self._resolve_workspace_path(root, path)
            if not target.is_file():
                raise FileNotFoundError(str(target))
            suffix = target.suffix.lower()
            if suffix in {".pdf", ".docx", ".doc"}:
                from focus.sandbox.readers import _read_doc, _read_docx, _read_pdf
                if suffix == ".pdf":
                    return _read_pdf(str(target))
                if suffix == ".docx":
                    return _read_docx(str(target))
                return _read_doc(str(target))
            return target.read_text(encoding="utf-8", errors="replace")

        @tool
        def list_files(path: str = ".") -> str:
            """列出当前真实工作区内目录；path 可以是绝对路径或相对工作区路径。"""
            if "read" not in permissions:
                raise PermissionError("当前运行未授权 read")
            target = self._resolve_workspace_path(root, path)
            if not target.is_dir():
                raise NotADirectoryError(str(target))
            return "\n".join(str(item) for item in sorted(target.iterdir()))

        @tool
        def write_file(path: str, content: str) -> str:
            """在当前真实工作区写入 UTF-8 文本；只有用户授权 write 时才会装备。"""
            if "write" not in permissions:
                raise PermissionError("当前运行未授权 write")
            target = self._resolve_workspace_path(root, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"已写入真实宿主机路径: {target}"

        @tool
        def powershell(command: str) -> str:
            """在真实工作区执行 PowerShell；只有用户授权 host_command 时才会装备。"""
            if "host_command" not in permissions:
                raise PermissionError("当前运行未授权 host_command")
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", command], cwd=root,
                capture_output=True, text=True, timeout=120, shell=False,
            )
            return (result.stdout + result.stderr)[-30000:]

        available: dict[str, BaseTool] = {}
        if "read" in permissions:
            available.update({"read_file": read_file, "list_files": list_files})
        if "write" in permissions:
            available["write_file"] = write_file
        if "host_command" in permissions:
            available["powershell"] = powershell
        selected = equipment.get("tools", "auto")
        tools = list(available.values()) if selected == "auto" else [available[name] for name in selected if name in available]
        if include_patrol_readers:
            tools.extend(self._build_patrol_reader_tools(task.task_id))
        return tools

    async def _material_context(self, task_id: str) -> str:
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
        for material in materials:
            reading = "优先完整阅读" if material.reading_mode == "full" else "优先粗略阅读，需要时仍可完整读取"
            instruction = "严格遵守" if material.instruction_mode == "strict" else "仅供参考"
            lines.append(
                f"- {Path(workspace.path, *Path(material.relative_path).parts)} | {reading} | {instruction}"
            )
        return "\n".join(lines)

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
            "skills": equipment.get("skills", "auto"),
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
    def _serialize_value(value: Any) -> Any:
        if isinstance(value, BaseMessage):
            return serialize_message(value)
        if isinstance(value, dict):
            return {key: DesktopService._serialize_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [DesktopService._serialize_value(item) for item in value]
        return value

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
        return {
            "task_id": task.task_id, "workspace_id": task.workspace_id, "workspace_path": workspace.path,
            "workspace_name": workspace.display_name, "thread_id": task.thread_id, "title": task.title,
            "ui_state": task.ui_state or {}, "active_run": self._run_payload(active) if active else None,
        }

    @staticmethod
    def _draft_payload(draft: PatrolDraft) -> dict[str, Any]:
        return {
            "draft_id": draft.draft_id, "task_id": draft.task_id, "status": draft.status,
            "system_prompt": draft.system_prompt, "history_messages": draft.history_messages,
            "final_human_message": draft.final_human_message, "equipment": draft.equipment,
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

    @staticmethod
    def _event_envelope(
        run: DesktopRun, task: DesktopThread, event: str, data: Any
    ) -> dict[str, Any]:
        return {
            "workspace_id": task.workspace_id, "thread_id": task.thread_id,
            "agent_id": run.agent_id, "run_id": run.run_id, "event": event, "data": data,
        }
