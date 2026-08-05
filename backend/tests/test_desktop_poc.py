import asyncio
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
import uuid

from fastapi.testclient import TestClient
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from sqlalchemy import delete


os.environ.setdefault("OPENAI_API_KEY", "desktop-test")
os.environ.setdefault("JWT_SECRET", "desktop-test")
os.environ.setdefault(
    "FOCUS_DATABASE_URL",
    "postgresql+asyncpg://focus:qweasdzxc123@127.0.0.1:7221/focus",
)

# 决策 1：桌面功能内嵌 Gateway，测试目标为唯一 FastAPI 应用
from backend.app.gateway.app import app  # noqa: E402
from backend.app.desktop.models import DesktopThread, DesktopWorkspace  # noqa: E402
from backend.app.desktop.service import (  # noqa: E402
    DesktopService,
    NamespacedCheckpointer,
    deserialize_messages,
    estimate_tokens,
    stream_text,
    validate_messages,
)


SESSION = {"X-Focus-Session": "focus-dev-session"}


def _client():
    # 决策 10：桌面 API 只接受 loopback 对等连接，测试显式模拟 loopback 来源
    return TestClient(app, client=("127.0.0.1", 50000))


def test_langgraph_messages_mode_streams_incrementally():
    async def collect() -> list[str]:
        graph = create_agent(model=FakeListChatModel(responses=["stream"]), tools=[])
        chunks = []
        async for mode, chunk in graph.astream(
            {"messages": [{"role": "user", "content": "go"}]},
            stream_mode=["messages", "values"],
        ):
            if mode == "messages":
                chunks.append(stream_text(chunk[0].content))
        return chunks

    assert asyncio.run(collect()) == list("stream")


def _git(folder: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(folder), *args], capture_output=True, text=True, check=True
    )
    return result.stdout


async def _seed_checkpoint(service: DesktopService, thread_id: str, namespace: str, text: str):
    graph = StateGraph(MessagesState)

    async def reply(_state):
        return {"messages": [AIMessage(content=f"ack:{text}")]}

    graph.add_node("reply", reply)
    graph.add_edge(START, "reply")
    graph.add_edge("reply", END)
    compiled = graph.compile(
        checkpointer=NamespacedCheckpointer(service.checkpointer, namespace)
    )
    await compiled.ainvoke(
        {"messages": [HumanMessage(content=text)]},
        {"configurable": {"thread_id": thread_id}},
    )
    return await service.get_checkpoint_messages(thread_id, namespace)


async def _cleanup(service: DesktopService, task_id: str, workspace_id: str, thread_id: str):
    await service.checkpointer.adelete_thread(thread_id)
    async with service.session_factory() as session:
        await session.execute(delete(DesktopThread).where(DesktopThread.task_id == task_id))
        await session.execute(
            delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace_id)
        )
        await session.commit()


def test_message_validation_token_estimate_and_path_policy(tmp_path):
    valid = [
        {
            "role": "ai",
            "content": "",
            "tool_calls": [{"id": "call-1", "name": "read_file", "args": {}}],
        },
        {"role": "tool", "content": "ok", "tool_call_id": "call-1"},
    ]
    validate_messages(valid)
    assert len(deserialize_messages(valid)) == 2
    assert estimate_tokens("系统", valid, "任务") > 0
    assert stream_text("逐字") == "逐字"
    assert stream_text([{"type": "text", "text": "流"}, {"text": "式"}]) == "流式"
    assert stream_text([{"type": "reasoning", "reasoning": "hidden"}]) == ""

    try:
        validate_messages(valid[:1])
    except ValueError as exc:
        assert "缺少结果" in str(exc)
    else:
        raise AssertionError("unpaired tool calls must be rejected")

    try:
        validate_messages([valid[0], {"role": "human", "content": "interleaved"}, valid[1]])
    except ValueError as exc:
        assert "必须紧跟" in str(exc)
    else:
        raise AssertionError("tool results must remain adjacent to their AI call")

    inside = DesktopService._resolve_workspace_path(tmp_path, "child.txt")
    assert inside == tmp_path / "child.txt"
    try:
        DesktopService._resolve_workspace_path(tmp_path, "../outside.txt")
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 403
    else:
        raise AssertionError("paths outside the real workspace must be rejected")


