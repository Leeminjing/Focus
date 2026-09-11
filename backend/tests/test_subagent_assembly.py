"""Subagent 三种模式装配集成测试：主 Agent/小兵按角色装配协作工具 + Mailbox 回合注入。

真实链路（start_run/worker/StreamBridge/DB），仅替换 make_lead_agent 为捕获型 fake 图。
独立文件：poc 文件内的嵌套 TestClient lifespan 会污染 app.state，隔离避免顺序依赖。
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import delete

os.environ.setdefault("OPENAI_API_KEY", "desktop-test")
pytestmark = pytest.mark.usefixtures("isolated_postgres_database")

from backend.app.gateway.app import app  # noqa: E402
from backend.app.desktop.models import (  # noqa: E402
    AgentMessage,
    DesktopThread,
    DesktopWorkspace,
    SwarmAgent,
)
from backend.app.desktop.service import DesktopService  # noqa: E402


SESSION = {"X-Focus-Session": "focus-dev-session"}


def _client():
    # 决策 10：桌面 API 只接受 loopback 对等连接，测试显式模拟 loopback 来源
    return TestClient(app, client=("127.0.0.1", 50000))


async def _cleanup(service: DesktopService, task_id: str, workspace_id: str, thread_id: str):
    await service.checkpointer.adelete_thread(thread_id)
    async with service.session_factory() as session:
        await session.execute(delete(DesktopThread).where(DesktopThread.task_id == task_id))
        await session.execute(
            delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace_id)
        )
        await session.commit()


def test_subagent_role_assembly_and_mailbox_injection(tmp_path, wait_until):
    """主 Agent/小兵按角色装配协作工具 + Mailbox 回合注入（fake 装配捕获，真实链路）。

    仅替换 make_lead_agent 为捕获型 fake 图，其余（start_run/worker/StreamBridge/DB）全真实。
    """
    import backend.app.desktop.service as svc

    captured: list[dict] = []

    class FakeGraph:
        async def astream(self, graph_input, config=None, context=None, stream_mode=None):
            yield "values", {"messages": [AIMessage(content="完成")]}

    async def fake_make_lead_agent(**kwargs):
        captured.append(kwargs)
        return FakeGraph()

    original = svc.make_lead_agent
    svc.make_lead_agent = fake_make_lead_agent
    try:
        with _client() as client:
            workspace_folder = tmp_path / "ws"
            workspace_folder.mkdir()
            ws = client.post(
                "/desktop/api/workspaces", headers=SESSION, json={"path": str(workspace_folder)}
            ).json()
            task = client.post(
                f"/desktop/api/workspaces/{ws['workspace_id']}/threads",
                headers=SESSION, json={"title": "t"},
            ).json()
            service = app.state.desktop_service
            task_id = task["task_id"]

            # 给小兵与主 Agent 各塞一条未读消息（主 Agent 链路的注入断言）
            async def seed_mailbox() -> None:
                async with service.session_factory() as session:
                    session.add(AgentMessage(
                        message_id=uuid.uuid4().hex, task_id=task_id,
                        from_agent="patrol-x", to_agent=f"main:{task_id}",
                        kind="message", content="汇报：工作完成",
                    ))
                    await session.commit()

            client.portal.call(seed_mailbox)

            run = client.post(
                f"/desktop/api/tasks/{task_id}/main/runs", headers=SESSION,
                json={"message": "继续", "permissions": ["read"]},
            ).json()
            assert run["status"] == "pending"
            wait_until(lambda: captured, timeout=15, message="主 Agent 装配未被捕获")
            main_cfg = captured[-1]
            main_names = {tool.name for tool in main_cfg["tools"]}
            assert {"spawn_agent", "spawn_teammate", "spawn_worker", "wake_agent",
                    "wait_for_swarm",
                    "publish_task", "list_board_tasks", "send_message",
                    "approve_plan", "respond_shutdown",
                    "list_patrol_agents", "read_patrol_agent_history",
                    "read_swarm_agent_history"} <= main_names
            assert main_cfg["middlewares"] is None
            # 主 Agent：工具错误 middleware + 必需图片注入 + 压缩门（均为仅 main 装配）
            assert len(main_cfg["additional_middlewares"]) == 3
            names = [
                middleware.__class__.__name__
                for middleware in main_cfg["additional_middlewares"]
            ]
            assert "MustViewImagesMiddleware" in names
            assert "CompressionGate" in names
            assert names.index("MustViewImagesMiddleware") < names.index("CompressionGate")
            assert "claim_task" not in main_names
            assert "<agent_messages>" in main_cfg["system_prompt"]
            assert 'from="patrol-x"' in main_cfg["system_prompt"]

            # 小兵链路：open_draft → update → deploy
            captured.clear()
            draft = client.post(f"/desktop/api/tasks/{task_id}/drafts/open", headers=SESSION).json()
            draft = client.put(f"/desktop/api/drafts/{draft['draft_id']}", headers=SESSION, json={
                "system_prompt": "小兵提示",
                "history_messages": [],
                "final_human_message": "执行任务",
                "equipment": {"model_name": None, "skills": [], "permissions": ["read"]},
            }).json()
            run2 = client.post(
                f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers=SESSION,
                json={"deployment_id": uuid.uuid4().hex},
            ).json()
            assert run2["status"] == "pending"
            wait_until(lambda: captured, timeout=15, message="小兵装配未被捕获")
            patrol_cfg = captured[-1]
            patrol_names = {tool.name for tool in patrol_cfg["tools"]}
            # 小兵机制纯净：仅工作区工具，无任何协作工具与消息注入
            assert patrol_names <= {"read_file", "list_files", "write_file",
                                    "bash", "powershell", "cmd", "sh"}
            assert "send_message" not in patrol_names
            assert "spawn_agent" not in patrol_names
            assert "claim_task" not in patrol_names
            assert "<agent_messages>" not in patrol_cfg["system_prompt"]
            assert patrol_cfg["middlewares"] == []
            assert len(patrol_cfg["additional_middlewares"]) == 1

            # 机制③④：teammate / worker 装配（直接走装配矩阵，fake 捕获工具集与注入）
            captured.clear()
            equipment = {"model_name": None, "skills": [],
                         "skill_snapshots": [], "permissions": ["read"]}

            async def inspect_roles() -> list[set[str]]:
                teammate_factory = service._build_agent_factory(
                    task_id, "swarm-t", str(workspace_folder), equipment, "协作提示", "", "teammate"
                )
                await teammate_factory()
                worker_factory = service._build_agent_factory(
                    task_id, "swarm-w", str(workspace_folder), equipment, "协作提示", "", "worker"
                )
                await worker_factory()
                return [{tool.name for tool in cfg["tools"]} for cfg in captured]

            role_names = client.portal.call(inspect_roles)
            teammate_names, worker_names = role_names
            assert {"send_message", "request_plan_approval", "request_shutdown"} <= teammate_names
            assert "approve_plan" not in teammate_names and "publish_task" not in teammate_names
            assert {"send_message", "claim_task", "complete_task"} <= worker_names
            assert "approve_plan" not in worker_names and "request_shutdown" not in worker_names
            # teammate/worker 有 Mailbox 注入；小兵（上一步）无注入
            assert "协作提示" in captured[0]["system_prompt"]

            # wake 唤醒：常驻 Agent 多轮闭环（权限沿用不放大、消息直接进输入不落 mailbox）
            captured.clear()
            wake_agent_id = uuid.uuid4().hex

            async def inspect_wake() -> str:
                await service.agent_collab.create_swarm_agent(wake_agent_id, task_id, "teammate", ["read"])
                return await service._wake_swarm(wake_agent_id, "继续调研竞品定价", wake_ctx)

            wake_ctx = {
                "workspace_id": ws["workspace_id"], "workspace": str(workspace_folder),
                "model_name": None, "task_id": task_id,
                "agent_id": f"main:{task_id}", "permissions": ["read"],
            }
            wake_run_id = client.portal.call(inspect_wake)
            wait_until(lambda: captured, timeout=15, message="wake 装配未被捕获")
            assert wake_run_id
            wake_cfg = captured[-1]
            wake_names = {tool.name for tool in wake_cfg["tools"]}
            # teammate 工具集 + 权限沿用 ["read"]（无 write_file，不放大）
            assert {"send_message", "request_plan_approval", "request_shutdown"} <= wake_names
            assert "write_file" not in wake_names
            # 消息直接进输入，不落 mailbox（避免回合注入重复带入）
            async def check_no_mailbox_copy() -> bool:
                from sqlalchemy import func, select
                from backend.app.desktop.models import AgentMessage

                async with service.session_factory() as session:
                    count = await session.scalar(
                        select(func.count()).select_from(AgentMessage)
                        .where(AgentMessage.to_agent == wake_agent_id, AgentMessage.content.like("%竞品定价%"))
                    )
                return count == 0

            assert client.portal.call(check_no_mailbox_copy) is True

            # 已停止 Agent 唤醒 → 409
            async def stop_and_wake() -> Exception | None:
                await service.agent_collab._stop_swarm_agent(wake_agent_id)
                try:
                    await service._wake_swarm(wake_agent_id, "再干一轮", wake_ctx)
                except Exception as exc:
                    return exc
                return None

            result = client.portal.call(stop_and_wake)
            assert result is not None and result.status_code == 409

            # 消息驱动自动唤醒（service 层 launcher）：触发 / busy 跳过 / stopped 跳过
            captured.clear()
            auto_id = uuid.uuid4().hex

            async def inspect_auto_wake() -> None:
                await service.agent_collab.create_swarm_agent(auto_id, task_id, "teammate", ["read"])
                await service._auto_wake_swarm(auto_id, "自动唤醒第一轮", 1)
                await service._auto_wake_swarm(auto_id, "自动唤醒第二轮", 2)  # 目标忙（上轮 pending）→ 跳过

            client.portal.call(inspect_auto_wake)
            wait_until(lambda: captured, timeout=15, message="自动唤醒装配未被捕获")
            assert len(captured) == 1, "busy 跳过未生效（不应有第二次装配）"
            auto_names = {tool.name for tool in captured[-1]["tools"]}
            assert {"send_message", "request_plan_approval", "request_shutdown"} <= auto_names
            assert "write_file" not in auto_names  # 权限沿用 ["read"]

            # stopped 后自动唤醒跳过（不触发）
            async def stop_then_auto() -> None:
                await service.agent_collab._stop_swarm_agent(auto_id)
                captured.clear()
                await service._auto_wake_swarm(auto_id, "停止后触发", 1)

            client.portal.call(stop_then_auto)
            assert captured == []

            def cleanup() -> None:
                async def do_cleanup() -> None:
                    async with service.session_factory() as session:
                        await session.execute(delete(AgentMessage).where(AgentMessage.task_id == task_id))
                        await session.execute(delete(SwarmAgent).where(SwarmAgent.agent_id == wake_agent_id))

                client.portal.call(do_cleanup)
                client.portal.call(
                    _cleanup, service, task_id, ws["workspace_id"], task["thread_id"]
                )

            cleanup()
    finally:
        svc.make_lead_agent = original
