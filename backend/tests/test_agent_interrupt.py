"""主 Agent 中断（cancel）与中断后继续对话的端到端回归。

仅替换装配工厂为可控图（首轮慢图模拟长运行，后续正常图），其余
（RunManager/worker/StreamBridge/SSE/checkpoint/attach sync/cancel 链路）
全部真实执行。
"""

import asyncio
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from sqlalchemy import delete

os.environ.setdefault("OPENAI_API_KEY", "desktop-test")
pytestmark = pytest.mark.usefixtures("isolated_postgres_database")

from backend.app.gateway.app import app  # noqa: E402
from backend.app.desktop.models import DesktopThread, DesktopWorkspace  # noqa: E402
from backend.app.desktop.service import DesktopService  # noqa: E402

SESSION = {"X-Focus-Session": "focus-dev-session"}


def _client():
    # 决策 10：桌面 API 只接受 loopback 对等连接，测试显式模拟 loopback 来源
    return TestClient(app, client=("127.0.0.1", 50000))


async def _cleanup(service: DesktopService, task_id: str, workspace_id: str, thread_id: str):
    await service.checkpointer.adelete_thread(thread_id)
    await service.checkpointer.adelete_thread(f"{thread_id}:commitment")
    async with service.session_factory() as session:
        await session.execute(delete(DesktopThread).where(DesktopThread.task_id == task_id))
        await session.execute(
            delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace_id)
        )
        await session.commit()


def _build_graph(slow: bool):
    async def slow_reply(_state):
        await asyncio.sleep(30)  # 模拟长运行中的主 Agent

    async def normal_reply(state):
        return {"messages": [AIMessage(content="replied-after-interrupt")]}

    graph = StateGraph(MessagesState)
    graph.add_node("reply", slow_reply if slow else normal_reply)
    graph.add_edge(START, "reply")
    graph.add_edge("reply", END)
    return graph.compile()


def test_interrupted_main_run_resumes_conversation_from_checkpoint(tmp_path, wait_for_memory_status, wait_until):
    """中断主 Agent 运行（保留 checkpoint）后，同一 thread 继续对话历史不丢失。"""
    import backend.app.desktop.service as svc

    calls = {"n": 0}

    async def fake_make_lead_agent(**kwargs):
        calls["n"] += 1
        return _build_graph(slow=calls["n"] == 1)  # 首轮慢图（被中断），后续正常图

    original = svc.make_lead_agent
    svc.make_lead_agent = fake_make_lead_agent
    thread_id = f"desktop-interrupt-e2e-{uuid.uuid4().hex}"
    try:
        with _client() as client:
            workspace_folder = tmp_path / "workspace"
            workspace_folder.mkdir()
            workspace = client.post(
                "/desktop/api/workspaces", headers=SESSION,
                json={"path": str(workspace_folder)},
            ).json()
            task = client.post(
                f"/desktop/api/workspaces/{workspace['workspace_id']}/threads",
                headers=SESSION, json={"thread_id": thread_id, "title": "interrupt e2e"},
            ).json()
            service = app.state.desktop_service

            # 第一轮：慢图运行中（内存状态 running）→ 中断
            run1 = client.post(
                f"/desktop/api/tasks/{task['task_id']}/main/runs",
                headers=SESSION,
                json={"message": "第一轮任务", "permissions": ["read"]},
            ).json()
            wait_for_memory_status(client, service, run1["run_id"], {"running"})
            # 等待首轮输入写入 checkpoint（pregel 首个 superstep 完成后），中断才有现场可保留
            messages_after: list = []

            def _checkpoint_written() -> bool:
                nonlocal messages_after
                messages_after = client.portal.call(
                    service.get_checkpoint_messages, thread_id, ""
                )
                return bool(messages_after)

            wait_until(
                _checkpoint_written,
                timeout=10, interval=0.1, message="首轮输入未写入 checkpoint",
            )
            cancelled = client.post(
                f"/desktop/api/runs/{run1['run_id']}/cancel", headers=SESSION
            ).json()
            assert cancelled["status"] == "interrupted"
            wait_for_memory_status(client, service, run1["run_id"], {"interrupted"})

            # 中断后的 checkpoint 保留第一轮 HumanMessage
            assert [m["role"] for m in messages_after] == ["human"]
            assert messages_after[0]["content"] == "第一轮任务"

            # 第二轮：同一 thread 继续对话 → 历史从 checkpoint 自动恢复
            run2 = client.post(
                f"/desktop/api/tasks/{task['task_id']}/main/runs",
                headers=SESSION,
                json={"message": "继续第二轮", "permissions": ["read"]},
            ).json()
            wait_for_memory_status(client, service, run2["run_id"], {"success"})

            messages_final = client.portal.call(
                service.get_checkpoint_messages, thread_id, ""
            )
            assert [m["role"] for m in messages_final] == ["human", "human", "ai"]
            assert messages_final[0]["content"] == "第一轮任务"
            assert messages_final[1]["content"] == "继续第二轮"
            assert messages_final[2]["content"] == "replied-after-interrupt"

            client.portal.call(
                _cleanup, service, task["task_id"], workspace["workspace_id"], thread_id
            )
    finally:
        svc.make_lead_agent = original
