"""
本文件对外提供 AgentCollab 类，集中实现 Claude Code 三套 subagent 机制的协作能力：
SendMessage（点对点/广播）、任务板（Coordinator CAS 机械协议）、计划审批与关机机械协议、
协作消息发送与协议处理；投递由独立 AgentInbox 管理。

输入为已初始化的 PostgreSQL session factory；协作工具另需运行上下文提供执行主体身份与任务身份
（`runtime.context` 的 `agent_id` / `task_id`）以及唤醒链深度 `swarm_depth`——三者都是受治理键，缺失即
显式失败并指明是哪一个键，绝不取默认值。输出为：
    build_collab_tools(role) — 按角色构建协作工具列表（main: send/approve_plan/respond_shutdown/
                              publish_task/list_board_tasks；teammate: send/request_plan_approval/
                              request_shutdown；worker: send/claim_task/complete_task；patrol: 空）
    create_swarm_agent / list_swarm_agent_ids / is_agent_stopped — 持久 Agent 身份管理

效果声明：协作工具只读写会话数据库，不产生受治理的本地文件副作用，由 build_collab_tools
统一签发为无本地效果。

具体工作流为：send_message 落表（from=当前 agent，kind 区分文本与协议消息，to_agent="*" 广播）；
目标 agent 通过 AgentInbox 只读取得 typed collaboration 内容，精确 checkpoint 提交后确认投递；
计划审批为回合制（teammate 请求 → main 响应 → teammate 回合注入）；关机批准为代码层动作
（swarm_agents.status 置 stopped）；任务板以数据库 CAS 更新强制状态机。

示例：
    collab = AgentCollab(session_factory)
    tools = collab.build_collab_tools(role="teammate")
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
import uuid

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.models import AgentBoardTask, AgentMessage, SwarmAgent
from focus.security.effects import NO_LOCAL_EFFECT, declare_all_effects
from focus.security.governed import declare_governed_keys
from focus.security.launch import SWARM_DEPTH_CONTEXT_KEY

logger = logging.getLogger(__name__)


_SWARM_DEPTH_LIMIT = 3


declare_governed_keys("agent_id", "task_id", "swarm_depth")

_MESSAGE_KINDS = frozenset({
    "message",
    "plan_approval_request",
    "plan_approval_response",
    "shutdown_request",
    "shutdown_response",
})
_PROTOCOL_KINDS = frozenset({
    "plan_approval_request",
    "plan_approval_response",
    "shutdown_request",
    "shutdown_response",
})


def _new_id() -> str:
    return uuid.uuid4().hex


def _context_value(context: object, key: str, label: str) -> str:


    if not isinstance(context, dict):
        raise RuntimeError("缺少协作上下文: runtime.context 必须为 dict")
    if key not in context or context[key] in (None, ""):
        raise RuntimeError(f"缺少协作上下文: runtime.context['{key}']（{label}）")
    return str(context[key])


def _collab_values(runtime: ToolRuntime[dict]) -> tuple[str, str]:

    context = runtime.context
    agent_id = _context_value(context, "agent_id", "执行主体身份")
    task_id = _context_value(context, "task_id", "任务身份")
    return agent_id, task_id


class AgentCollab:
    """Agent 间协作：SendMessage、任务板、协议消息（审批/关机）与回合注入。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        swarm_launcher: Callable[[str, str, int], Awaitable[None]] | None = None,
    ) -> None:


        self.session_factory = session_factory
        self.swarm_launcher = swarm_launcher


    def build_collab_tools(self, role: str) -> list[BaseTool]:


        return declare_all_effects(self._role_tools(role), NO_LOCAL_EFFECT)

    def _role_tools(self, role: str) -> list[BaseTool]:

        if role == "main":
            return [
                self.build_send_message_tool(),
                self.build_approve_plan_tool(),
                self.build_respond_shutdown_tool(),
                self.build_publish_task_tool(),
                self.build_list_board_tasks_tool(),
            ]
        if role == "teammate":
            return [
                self.build_send_message_tool(),
                self.build_request_plan_approval_tool(),
                self.build_request_shutdown_tool(),
            ]
        if role == "worker":
            return [
                self.build_send_message_tool(),
                self.build_claim_task_tool(),
                self.build_complete_task_tool(),
            ]
        return []

    def build_send_message_tool(self) -> BaseTool:
        @tool
        async def send_message(
            to_agent: str, content: str, runtime: ToolRuntime[dict], kind: str = "message"
        ) -> str:
            """给指定 Agent 发送消息；to_agent="*" 广播给主 Agent 与全部协作 Agent。对方下一次运行时读取，每条消息只被消费一次。"""
            agent_id, task_id = _collab_values(runtime)
            if kind not in _MESSAGE_KINDS:
                raise ValueError(f"未知消息类型: {kind}")
            if not content.strip():
                raise ValueError("消息内容不能为空")
            targets = await self._broadcast_targets(task_id) if to_agent == "*" else [to_agent]
            async with self.session_factory() as session:
                for target in targets:
                    session.add(AgentMessage(
                        message_id=_new_id(), task_id=task_id, from_agent=agent_id,
                        to_agent=target, kind=kind, content=content,
                    ))
                await session.commit()
            if to_agent == "*":
                return f"广播已发送给 {len(targets)} 个 Agent"
            source_depth = _context_value(runtime.context, SWARM_DEPTH_CONTEXT_KEY, "唤醒链深度")
            self._maybe_auto_wake(task_id, to_agent, content, int(source_depth) + 1)
            return f"消息已发送给 {to_agent}"

        return send_message

    def _maybe_auto_wake(self, task_id: str, to_agent: str, content: str, depth: int) -> None:


        if self.swarm_launcher is None:
            return
        if to_agent == f"main:{task_id}":
            return
        if depth >= _SWARM_DEPTH_LIMIT:
            logger.info("自动唤醒深度达上限 (%s)，仅落表排队: to_agent=%s", _SWARM_DEPTH_LIMIT, to_agent)
            return
        try:
            asyncio.create_task(self.swarm_launcher(to_agent, content, depth))
        except RuntimeError:
            pass

    def build_request_plan_approval_tool(self) -> BaseTool:
        @tool
        async def request_plan_approval(plan: str, runtime: ToolRuntime[dict]) -> str:
            """向主 Agent 提交计划审批请求；主 Agent 下一次运行时可看到并批准或拒绝。"""
            agent_id, task_id = _collab_values(runtime)
            async with self.session_factory() as session:
                session.add(AgentMessage(
                    message_id=_new_id(), task_id=task_id, from_agent=agent_id,
                    to_agent=f"main:{task_id}", kind="plan_approval_request", content=plan,
                ))
                await session.commit()
            return "计划审批请求已提交给主 Agent"

        return request_plan_approval

    def build_request_shutdown_tool(self) -> BaseTool:
        @tool
        async def request_shutdown(reason: str, runtime: ToolRuntime[dict]) -> str:
            """请求主 Agent 批准关机（停止你的后续运行）；主 Agent 下一次运行时可批准或拒绝。"""
            agent_id, task_id = _collab_values(runtime)
            async with self.session_factory() as session:
                session.add(AgentMessage(
                    message_id=_new_id(), task_id=task_id, from_agent=agent_id,
                    to_agent=f"main:{task_id}", kind="shutdown_request", content=reason,
                ))
                await session.commit()
            return "关机请求已提交给主 Agent"

        return request_shutdown

    def build_approve_plan_tool(self) -> BaseTool:
        @tool
        async def approve_plan(
            agent_id: str, approved: bool, runtime: ToolRuntime[dict], feedback: str | None = None
        ) -> str:
            """批准或拒绝某 Agent 的计划审批请求；结果该 Agent 下一次运行时可见。"""
            _, task_id = _collab_values(runtime)
            content = (
                f"计划已批准。反馈：{feedback}" if approved
                else f"计划被拒绝。反馈：{feedback or '请修改后重新提交'}"
            )
            async with self.session_factory() as session:
                session.add(AgentMessage(
                    message_id=_new_id(), task_id=task_id, from_agent=f"main:{task_id}",
                    to_agent=agent_id, kind="plan_approval_response", content=content,
                ))
                await session.commit()
            return f"审批结果已发送给 {agent_id}"

        return approve_plan

    def build_respond_shutdown_tool(self) -> BaseTool:
        @tool
        async def respond_shutdown(agent_id: str, approved: bool, runtime: ToolRuntime[dict]) -> str:
            """批准或拒绝某 Agent 的关机请求；批准时立即停止该 Agent 后续运行（代码层），拒绝时该 Agent 下一次运行可见。"""
            _, task_id = _collab_values(runtime)
            if approved:
                stopped = await self._stop_swarm_agent(agent_id)
                return f"已批准关机，该 Agent 已停止后续运行" if stopped else "该 Agent 不存在或已停止"
            async with self.session_factory() as session:
                session.add(AgentMessage(
                    message_id=_new_id(), task_id=task_id, from_agent=f"main:{task_id}",
                    to_agent=agent_id, kind="shutdown_response", content="关机请求被拒绝，可继续运行",
                ))
                await session.commit()
            return f"已拒绝关机，结果已发送给 {agent_id}"

        return respond_shutdown

    def build_publish_task_tool(self) -> BaseTool:
        @tool
        async def publish_task(description: str, runtime: ToolRuntime[dict], requirements: str | None = None) -> str:
            """发布任务到任务板（status=pending），Worker 可认领执行；发布后空闲 Worker 会被自动唤醒。"""
            _, task_id = _collab_values(runtime)
            board = AgentBoardTask(
                board_task_id=_new_id(), thread_task_id=task_id,
                description=description, requirements=requirements, status="pending",
            )
            async with self.session_factory() as session:
                session.add(board)
                await session.commit()
            if self.swarm_launcher is not None:

                wake_message = (
                    f"任务板有新任务：{description}（board_task_id={board.board_task_id}），"
                    f"请用 claim_task 认领。"
                )
                for target in await self.list_swarm_agent_ids(
                    task_id, role="worker", status="active"
                ):
                    self._maybe_auto_wake(task_id, target, wake_message, 1)
            return f"任务已发布到看板: {board.board_task_id}"

        return publish_task

    def build_list_board_tasks_tool(self) -> BaseTool:
        @tool
        async def list_board_tasks(runtime: ToolRuntime[dict]) -> str:
            """查看任务板全部任务的状态、认领者与结果。"""
            _, task_id = _collab_values(runtime)
            async with self.session_factory() as session:
                rows = (
                    await session.execute(
                        select(AgentBoardTask)
                        .where(AgentBoardTask.thread_task_id == task_id)
                        .order_by(AgentBoardTask.created_at)
                    )
                ).scalars().all()
            return json.dumps(
                [
                    {
                        "board_task_id": row.board_task_id,
                        "status": row.status,
                        "claimed_by": row.claimed_by,
                        "description": row.description,
                        "requirements": row.requirements,
                        "result": row.result,
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                    }
                    for row in rows
                ],
                ensure_ascii=False,
            )

        return list_board_tasks

    def build_claim_task_tool(self) -> BaseTool:
        @tool
        async def claim_task(board_task_id: str, runtime: ToolRuntime[dict]) -> str:
            """认领任务板上一个 pending 任务；已被认领或不属于当前任务时认领失败。"""
            agent_id, task_id = _collab_values(runtime)
            async with self.session_factory() as session:
                result = await session.execute(
                    update(AgentBoardTask)
                    .where(
                        AgentBoardTask.board_task_id == board_task_id,
                        AgentBoardTask.thread_task_id == task_id,
                        AgentBoardTask.status == "pending",
                    )
                    .values(status="claimed", claimed_by=agent_id, claimed_at=datetime.now(timezone.utc))
                    .returning(AgentBoardTask.board_task_id)
                )
                await session.commit()
            if result.scalar() is None:
                return "认领失败：任务不存在、已被他人认领或不属于当前任务"
            return "认领成功"

        return claim_task

    def build_complete_task_tool(self) -> BaseTool:
        @tool
        async def complete_task(board_task_id: str, result: str, runtime: ToolRuntime[dict]) -> str:
            """完成自己认领的任务并提交结果；仅认领者本人可完成。"""
            agent_id, task_id = _collab_values(runtime)
            async with self.session_factory() as session:
                updated = await session.execute(
                    update(AgentBoardTask)
                    .where(
                        AgentBoardTask.board_task_id == board_task_id,
                        AgentBoardTask.thread_task_id == task_id,
                        AgentBoardTask.status == "claimed",
                        AgentBoardTask.claimed_by == agent_id,
                    )
                    .values(status="completed", result=result, completed_at=datetime.now(timezone.utc))
                    .returning(AgentBoardTask.board_task_id)
                )
                await session.commit()
            if updated.scalar() is None:
                return "提交失败：任务未认领、非本人认领或已完成"
            return "任务完成已提交"

        return complete_task


    async def create_swarm_agent(
        self, agent_id: str, task_id: str, role: str, permissions: list[str] | None = None,
        access_mode: str = "workspace-write",
    ) -> None:


        async with self.session_factory() as session:
            session.add(SwarmAgent(
                agent_id=agent_id, task_id=task_id, role=role,
                checkpoint_ns=f"swarm:{agent_id}", status="active",
                permissions=permissions or ["read"],
                access_mode=access_mode,
            ))
            await session.commit()

    async def get_swarm_agent(self, agent_id: str) -> SwarmAgent | None:

        async with self.session_factory() as session:
            return await session.get(SwarmAgent, agent_id)

    async def list_swarm_agent_ids(
        self, task_id: str, *, role: str | None = None, status: str | None = None
    ) -> list[str]:

        query = select(SwarmAgent.agent_id).where(SwarmAgent.task_id == task_id)
        if role is not None:
            query = query.where(SwarmAgent.role == role)
        if status is not None:
            query = query.where(SwarmAgent.status == status)
        async with self.session_factory() as session:
            rows = (await session.execute(query)).scalars().all()
        return list(rows)

    async def is_agent_stopped(self, agent_id: str) -> bool:

        async with self.session_factory() as session:
            status = await session.scalar(
                select(SwarmAgent.status).where(SwarmAgent.agent_id == agent_id)
            )
        return status == "stopped"

    async def _stop_swarm_agent(self, agent_id: str) -> bool:

        async with self.session_factory() as session:
            result = await session.execute(
                update(SwarmAgent)
                .where(SwarmAgent.agent_id == agent_id, SwarmAgent.status == "active")
                .values(status="stopped", stopped_at=datetime.now(timezone.utc))
                .returning(SwarmAgent.agent_id)
            )
            await session.commit()
        return result.scalar() is not None

    async def _broadcast_targets(self, task_id: str) -> list[str]:

        swarm_ids = await self.list_swarm_agent_ids(task_id)
        return [f"main:{task_id}", *swarm_ids]
