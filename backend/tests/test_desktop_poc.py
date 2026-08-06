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
from sqlalchemy import delete, func, select

from focus.runtime.checkpointer.namespaced import NamespacedCheckpointer
from focus.runtime.runs.events import (
    build_envelope,
    deserialize_messages,
    stream_text,
    validate_messages,
)
from focus.tools.builtins.workspace_tools import select_workspace_tools


os.environ.setdefault("OPENAI_API_KEY", "desktop-test")
os.environ.setdefault(
    "FOCUS_DATABASE_URL",
    "postgresql+asyncpg://focus:qweasdzxc123@127.0.0.1:7221/focus",
)

# 决策 1：桌面功能内嵌 Gateway，测试目标为唯一 FastAPI 应用
from backend.app.gateway.app import app  # noqa: E402
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace  # noqa: E402
from backend.app.desktop.service import (  # noqa: E402
    DesktopService,
    estimate_tokens,
    prompt_with_skills,
)
from backend.app.gateway.routers.thread_runs import sse_consumer  # noqa: E402


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


async def _run_count(service: DesktopService, task_id: str) -> int:
    async with service.session_factory() as session:
        return await session.scalar(
            select(func.count()).select_from(DesktopRun).where(DesktopRun.task_id == task_id)
        )


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


def test_unified_pipeline_main_run_end_to_end(tmp_path):
    """端到端：真实 HTTP → start_run → worker.run_agent（统一事件契约）→ DB 终态同步。

    仅替换装配工厂为 FakeListChatModel 图，其余（RunManager/worker/StreamBridge/SSE/
    checkpoint/attach sync）全部真实执行。
    """
    import backend.app.desktop.service as svc

    async def fake_make_lead_agent(**kwargs):
        return create_agent(model=FakeListChatModel(responses=["你好"]), tools=[])

    original = svc.make_lead_agent
    svc.make_lead_agent = fake_make_lead_agent
    thread_id = f"desktop-e2e-{uuid.uuid4().hex}"
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
                headers=SESSION, json={"thread_id": thread_id, "title": "e2e"},
            ).json()
            service = app.state.desktop_service
            before = client.portal.call(_run_count, service, task["task_id"])

            run = client.post(
                f"/desktop/api/tasks/{task['task_id']}/main/runs",
                headers=SESSION,
                json={"message": "你好", "permissions": ["read"]},
            ).json()
            assert run["status"] == "pending"
            assert client.portal.call(_run_count, service, task["task_id"]) == before + 1

            # 订阅统一事件流，收集 tokens/events/end
            async def collect() -> list[str]:
                events = []
                async for event in service.bridge.subscribe(run["run_id"]):
                    from focus.runtime.stream_bridge.schemas import END_SENTINEL

                    if event is END_SENTINEL:
                        break
                    events.append(event.event)
                    if len(events) > 50:
                        break
                return events

            # worker 完成后 DB 终态应为 success（attach_run_sync 同步）
            deadline = time.monotonic() + 20
            status = None
            while time.monotonic() < deadline:
                current = client.get(
                    f"/desktop/api/runs/{run['run_id']}", headers=SESSION
                ).json()
                status = current["status"]
                if status == "success":
                    break
                time.sleep(0.2)
            assert status == "success"

            names = client.portal.call(collect)
            assert "tokens" in names and "events" in names

            messages = client.portal.call(
                service.get_checkpoint_messages, thread_id, ""
            )
            assert messages[-1]["role"] == "ai"
            assert "你好" in messages[-1]["content"]

            # 清理
            client.portal.call(
                _cleanup, service, task["task_id"], workspace["workspace_id"], thread_id
            )
    finally:
        svc.make_lead_agent = original


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

        skills_root = workspace_folder / ".agents" / "skills"
        for name in ("one", "two"):
            skill_file = skills_root / name / "SKILL.md"
            skill_file.parent.mkdir(parents=True, exist_ok=True)
            skill_file.write_text(
                f"---\nname: {name}\ndescription: Skill {name}\n---\n\nOriginal {name} instructions.\n",
                encoding="utf-8",
            )
        catalog = client.get(
            f"/desktop/api/tasks/{task['task_id']}/skills", headers=SESSION
        ).json()
        assert [skill["name"] for skill in catalog["skills"]] == ["one", "two"]

        runs_before = client.portal.call(_run_count, service, task["task_id"])
        unavailable_main = client.post(
            f"/desktop/api/tasks/{task['task_id']}/main/runs",
            headers=SESSION,
            json={"message": "run", "skills": ["missing"]},
        )
        assert unavailable_main.status_code == 422
        assert unavailable_main.json()["detail"] == {
            "code": "skill_unavailable", "names": ["missing"]
        }
        assert client.portal.call(_run_count, service, task["task_id"]) == runs_before

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

        stale = client.put(
            f"/desktop/api/drafts/{draft['draft_id']}",
            headers=SESSION,
            json={
                "history_messages": draft["history_messages"],
                "final_human_message": "run",
                "equipment": {"skills": ["missing"], "permissions": ["read"]},
            },
        )
        assert stale.status_code == 200
        runs_before = client.portal.call(_run_count, service, task["task_id"])
        unavailable_patrol = client.post(
            f"/desktop/api/drafts/{draft['draft_id']}/deploy",
            headers=SESSION,
            json={"deployment_id": uuid.uuid4().hex},
        )
        assert unavailable_patrol.status_code == 422
        assert unavailable_patrol.json()["detail"] == {
            "code": "skill_unavailable", "names": ["missing"]
        }
        assert client.portal.call(_run_count, service, task["task_id"]) == runs_before

        updated = client.put(
            f"/desktop/api/drafts/{draft['draft_id']}",
            headers=SESSION,
            json={
                "system_prompt": "只返回结论",
                "history_messages": draft["history_messages"],
                "final_human_message": "检查材料",
                "equipment": {"model_name": "deepseek-v4-flash", "tools": "auto", "skills": ["one", "two", "one"], "permissions": ["read"]},
            },
        ).json()
        assert updated["source_checkpoint_id"] == draft["source_checkpoint_id"]
        assert updated["equipment"]["skills"] == ["one", "two"]
        assert updated["token_estimate"] > estimate_tokens(
            updated["system_prompt"], updated["history_messages"], updated["final_human_message"]
        )
        oversized_with_skill = estimate_tokens(
            prompt_with_skills("", [{"name": "huge", "content": "界" * 131100}]), [], ""
        )
        try:
            service._validate_model_window("deepseek-v4-flash", oversized_with_skill)
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 422
        else:
            raise AssertionError("deployment above the declared context window must be blocked")

        launched = []

        async def fake_start_run(body, thread_id, request, agent_factory=None):
            launched.append((body, thread_id, agent_factory))
            future = asyncio.Future()
            future.set_result(None)
            return SimpleNamespace(
                run_id=uuid.uuid4().hex, thread_id=thread_id,
                status=SimpleNamespace(value="pending"), task=future,
            )

        import backend.app.desktop.routes as desktop_routes

        desktop_routes.start_run = fake_start_run
        main = client.post(
            f"/desktop/api/tasks/{task['task_id']}/main/runs",
            headers=SESSION,
            json={"message": "run", "skills": ["two", "one", "two"]},
        )
        assert main.status_code == 200
        main_body, main_thread, main_factory = launched[-1]
        assert main_thread == thread_id
        assert main_body.context["skills"] == ["two", "one"]
        assert main_body.context["checkpoint_ns"] == ""
        assert main_body.context["agent_id"] == f"main:{task['task_id']}"
        assert main_body.context["workspace"] == str(workspace_folder)
        assert main_body.stream_mode == ["messages-tuple", "values"]
        assert len(main_body.input["messages"]) == 1
        assert main_factory is not None

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
        assert len(launched) == 2  # 幂等部署不重复发起
        patrol_body, patrol_thread, patrol_factory = launched[-1]
        assert patrol_body.context["checkpoint_ns"] == f"patrol:{first['agent_id']}"
        assert patrol_body.context["agent_id"] == first["agent_id"]
        assert patrol_body.input["messages"][-1]["role"] == "human"
        envelope = build_envelope(
            workspace["workspace_id"], thread_id, first["agent_id"], first["run_id"], "events", {"ok": True},
        )
        assert set(envelope) == {"workspace_id", "thread_id", "agent_id", "run_id", "event", "data"}
        agent = client.get(
            f"/desktop/api/tasks/{task['task_id']}/agents", headers=SESSION
        ).json()[0]
        assert agent["checkpoint_ns"] == f"patrol:{agent['agent_id']}"

        (skills_root / "one" / "SKILL.md").write_text(
            "---\nname: one\ndescription: changed\n---\n\nChanged instructions.\n",
            encoding="utf-8",
        )
        retried = client.post(
            f"/desktop/api/agents/{agent['agent_id']}/retry", headers=SESSION
        ).json()
        continued = client.post(
            f"/desktop/api/agents/{agent['agent_id']}/continue",
            headers=SESSION,
            json={"message": "继续"},
        ).json()
        assert retried["agent_id"] == continued["agent_id"] == agent["agent_id"]
        assert launched[-2][0].context["checkpoint_ns"] == f"patrol:{agent['agent_id']}"
        assert launched[-1][0].context["checkpoint_ns"] == f"patrol:{agent['agent_id']}"
        assert client.post(
            f"/desktop/api/runs/{continued['run_id']}/cancel", headers=SESSION
        ).json()["status"] == "interrupted"

        assert {t.name for t in select_workspace_tools(["read"])} == {"read_file", "list_files"}
        assert select_workspace_tools([]) == []
        assert {t.name for t in select_workspace_tools(["read", "write", "host_command"])} == {
            "read_file", "list_files", "write_file", "bash", "powershell", "cmd", "sh"
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
