"""Swarm/Coordinator 机制测试：广播、协议分拣、计划审批回合、关机状态机（独立于小兵机制）。"""

import asyncio
import atexit
import os
import uuid

import pytest
from langchain.tools import ToolRuntime
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# asyncpg 连接池绑定事件循环：模块级共享一个 loop，不得 set_event_loop（避免污染 TestClient）
if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
_LOOP = asyncio.new_event_loop()

# 独立 engine，不碰全局单例（poc 测试依赖 lifespan 的 dispose_engine）
pytestmark = pytest.mark.usefixtures("isolated_postgres_database")

_ENGINE = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
_SESSION_FACTORY = async_sessionmaker(_ENGINE, expire_on_commit=False)
atexit.register(lambda: _LOOP.run_until_complete(_ENGINE.dispose()))

from backend.app.desktop.collab import AgentCollab  # noqa: E402
from backend.app.desktop.models import (  # noqa: E402
    AgentMessage,
    DesktopThread,
    DesktopWorkspace,
    SwarmAgent,
)


def _runtime(agent_id: str, task_id: str) -> ToolRuntime:
    return ToolRuntime(
        state={}, context={"agent_id": agent_id, "task_id": task_id},
        config={}, stream_writer=None, tool_call_id=None, store=None, tools=[],
    )


async def _seed(collab: AgentCollab) -> tuple[str, list[str]]:
    task_id = f"swarm-{uuid.uuid4().hex[:14]}"
    workspace_id = f"ws-{uuid.uuid4().hex[:14]}"
    async with collab.session_factory() as session:
        session.add(
            DesktopWorkspace(workspace_id=workspace_id, path=f"/tmp/{workspace_id}", display_name="swarm-test")
        )
        await session.flush()
        session.add(
            DesktopThread(
                task_id=task_id, workspace_id=workspace_id,
                thread_id=f"th-{uuid.uuid4().hex[:10]}", title="swarm",
            )
        )
        await session.commit()
    teammate_id = uuid.uuid4().hex
    worker_id = uuid.uuid4().hex
    await collab.create_swarm_agent(teammate_id, task_id, "teammate")
    await collab.create_swarm_agent(worker_id, task_id, "worker")
    return task_id, [teammate_id, worker_id]


async def _cleanup(collab: AgentCollab, task_id: str) -> None:
    async with collab.session_factory() as session:
        workspace_ids = list(
            (
                await session.execute(
                    select(DesktopThread.workspace_id).where(DesktopThread.task_id == task_id)
                )
            ).scalars()
        )
        await session.execute(delete(AgentMessage).where(AgentMessage.task_id == task_id))
        await session.execute(delete(SwarmAgent).where(SwarmAgent.task_id == task_id))
        await session.execute(delete(DesktopThread).where(DesktopThread.task_id == task_id))
        await session.execute(
            delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id.in_(workspace_ids))
        )
        await session.commit()


def test_broadcast_writes_per_target_and_independent_consume():
    async def run() -> None:
        collab = AgentCollab(_SESSION_FACTORY)
        task_id, [teammate_id, worker_id] = await _seed(collab)
        try:
            send = collab.build_send_message_tool()
            main = _runtime(f"main:{task_id}", task_id)

            result = await send.ainvoke(
                {"to_agent": "*", "content": "周五前完成各自任务", "runtime": main}
            )
            assert "广播" in result

            # 逐行落表：main + 2 个 swarm agents 各一行
            async with collab.session_factory() as session:
                total = await session.scalar(
                    select(func.count()).select_from(AgentMessage)
                    .where(AgentMessage.task_id == task_id, AgentMessage.content == "周五前完成各自任务")
                )
                assert total == 3

            # 任一 agent 消费不影响其他（独立 read_at）
            assert "周五前完成各自任务" in await collab.load_unread_messages(f"main:{task_id}", task_id)
            assert await collab.load_unread_messages(f"main:{task_id}", task_id) == ""
            assert "周五前完成各自任务" in await collab.load_unread_messages(teammate_id, task_id)
            assert "周五前完成各自任务" in await collab.load_unread_messages(worker_id, task_id)
        finally:
            await _cleanup(collab, task_id)

    _LOOP.run_until_complete(run())