def test_postgres_draft_runtime_namespace_and_materials(tmp_path):
    workspace_folder = tmp_path / "workspace"
    workspace_folder.mkdir()
    material_file = workspace_folder / "material.txt"
    material_file.write_text("version one", encoding="utf-8")
    thread_id = f"desktop-test-{uuid.uuid4().hex}"

    with _client() as client:
        assert client.get("/desktop/api/bootstrap").status_code == 401
        workspace = client.post(
            "/desktop/api/workspaces",
            headers=SESSION,
            json={"path": str(workspace_folder)},
        ).json()
        task = client.post(
            f"/desktop/api/workspaces/{workspace['workspace_id']}/threads",
            headers=SESSION,
            json={"thread_id": thread_id, "title": "integration"},
        ).json()
        service = app.state.desktop_service

        main_history = client.portal.call(
            _seed_checkpoint, service, thread_id, "", "main checkpoint"
        )
        patrol_history = client.portal.call(
            _seed_checkpoint, service, thread_id, "patrol:isolation", "patrol checkpoint"
        )
        assert main_history[-1]["content"] == "ack:main checkpoint"
        assert patrol_history[-1]["content"] == "ack:patrol checkpoint"

        draft = client.post(
            f"/desktop/api/tasks/{task['task_id']}/drafts/open", headers=SESSION
        ).json()
        frozen_count = len(draft["history_messages"])
        client.portal.call(_seed_checkpoint, service, thread_id, "", "later main message")
        reopened = client.post(
            f"/desktop/api/tasks/{task['task_id']}/drafts/open", headers=SESSION
        ).json()
        assert len(reopened["history_messages"]) == frozen_count

        invalid = client.put(
            f"/desktop/api/drafts/{draft['draft_id']}",
            headers=SESSION,
            json={
                "history_messages": [{
                    "role": "ai", "content": "", "tool_calls": [{"id": "missing", "name": "x", "args": {}}]
                }],
                "final_human_message": "run",
                "equipment": {"permissions": ["read"]},
            },
        )
        assert invalid.status_code == 422

        updated = client.put(
            f"/desktop/api/drafts/{draft['draft_id']}",
            headers=SESSION,
            json={
                "system_prompt": "只返回结论",
                "history_messages": draft["history_messages"],
                "final_human_message": "检查材料",
                "equipment": {"model_name": "deepseek-v4-flash", "tools": "auto", "skills": "auto", "permissions": ["read"]},
            },
        ).json()
        assert updated["source_checkpoint_id"] == draft["source_checkpoint_id"]
        try:
            service._validate_model_window("deepseek-v4-flash", 131073)
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 422
        else:
            raise AssertionError("deployment above the declared context window must be blocked")

        async def no_run(*_args, **_kwargs):
            return None

        service._schedule_run = no_run
        deployment_id = uuid.uuid4().hex
        first = client.post(
            f"/desktop/api/drafts/{draft['draft_id']}/deploy",
            headers=SESSION,
            json={"deployment_id": deployment_id},
        ).json()
        second = client.post(
            f"/desktop/api/drafts/{draft['draft_id']}/deploy",
            headers=SESSION,
            json={"deployment_id": deployment_id},
        ).json()
        assert first["run_id"] == second["run_id"]
        envelope = service._event_envelope(
            SimpleNamespace(run_id=first["run_id"], agent_id=first["agent_id"]),
            SimpleNamespace(workspace_id=workspace["workspace_id"], thread_id=thread_id),
            "events", {"ok": True},
        )
        assert set(envelope) == {"workspace_id", "thread_id", "agent_id", "run_id", "event", "data"}
        agent = client.get(
            f"/desktop/api/tasks/{task['task_id']}/agents", headers=SESSION
        ).json()[0]
        assert agent["checkpoint_ns"] == f"patrol:{agent['agent_id']}"

        retried = client.post(
            f"/desktop/api/agents/{agent['agent_id']}/retry", headers=SESSION
        ).json()
        continued = client.post(
            f"/desktop/api/agents/{agent['agent_id']}/continue",
            headers=SESSION,
            json={"message": "继续"},
        ).json()
        assert retried["agent_id"] == continued["agent_id"] == agent["agent_id"]
        assert client.post(
            f"/desktop/api/runs/{continued['run_id']}/cancel", headers=SESSION
        ).json()["status"] == "interrupted"

        async def tool_names(permissions):
            async with service.session_factory() as session:
                task_row = await session.get(DesktopThread, task["task_id"])
                workspace_row = await session.get(DesktopWorkspace, workspace["workspace_id"])
            tools = await service._build_tools(
                task_row, workspace_row,
                {"tools": "auto", "permissions": permissions}, False,
            )
            return {item.name for item in tools}

        assert client.portal.call(tool_names, ["read"]) == {"read_file", "list_files"}
        assert client.portal.call(tool_names, []) == set()
        assert client.portal.call(tool_names, ["read", "write", "host_command"]) == {
            "read_file", "list_files", "write_file", "powershell"
        }

        _git(workspace_folder, "init")
        status_before = _git(workspace_folder, "status", "--porcelain")
        material = client.post(
            f"/desktop/api/tasks/{task['task_id']}/materials",
            headers=SESSION,
            json={"path": str(material_file), "retention": "irreplaceable", "confirm_git_init": True},
        ).json()
        assert material["path"] == str(material_file.resolve())
        assert _git(workspace_folder, "status", "--porcelain") == status_before
        assert material["git_ref"] in _git(workspace_folder, "show-ref")

        versions = client.get(
            f"/desktop/api/materials/{material['material_id']}/versions", headers=SESSION
        ).json()
        client.post(
            f"/desktop/api/materials/{material['material_id']}/clear", headers=SESSION
        )
        assert material_file.read_bytes() == b""
        client.post(
            f"/desktop/api/materials/{material['material_id']}/restore",
            headers=SESSION,
            json={"version_id": versions[-1]["version_id"]},
        )
        assert material_file.read_text(encoding="utf-8") == "version one"
        assert client.delete(
            f"/desktop/api/materials/{material['material_id']}", headers=SESSION
        ).status_code == 409

        material_file.write_text("external version", encoding="utf-8")
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            current = client.get(
                f"/desktop/api/materials/{material['material_id']}/versions", headers=SESSION
            ).json()
            if len(current) >= len(versions) + 3:
                break
            time.sleep(0.25)
        else:
            raise AssertionError("external material change was not versioned")

        material_file.unlink()
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline and not material_file.exists():
            time.sleep(0.25)
        assert material_file.read_text(encoding="utf-8") == "external version"
        current_material = client.get(
            f"/desktop/api/tasks/{task['task_id']}/materials", headers=SESSION
        ).json()[0]
        assert current_material["needs_confirmation"] is True
        assert _git(workspace_folder, "status", "--porcelain") == status_before

        orphan_run_id = retried["run_id"]

    with _client() as restarted:
        assert restarted.get(
            f"/desktop/api/runs/{orphan_run_id}", headers=SESSION
        ).json()["status"] == "interrupted"
        restarted.portal.call(
            _cleanup, app.state.desktop_service, task["task_id"], workspace["workspace_id"], thread_id
        )
