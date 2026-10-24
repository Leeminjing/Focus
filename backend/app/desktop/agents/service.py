"""CollaborationService 的业务实现与显式依赖端口。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol

from fastapi import HTTPException
from focus.security.context import security_context_of
from focus.security.effects import (
    DELEGATED_EXECUTION_EFFECT,
    NO_LOCAL_EFFECT,
    declare_all_effects,
    declare_effect,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool
from sqlalchemy import select

from backend.app.desktop.agents.contracts import WaitSeconds, validate_wait_seconds
from backend.app.desktop.capabilities.equipment import effective_equipment
from backend.app.desktop.conversations.checkpoint_recovery import select_checkpoint_base
from backend.app.desktop.persistence.ids import new_id
from backend.app.desktop.persistence.models import (
    AgentBoardTask,
    AgentMessage,
    DesktopRun,
    DesktopThread,
    DesktopWorkspace,
    SwarmAgent,
)
from backend.app.desktop.runs.identity import _identity_unregistered
from backend.app.desktop.runs.orchestration import RunAdmissionService
from backend.app.desktop.runs.policy import _RUN_DISPATCH_EQUIPMENT_KEY

logger = logging.getLogger(__name__)


class CollaborationServicePorts(Protocol):
    _TEAMMATE_PROMPT: Any
    _WORKER_PROMPT: Any

    def _admission_service(self) -> RunAdmissionService: ...

    def _require_run_admission(self) -> None: ...

    async def _task_thread_id(self, task_id: str) -> str: ...

    agent_collab: Any
    checkpointer: Any

    async def get_checkpoint_messages(
        self, thread_id: str, checkpoint_ns: str
    ) -> list[dict[str, Any]]: ...

    def notify_run_dispatch(self) -> None: ...

    session_factory: Any


class CollaborationService:
    def __init__(self, ports: CollaborationServicePorts) -> None:
        self._ports = ports

    def _build_swarm_tools(self) -> list[BaseTool]:

        @tool
        async def spawn_teammate(
            task: str, runtime: ToolRuntime[dict], system_prompt: str | None = None
        ) -> str:
            """派生一个持续存在的 Teammate 独立执行任务（后台运行），返回其 agent_id；它完成工作后会经消息向你汇报。"""
            return await self._spawn_swarm(task, system_prompt, "teammate", runtime)

        @tool
        async def spawn_worker(
            task: str, runtime: ToolRuntime[dict], system_prompt: str | None = None
        ) -> str:
            """派生一个持续存在的 Worker（后台运行），返回其 agent_id；它可认领并完成任务板任务。"""
            return await self._spawn_swarm(task, system_prompt, "worker", runtime)

        @tool
        async def wake_agent(
            agent_id: str, message: str, runtime: ToolRuntime[dict]
        ) -> str:
            """唤醒一个常驻 Agent（teammate/worker）并下达新指令；其历史与权限自动延续，可继续工作。"""
            context = runtime.context
            if not isinstance(context, dict) or not context.get("workspace"):
                raise RuntimeError("缺少工作区上下文: runtime.context['workspace']")
            return await self._wake_swarm(agent_id, message, context)

        @tool
        async def wait_for_swarm(
            agent_ids: list[str],
            runtime: ToolRuntime[dict],
            timeout_seconds: WaitSeconds = 30,
        ) -> str:
            """有界等待 Teammate/Worker 状态变化，返回运行、任务板和主 Agent 未读消息快照。"""
            context = runtime.context
            if not isinstance(context, dict) or not context.get("task_id"):
                raise RuntimeError("缺少协作上下文: runtime.context['task_id']")
            task_id = context["task_id"]
            snapshot = await self._wait_for_swarm(task_id, agent_ids, timeout_seconds)
            return json.dumps(snapshot, ensure_ascii=False)

        return [
            *declare_all_effects(
                [spawn_teammate, spawn_worker, wake_agent], DELEGATED_EXECUTION_EFFECT
            ),
            declare_effect(wait_for_swarm, NO_LOCAL_EFFECT),
        ]

    async def _wait_for_swarm(
        self, task_id: str, agent_ids: list[str], timeout_seconds: int
    ) -> dict[str, Any]:
        ports = self._ports
        if not agent_ids:
            raise ValueError("agent_ids 不能为空")
        timeout_seconds = validate_wait_seconds(timeout_seconds)
        targets = list(dict.fromkeys(agent_ids))
        async with ports.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(SwarmAgent).where(SwarmAgent.agent_id.in_(targets))
                    )
                )
                .scalars()
                .all()
            )
        valid = {
            row.agent_id
            for row in rows
            if row.task_id == task_id
            and row.role in ("teammate", "worker")
            and (row.status == "active")
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
                return {
                    "timed_out": True,
                    **await self._swarm_snapshot(task_id, targets),
                }
            await asyncio.sleep(min(0.1, remaining))
            current = await self._swarm_snapshot(task_id, targets)
            if (
                not self._snapshot_is_busy(current)
                or json.dumps(current, ensure_ascii=False, sort_keys=True)
                != baseline_key
            ):
                return {"timed_out": False, **current}

    async def _swarm_snapshot(
        self, task_id: str, agent_ids: list[str]
    ) -> dict[str, Any]:
        ports = self._ports
        async with ports.session_factory() as session:
            await session.connection(
                execution_options={"isolation_level": "REPEATABLE READ"}
            )
            runs = (
                (
                    await session.execute(
                        select(DesktopRun)
                        .where(DesktopRun.agent_id.in_(agent_ids))
                        .order_by(
                            DesktopRun.created_at.desc(), DesktopRun.run_id.desc()
                        )
                    )
                )
                .scalars()
                .all()
            )
            board_tasks = (
                (
                    await session.execute(
                        select(AgentBoardTask)
                        .where(AgentBoardTask.thread_task_id == task_id)
                        .order_by(
                            AgentBoardTask.created_at, AgentBoardTask.board_task_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            messages = (
                (
                    await session.execute(
                        select(AgentMessage)
                        .where(
                            AgentMessage.task_id == task_id,
                            AgentMessage.to_agent == f"main:{task_id}",
                            AgentMessage.read_at.is_(None),
                        )
                        .order_by(AgentMessage.created_at, AgentMessage.message_id)
                    )
                )
                .scalars()
                .all()
            )
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
            (agent["status"] in ("pending", "running") for agent in snapshot["agents"])
        )

    async def _spawn_swarm(
        self, task: str, system_prompt: str | None, role: str, runtime: Any
    ) -> str:
        ports = self._ports
        parent = security_context_of(runtime.context)
        task_id = runtime.context.get("task_id")
        workspace_id = parent.routing.workspace_id
        if not task_id:
            raise RuntimeError("缺少协作上下文: runtime.context['task_id']（任务身份）")
        if not workspace_id:
            raise RuntimeError(
                "缺少协作上下文: security_context.routing.workspace_id（工作区身份）"
            )
        source = await self._swarm_source(parent.routing.run_id)
        agent_id = new_id()
        await ports.agent_collab.create_swarm_agent(
            agent_id,
            task_id,
            role,
            list(parent.authorization.permissions),
            access_mode=str(parent.authorization.access_mode),
        )
        equipment = effective_equipment(
            source.equipment or {}, permissions=list(parent.authorization.permissions)
        )
        prompt = system_prompt or (
            ports._TEAMMATE_PROMPT if role == "teammate" else ports._WORKER_PROMPT
        )
        run_id = await self._launch_swarm_run(
            task_id,
            agent_id,
            role,
            task,
            prompt,
            workspace_id,
            str(parent.authorization.workspace),
            equipment,
            parent_run_id=source.run_id,
        )
        logger.info(
            "持久 Agent 已派生: agent_id='%s' role=%s run_id='%s'",
            agent_id,
            role,
            run_id,
        )
        return agent_id

    async def _wake_swarm(
        self, agent_id: str, message: str, context: dict[str, Any]
    ) -> str:
        ports = self._ports
        agent_row = await ports.agent_collab.get_swarm_agent(agent_id)
        if agent_row is None:
            raise _identity_unregistered(f"派生执行主体 {agent_id}")
        if agent_row.status == "stopped":
            raise HTTPException(409, "该 Agent 已停止")
        parent = security_context_of(context)
        source = await self._swarm_source(parent.routing.run_id)
        equipment = effective_equipment(
            source.equipment or {},
            permissions=list(agent_row.permissions or ["read"]),
            overrides={"access_mode": agent_row.access_mode},
        )
        prompt = (
            ports._TEAMMATE_PROMPT
            if agent_row.role == "teammate"
            else ports._WORKER_PROMPT
        )
        run_id = await self._launch_swarm_run(
            agent_row.task_id,
            agent_id,
            agent_row.role,
            message,
            prompt,
            parent.routing.workspace_id,
            str(parent.authorization.workspace),
            equipment,
            parent_run_id=source.run_id,
        )
        logger.info("持久 Agent 已唤醒: agent_id='%s' run_id='%s'", agent_id, run_id)
        return run_id

    async def _auto_wake_swarm(self, agent_id: str, message: str, depth: int) -> None:
        ports = self._ports
        try:
            agent_row = await ports.agent_collab.get_swarm_agent(agent_id)
            if agent_row is None or agent_row.status == "stopped":
                return
            async with ports.session_factory() as session:
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
                workspace_row = await session.get(
                    DesktopWorkspace, task_row.workspace_id
                )
                if not workspace_row:
                    return
                source = await session.scalar(
                    select(DesktopRun)
                    .where(DesktopRun.agent_id == agent_id)
                    .order_by(DesktopRun.created_at.desc())
                    .limit(1)
                )
                if source is None or source.loop_id is not None:
                    return
            equipment = effective_equipment(
                source.equipment or {},
                permissions=list(agent_row.permissions or ["read"]),
                overrides={"access_mode": agent_row.access_mode},
            )
            prompt = (
                ports._TEAMMATE_PROMPT
                if agent_row.role == "teammate"
                else ports._WORKER_PROMPT
            )
            await self._launch_swarm_run(
                agent_row.task_id,
                agent_id,
                agent_row.role,
                message,
                prompt,
                workspace_row.workspace_id,
                workspace_row.path,
                equipment,
                swarm_depth=depth,
                parent_run_id=source.run_id,
            )
            logger.info("自动唤醒已触发: agent_id='%s' depth=%s", agent_id, depth)
        except Exception:
            logger.warning("自动唤醒失败: agent_id=%s", agent_id, exc_info=True)

    async def _swarm_source(self, run_id: str) -> DesktopRun:
        ports = self._ports
        async with ports.session_factory() as session:
            source = await session.get(DesktopRun, run_id)
            if source is None:
                raise ValueError("协作派生缺少持久父执行")
            if source.loop_id is not None:
                raise ValueError(
                    "Loop 派生必须经过 Patrol Context Expansion 与 Kernel 授权"
                )
            return source

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
        ports = self._ports
        if await ports.agent_collab.is_agent_stopped(agent_id):
            raise HTTPException(409, "该 Agent 已停止")
        async with ports.session_factory() as session:
            task_row = await session.get(DesktopThread, task_id)
            if not task_row:
                raise _identity_unregistered(f"任务 {task_id}")
            agent_row = await session.get(SwarmAgent, agent_id)
            if agent_row is None:
                raise _identity_unregistered(f"派生执行主体 {agent_id}")
            checkpoint_ns = agent_row.checkpoint_ns
            thread_id = task_row.thread_id
            checkpoint_id = await select_checkpoint_base(
                ports.checkpointer, thread_id, checkpoint_ns
            )
            ports._require_run_admission()
            run = DesktopRun(
                run_id=new_id(),
                task_id=task_id,
                agent_id=agent_id,
                kind=role,
                status="pending",
                input_messages=[{"role": "human", "content": task_text}],
                model_name=equipment.get("model_name"),
                origin="swarm",
                execution_thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                context_revision_id=task_row.current_revision_id,
                parent_run_id=parent_run_id,
                equipment={
                    **equipment,
                    _RUN_DISPATCH_EQUIPMENT_KEY: {
                        "agent_role": role,
                        "base_prompt": system_prompt,
                        "checkpoint_id": checkpoint_id,
                        "allow_global_config": False,
                        "swarm_depth": swarm_depth,
                    },
                },
                workspace_anchor={
                    "workspace_id": workspace_id,
                    "workspace_path": workspace_path,
                },
            )
            await ports._admission_service().admit(session, run)
            await session.commit()
        ports.notify_run_dispatch()
        return run.run_id

    def _build_swarm_reader_tools(self, task_id: str) -> list[BaseTool]:
        ports = self._ports

        @tool
        async def read_swarm_agent_history(agent_id: str) -> str:
            """读取当前任务指定 Teammate/Worker 已提交的完整消息历史，不读取小兵。"""
            agent = await ports.agent_collab.get_swarm_agent(agent_id)
            if agent is None or agent.task_id != task_id:
                raise ValueError("该协作 Agent 不属于当前任务")
            thread_id = await ports._task_thread_id(task_id)
            messages = await ports.get_checkpoint_messages(
                thread_id, agent.checkpoint_ns
            )
            return json.dumps(messages, ensure_ascii=False)

        return declare_all_effects([read_swarm_agent_history], NO_LOCAL_EFFECT)