def test_peer_to_peer_message_between_teammates():
    async def run() -> None:
        collab = AgentCollab(_SESSION_FACTORY)
        task_id, [teammate_a, teammate_b] = await _seed(collab)
        try:
            send = collab.build_send_message_tool()
            rt_a = _runtime(teammate_a, task_id)
            rt_b = _runtime(teammate_b, task_id)

            # A → B 点对点
            result = await send.ainvoke(
                {"to_agent": teammate_b, "content": "帮我查一下竞品定价", "runtime": rt_a}
            )
            assert f"已发送给 {teammate_b}" in result
            block_b = await collab.load_unread_messages(teammate_b, task_id)
            assert f'from="{teammate_a}"' in block_b
            assert "竞品定价" in block_b
            # 消费即销毁
            assert await collab.load_unread_messages(teammate_b, task_id) == ""

            # B → A 回复（双向）
            await send.ainvoke(
                {"to_agent": teammate_a, "content": "查到了，三档定价", "runtime": rt_b}
            )
            block_a = await collab.load_unread_messages(teammate_a, task_id)
            assert f'from="{teammate_b}"' in block_a
            assert "三档定价" in block_a

            # 无关的第三个 agent 收不到（消息精确投递）
            third = uuid.uuid4().hex
            await collab.create_swarm_agent(third, task_id, "worker")
            assert await collab.load_unread_messages(third, task_id) == ""
        finally:
            await _cleanup(collab, task_id)

    _LOOP.run_until_complete(run())


def test_message_driven_auto_wakeup():
    async def run() -> None:
        calls: list[tuple[str, str, int]] = []

        async def fake_launcher(agent_id: str, message: str, depth: int) -> None:
            calls.append((agent_id, message, depth))

        collab = AgentCollab(_SESSION_FACTORY, swarm_launcher=fake_launcher)
        task_id, [teammate_a, teammate_b] = await _seed(collab)
        try:
            send = collab.build_send_message_tool()
            rt_a = _runtime(teammate_a, task_id)

            # A→B 点对点：落表 + 自动触发 B（depth=1）
            await send.ainvoke({"to_agent": teammate_b, "content": "帮我查定价", "runtime": rt_a})
            assert (teammate_b, "帮我查定价", 1) in calls

            # 来源 depth=2 → 触发 depth=3 达上限 → 仅落表不触发（防环截断）
            deep_ctx = ToolRuntime(
                state={}, context={"agent_id": teammate_a, "task_id": task_id, "swarm_depth": 2},
                config={}, stream_writer=None, tool_call_id=None, store=None, tools=[],
            )
            await send.ainvoke({"to_agent": teammate_b, "content": "再查一轮", "runtime": deep_ctx})
            assert not any(call[2] >= 3 for call in calls)

            # main 目标 → 不触发（只落表）
            before = len(calls)
            await send.ainvoke({"to_agent": f"main:{task_id}", "content": "汇报完成", "runtime": rt_a})
            assert len(calls) == before

            # publish_task → 只触发 active worker（teammate 没有 claim_task 权限）
            calls.clear()
            publish = collab.build_publish_task_tool()
            rt_main = _runtime(f"main:{task_id}", task_id)
            out = await publish.ainvoke({"description": "统计代码行数", "runtime": rt_main})
            board_id = out.split(": ")[-1]
            assert len(calls) == 1
            assert calls[0][0] == teammate_b
            assert calls[0][2] == 1
            assert board_id in calls[0][1]

            # 查询过滤能力由 publish_task 复用；广播仍使用无过滤查询。
            assert await collab.list_swarm_agent_ids(task_id, role="teammate") == [teammate_a]
            assert await collab.list_swarm_agent_ids(
                task_id, role="worker", status="active"
            ) == [teammate_b]
        finally:
            await _cleanup(collab, task_id)

    _LOOP.run_until_complete(run())


