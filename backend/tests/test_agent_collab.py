"""AgentCollab 协作能力测试：Mailbox 消息回合消费、任务板 CAS 机械协议。"""

import asyncio
import atexit
import os
import uuid

import pytest
from langchain.tools import ToolRuntime
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# asyncpg 连接池绑定事件循环：模块级共享一个 loop，但不得 set_event_loop
# （会污染后续 TestClient 的 loop 选择），仅显式 run_until_complete
if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
_LOOP = asyncio.new_event_loop()

# 独立 engine，不碰全局单例（poc 测试依赖 lifespan 的 dispose_engine，避免互相干扰）
pytestmark = pytest.mark.usefixtures("isolated_postgres_database")

_ENGINE = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
_SESSION_FACTORY = async_sessionmaker(_ENGINE, expire_on_commit=False)
atexit.register(lambda: _LOOP.run_until_complete(_ENGINE.dispose()))

from backend.app.desktop.collab import AgentCollab
from backend.app.desktop.models import (
    AgentBoardTask,
    AgentMessage,
    DesktopThread,
    DesktopWorkspace,
)


def _runtime(agent_id: str, task_id: str) -> ToolRuntime:
    """构造带协作上下文的 ToolRuntime（工具直接 ainvoke 时注入 runtime 字段）。"""
    return ToolRuntime(
        state={}, context={"agent_id": agent_id, "task_id": task_id},
        config={}, stream_writer=None, tool_call_id=None, store=None, tools=[],
    )


async def _seed(collab: AgentCollab) -> str:
    task_id = f"collab-{uuid.uuid4().hex[:16]}"
    workspace_id = f"ws-{uuid.uuid4().hex[:16]}"
    async with collab.session_factory() as session:
        session.add(
            DesktopWorkspace(workspace_id=workspace_id, path=f"/tmp/{workspace_id}", display_name="collab-test")
        )
        await session.flush()  # 无 ORM relationship 时不按外键排序，先落 workspace 再插 thread
        session.add(
            DesktopThread(
                task_id=task_id, workspace_id=workspace_id,
                thread_id=f"th-{uuid.uuid4().hex[:12]}", title="collab",
            )
        )
        await session.commit()
    return task_id


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
        await session.execute(delete(AgentBoardTask).where(AgentBoardTask.thread_task_id == task_id))
        await session.execute(delete(DesktopThread).where(DesktopThread.task_id == task_id))
        await session.execute(
            delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id.in_(workspace_ids))
        )
        await session.commit()


def test_mailbox_send_and_turn_consume():
    async def run() -> None:
        collab = AgentCollab(_SESSION_FACTORY)
        task_id = await _seed(collab)
        try:
            send = collab.build_send_message_tool()
            main = _runtime(f"main:{task_id}", task_id)
            patrol = _runtime("patrol-x", task_id)

            result = await send.ainvoke(
                {"to_agent": "patrol-x", "content": "补充要求：报告需含测试覆盖率", "runtime": main}
            )
            assert "已发送" in result

            block = await collab.load_unread_messages("patrol-x", task_id)
            assert "<agent_messages>" in block
            assert f'from="main:{task_id}"' in block
            assert "补充要求" in block
            assert "at=" in block

            # 消费即销毁：再次注入为空，且消息已标记已读
            assert await collab.load_unread_messages("patrol-x", task_id) == ""
            assert await collab.load_unread_messages(f"main:{task_id}", task_id) == ""

            # 非目标 agent 不受影响（消息只投递给 to_agent）
            result = await send.ainvoke(
                {"to_agent": f"main:{task_id}", "content": "小兵汇报：完成", "runtime": patrol}
            )
            assert "已发送" in result
            assert f'from="patrol-x"' in await collab.load_unread_messages(f"main:{task_id}", task_id)
            # XML 转义：内容含 < 时不破坏注入块
            await send.ainvoke(
                {"to_agent": "patrol-x", "content": "注意 <important> 标签", "runtime": main}
            )
            block = await collab.load_unread_messages("patrol-x", task_id)
            assert "&lt;important&gt;" in block
        finally:
            await _cleanup(collab, task_id)

    _LOOP.run_until_complete(run())


def test_board_cas_protocol():
    async def run() -> None:
        collab = AgentCollab(_SESSION_FACTORY)
        task_id = await _seed(collab)
        try:
            publish = collab.build_publish_task_tool()
            list_tool = collab.build_list_board_tasks_tool()
            claim = collab.build_claim_task_tool()
            complete = collab.build_complete_task_tool()
            main = _runtime(f"main:{task_id}", task_id)
            worker = _runtime("patrol-1", task_id)
            other = _runtime("patrol-2", task_id)

            out = await publish.ainvoke(
                {"description": "统计 README 中的库名", "runtime": main}
            )
            board_id = out.split(": ")[-1]

            # 跳过 claim 直接 complete → 失败
            result = await complete.ainvoke(
                {"board_task_id": board_id, "result": "x", "runtime": worker}
            )
            assert "失败" in result

            # worker 认领成功
            result = await claim.ainvoke({"board_task_id": board_id, "runtime": worker})
            assert "成功" in result

            # 他人认领 → 失败（CAS）
            result = await claim.ainvoke({"board_task_id": board_id, "runtime": other})
            assert "失败" in result

            # 他人 complete → 失败（非认领者）
            result = await complete.ainvoke(
                {"board_task_id": board_id, "result": "x", "runtime": other}
            )
            assert "失败" in result

            # 认领者 complete 成功
            result = await complete.ainvoke(
                {"board_task_id": board_id, "result": "共 12 个库，见 out.md", "runtime": worker}
            )
            assert "已提交" in result

            # 已完成任务再 complete → 失败
            result = await complete.ainvoke(
                {"board_task_id": board_id, "result": "x", "runtime": worker}
            )
            assert "失败" in result

            # 看板展示状态机完整
            board = await list_tool.ainvoke({"runtime": main})
            assert '"status": "completed"' in board
            assert '"claimed_by": "patrol-1"' in board
        finally:
            await _cleanup(collab, task_id)

    _LOOP.run_until_complete(run())
