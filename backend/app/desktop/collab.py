"""
本文件对外提供 AgentCollab 类，集中实现 Claude Code 三套 subagent 机制的协作能力：
SendMessage（点对点/广播）、任务板（Coordinator CAS 机械协议）、计划审批与关机机械协议、
未读消息回合注入（按 kind 分拣）。

输入为已初始化的 PostgreSQL session factory；输出为：
    build_collab_tools(role) — 按角色构建协作工具列表（main: send/approve_plan/respond_shutdown/
                              publish_task/list_board_tasks；teammate: send/request_plan_approval/
                              request_shutdown；worker: send/claim_task/complete_task；patrol: 空）
    load_unread_messages(agent_id, task_id) — 查询未读消息并按 kind 分拣组装注入块，随后标记已读
    create_swarm_agent / list_swarm_agent_ids / is_agent_stopped — 持久 Agent 身份管理

具体工作流为：send_message 落表（from=当前 agent，kind 区分文本与协议消息，to_agent="*" 广播）；
目标 agent 下一次 run 装配时 load_unread_messages 按 kind 分拣——"message" 进 <agent_messages>
文本块，协议 kind 进 <agent_protocol> 块（from/kind 标签），统一读后标已读（消费即销毁，防膨胀）；
计划审批为回合制（teammate 请求 → main 响应 → teammate 回合注入）；关机批准为代码层动作
（swarm_agents.status 置 stopped）；任务板以数据库 CAS 更新强制状态机。

示例：
    collab = AgentCollab(session_factory)
    tools = collab.build_collab_tools(role="teammate")
    block = await collab.load_unread_messages(agent_id="swarm:abc", task_id="t-1")
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

logger = logging.getLogger(__name__)

# 消息驱动自动唤醒的深度上限（防 A→B→A→B 乒乓；超限只落表排队等主 Agent wake）
_SWARM_DEPTH_LIMIT = 3

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


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _collab_values(runtime: ToolRuntime) -> tuple[str, str]:
    """从 runtime.context 提取当前 agent_id 与 task_id。"""
    context = runtime.context
    if not isinstance(context, dict):
        raise RuntimeError("缺少协作上下文: runtime.context 必须为 dict")
    agent_id = context.get("agent_id")
    task_id = context.get("task_id")
    if not agent_id or not task_id:
        raise RuntimeError("缺少协作上下文: runtime.context['agent_id'] / ['task_id']")
    return agent_id, task_id


class AgentCollab:
    """Agent 间协作：SendMessage、任务板、协议消息（审批/关机）与回合注入。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        swarm_launcher: Callable[[str, str, int], Awaitable[None]] | None = None,
    ) -> None:
        """初始化 AgentCollab。

        输入:
            session_factory: async_sessionmaker — 数据库会话工厂
            swarm_launcher: Callable | None — 消息驱动自动唤醒的执行器（service 层提供，
                签名 (agent_id, message, depth)），None 时禁用自动触发（仅落表）
        """
        self.session_factory = session_factory
        self.swarm_launcher = swarm_launcher

    # === 工具构建 ===

    def build_collab_tools(self, role: str) -> list[BaseTool]:
        """按角色构建协作工具列表。

        输入:
            role: str — "main"（Team Lead/Coordinator）、"teammate"、"worker"；"patrol" 返回空列表

        输出:
            list[BaseTool] — 该角色可用的协作工具
        """
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
            to_agent: str, content: str, runtime: ToolRuntime, kind: str = "message"
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
            source_depth = runtime.context.get("swarm_depth", 0) if isinstance(runtime.context, dict) else 0
            self._maybe_auto_wake(task_id, to_agent, content, int(source_depth) + 1)
            return f"消息已发送给 {to_agent}"

        return send_message

    def _maybe_auto_wake(self, task_id: str, to_agent: str, content: str, depth: int) -> None:
        """落表后评估自动触发目标 agent 的一轮 run（消息驱动自动唤醒）。

        输入:
            task_id: str — 所属任务
            to_agent: str — 目标 agent
            content: str — 触发 run 的输入消息
            depth: int — 目标 run 的 swarm_depth（来源 depth+1）

        工作流:
            (1) 未注入 launcher 或目标为主 Agent（main:{task_id}）→ 不触发（仅落表）
            (2) depth 达上限 → 不触发（防环截断，消息排队等主 Agent wake）
            (3) 异步调用 launcher（由 service 层查忙/查身份后触发 run）
        """
        if self.swarm_launcher is None:
            return
        if to_agent == f"main:{task_id}":
            return  # main 代表用户，永不被自动触发（须用户驱动）
        if depth >= _SWARM_DEPTH_LIMIT:
            logger.info("自动唤醒深度达上限 (%s)，仅落表排队: to_agent=%s", _SWARM_DEPTH_LIMIT, to_agent)
            return
        try:
            asyncio.create_task(self.swarm_launcher(to_agent, content, depth))
        except RuntimeError:
            pass

    def build_request_plan_approval_tool(self) -> BaseTool:
        @tool
        async def request_plan_approval(plan: str, runtime: ToolRuntime) -> str:
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
        async def request_shutdown(reason: str, runtime: ToolRuntime) -> str:
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
            agent_id: str, approved: bool, runtime: ToolRuntime, feedback: str | None = None
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
        async def respond_shutdown(agent_id: str, approved: bool, runtime: ToolRuntime) -> str:
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
        async def publish_task(description: str, runtime: ToolRuntime, requirements: str | None = None) -> str:
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
                # 任务板只有 active worker 能消费；消息携带任务 ID，供 claim_task 认领
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
        async def list_board_tasks(runtime: ToolRuntime) -> str:
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
        async def claim_task(board_task_id: str, runtime: ToolRuntime) -> str:
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
        async def complete_task(board_task_id: str, result: str, runtime: ToolRuntime) -> str:
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

    # === 持久 Agent 身份（机制③④）===

    async def create_swarm_agent(
        self, agent_id: str, task_id: str, role: str, permissions: list[str] | None = None
    ) -> None:
        """创建持久 Agent 身份行（checkpoint_ns=swarm:{agent_id}，status=active，permissions 持久化供 wake 沿用）。"""
        async with self.session_factory() as session:
            session.add(SwarmAgent(
                agent_id=agent_id, task_id=task_id, role=role,
                checkpoint_ns=f"swarm:{agent_id}", status="active",
                permissions=permissions or ["read"],
            ))
            await session.commit()

    async def get_swarm_agent(self, agent_id: str) -> SwarmAgent | None:
        """按 agent_id 查询持久 Agent 身份行（wake 等入口校验存在性、角色与权限）。"""
        async with self.session_factory() as session:
            return await session.get(SwarmAgent, agent_id)

    async def list_swarm_agent_ids(
        self, task_id: str, *, role: str | None = None, status: str | None = None
    ) -> list[str]:
        """按可选角色/状态过滤任务下的持久 Agent ID。"""
        query = select(SwarmAgent.agent_id).where(SwarmAgent.task_id == task_id)
        if role is not None:
            query = query.where(SwarmAgent.role == role)
        if status is not None:
            query = query.where(SwarmAgent.status == status)
        async with self.session_factory() as session:
            rows = (await session.execute(query)).scalars().all()
        return list(rows)

    async def is_agent_stopped(self, agent_id: str) -> bool:
        """查询持久 Agent 是否已关机（status=stopped）。"""
        async with self.session_factory() as session:
            status = await session.scalar(
                select(SwarmAgent.status).where(SwarmAgent.agent_id == agent_id)
            )
        return status == "stopped"

    async def _stop_swarm_agent(self, agent_id: str) -> bool:
        """代码层关机：将 Agent 状态置 stopped（CAS 仅 active 可停）。"""
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
        """广播目标：主 Agent + 任务下全部持久 Agent。"""
        swarm_ids = await self.list_swarm_agent_ids(task_id)
        return [f"main:{task_id}", *swarm_ids]

    # === 回合注入 ===

    async def load_unread_messages(self, agent_id: str, task_id: str) -> str:
        """查询当前 agent 的未读消息，按 kind 分拣组装注入块，随后标记已读。

        输入:
            agent_id: str — 当前 run 的 agent 标识
            task_id: str — 所属任务 ID

        输出:
            str — 未读消息非空时返回注入块（"message" 进 <agent_messages> 文本块，协议 kind
                  进 <agent_protocol> 块，均带 from/kind 标签），否则返回空字符串

        工作流:
            (1) 查询 to_agent=agent_id 且 read_at 为 NULL 的消息（按创建时间排序）
            (2) 按 kind 分拣组装注入块（内容转义）
            (3) 同一事务内将所有消息标记已读（消费即销毁）
        """
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(AgentMessage)
                    .where(
                        AgentMessage.to_agent == agent_id,
                        AgentMessage.task_id == task_id,
                        AgentMessage.read_at.is_(None),
                    )
                    .order_by(AgentMessage.created_at)
                )
            ).scalars().all()
            if not rows:
                return ""
            text_blocks: list[str] = []
            protocol_blocks: list[str] = []
            for row in rows:
                stamp = row.created_at.isoformat() if row.created_at else ""
                if row.kind == "message":
                    text_blocks.append(
                        f'<agent_message from="{row.from_agent}" at="{stamp}">{_escape(row.content)}</agent_message>'
                    )
                else:
                    protocol_blocks.append(
                        f'<agent_protocol from="{row.from_agent}" kind="{row.kind}">{_escape(row.content)}</agent_protocol>'
                    )
            now = datetime.now(timezone.utc)
            for row in rows:
                row.read_at = now
            await session.commit()
        parts: list[str] = []
        if text_blocks:
            parts.append("<agent_messages>\n" + "\n".join(text_blocks) + "\n</agent_messages>")
        if protocol_blocks:
            parts.append("<agent_protocols>\n" + "\n".join(protocol_blocks) + "\n</agent_protocols>")
        return "\n\n".join(parts)