def test_protocol_kind_segregation():
    async def run() -> None:
        collab = AgentCollab(_SESSION_FACTORY)
        task_id, [teammate_id, _] = await _seed(collab)
        try:
            request_plan = collab.build_request_plan_approval_tool()
            request_shutdown = collab.build_request_shutdown_tool()
            teammate = _runtime(teammate_id, task_id)

            await request_plan.ainvoke({"plan": "先重构核心模块再补测试", "runtime": teammate})
            await request_shutdown.ainvoke({"reason": "工作已完成", "runtime": teammate})

            block = await collab.load_unread_messages(f"main:{task_id}", task_id)
            # 协议消息进 <agent_protocols> 块（from/kind 标签），不进 <agent_messages>
            assert "<agent_protocols>" in block
            assert f'from="{teammate_id}"' in block
            assert 'kind="plan_approval_request"' in block
            assert 'kind="shutdown_request"' in block
            assert "先重构核心模块再补测试" in block
            assert "<agent_messages>" not in block
            # 只注入 main：teammate 自己看不到（无发送给它的协议消息）
            assert await collab.load_unread_messages(teammate_id, task_id) == ""
        finally:
            await _cleanup(collab, task_id)

    _LOOP.run_until_complete(run())


def test_plan_approval_roundtrip():
    async def run() -> None:
        collab = AgentCollab(_SESSION_FACTORY)
        task_id, [teammate_id, _] = await _seed(collab)
        try:
            request_plan = collab.build_request_plan_approval_tool()
            approve = collab.build_approve_plan_tool()
            teammate = _runtime(teammate_id, task_id)
            main = _runtime(f"main:{task_id}", task_id)

            await request_plan.ainvoke({"plan": "先补测试", "runtime": teammate})
            assert 'kind="plan_approval_request"' in await collab.load_unread_messages(f"main:{task_id}", task_id)

            result = await approve.ainvoke(
                {"agent_id": teammate_id, "approved": False, "feedback": "先出设计文档", "runtime": main}
            )
            assert "已发送" in result

            response_block = await collab.load_unread_messages(teammate_id, task_id)
            assert "<agent_protocols>" in response_block
            assert 'kind="plan_approval_response"' in response_block
            assert "计划被拒绝" in response_block
            assert "先出设计文档" in response_block
        finally:
            await _cleanup(collab, task_id)

    _LOOP.run_until_complete(run())


def test_shutdown_state_machine():
    async def run() -> None:
        collab = AgentCollab(_SESSION_FACTORY)
        task_id, [teammate_id, _] = await _seed(collab)
        try:
            respond = collab.build_respond_shutdown_tool()
            main = _runtime(f"main:{task_id}", task_id)

            # 拒绝：协议注入（可继续），状态不变
            result = await respond.ainvoke(
                {"agent_id": teammate_id, "approved": False, "runtime": main}
            )
            assert "拒绝" in result
            assert await collab.is_agent_stopped(teammate_id) is False
            block = await collab.load_unread_messages(teammate_id, task_id)
            assert 'kind="shutdown_response"' in block
            assert "被拒绝" in block

            # 批准：代码层置 stopped（无消息注入）
            result = await respond.ainvoke(
                {"agent_id": teammate_id, "approved": True, "runtime": main}
            )
            assert "已停止" in result
            assert await collab.is_agent_stopped(teammate_id) is True
            assert await collab.load_unread_messages(teammate_id, task_id) == ""

            # 重复批准：幂等（已停止）
            result = await respond.ainvoke(
                {"agent_id": teammate_id, "approved": True, "runtime": main}
            )
            assert "不存在或已停止" in result
        finally:
            await _cleanup(collab, task_id)

    _LOOP.run_until_complete(run())
