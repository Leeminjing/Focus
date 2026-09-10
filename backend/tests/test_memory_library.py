"""验证记忆库 CRUD、来源解析、压缩总结与主 Agent 的 `<memory>` 注入。"""

import os
from pathlib import Path
import shutil
import uuid

import pytest
from fastapi.testclient import TestClient
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage

os.environ.setdefault("OPENAI_API_KEY", "memory-test")
pytestmark = pytest.mark.usefixtures("isolated_postgres_database")

from backend.app.desktop.memory import (  # noqa: E402
    _MAIN_RUNTIME_MEMORY_KEY,
    MemoryCreate,
    MemorySelection,
    MemorySource,
    MemorySummarizeRequest,
    MemoryUpdate,
)
from backend.app.desktop.models import DesktopThread  # noqa: E402
from backend.app.gateway.app import app  # noqa: E402

SESSION = {"X-Focus-Session": "focus-dev-session"}


def _client():
    return TestClient(app, client=("127.0.0.1", 50000))


def _make_workspace_folder():
    base = Path(__file__).resolve().parent / ".memory-test"
    base.mkdir(parents=True, exist_ok=True)
    folder = base / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _remove_workspace_folder(folder):
    if folder:
        try:
            shutil.rmtree(folder)
        except OSError:
            pass


def test_memory_crud_and_injection(monkeypatch):
    import backend.app.desktop.memory as memory_module

    class _FakeResponse:
        content = "这是概括后的记忆内容"

    async def fake_ainvoke(_messages, *_args, **_kwargs):
        return _FakeResponse()

    monkeypatch.setattr(
        memory_module,
        "create_chat_model",
        lambda *a, **k: type("M", (), {"ainvoke": fake_ainvoke})(),
    )

    folder = _make_workspace_folder()
    with _client() as client:
        service = app.state.desktop_service
        portal = client.portal
        workspace = client.post(
            "/desktop/api/workspaces", headers=SESSION, json={"path": str(folder)}
        ).json()
        root = client.post(
            f"/desktop/api/workspaces/{workspace['workspace_id']}/threads",
            headers=SESSION,
            json={"thread_id": uuid.uuid4().hex, "title": "记忆源会话"},
        ).json()
        task_id = root["task_id"]
        thread_id = root["thread_id"]

        async def scenario() -> dict:
            # 种子消息到 thread
            graph = create_agent(model=FakeListChatModel(responses=["unused"]), tools=[])
            graph.checkpointer = service.checkpointer
            await graph.aupdate_state(
                {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
                {"messages": [HumanMessage(content="关键技术决策：使用 PostgreSQL 作为记忆库。")]},
            )
            await graph.aupdate_state(
                {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
                {"messages": [HumanMessage(content="第二条事实，用于非连续消息切片。")]},
                as_node="model",
            )
            messages = await service.memory._context_reader(task_id)
            assert messages, "应为种子消息返回非空"
            first_id = messages[0]["id"]

            # 压缩总结（session 来源，complete 默认）
            selection = MemorySelection(
                sources=[MemorySource(type="session", context_id=task_id)]
            )
            summary = await service.memory.summarize(
                MemorySummarizeRequest(selection=selection)
            )
            assert summary["mode"] == "complete"
            assert summary["content"] == "这是概括后的记忆内容"

            # 压缩总结（分段模式）：每个来源各自一段
            seg_result = await service.memory.summarize(
                MemorySummarizeRequest(selection=selection, mode="segmented")
            )
            assert seg_result["mode"] == "segmented"
            assert len(seg_result["segments"]) == 1
            assert seg_result["segments"][0]["body"] == "这是概括后的记忆内容"

            # 创建记忆
            mem = await service.memory.create(MemoryCreate(
                title="关键记忆", content=summary["content"], source_kind="session", source=selection,
            ))
            assert mem["content"] == "这是概括后的记忆内容"

            # CRUD 列表/单条/更新
            assert any(m["memory_id"] == mem["memory_id"] for m in await service.memory.list())
            assert (await service.memory.get(mem["memory_id"]))["title"] == "关键记忆"
            updated = await service.memory.update(
                mem["memory_id"], MemoryUpdate(content="编辑后的最终内容")
            )
            assert updated["content"] == "编辑后的最终内容"

            # 注入块
            block = await service.memory.build_memory_block([mem["memory_id"]])
            assert block.startswith("<memory>")
            assert "编辑后的最终内容" in block
            assert await service.memory.build_memory_block([]) == ""

            # 分段记忆注入块：mode="segmented" 时含多个 <segment>
            seg_mem = await service.memory.create(MemoryCreate(
                title="分段记忆", content="段1\n\n段2", content_mode="segmented",
                segments=[{"title": "段A", "body": "第一段正文"}, {"title": "段B", "body": "第二段正文"}],
                source_kind="partial_messages", source=selection,
            ))
            seg_block = await service.memory.build_memory_block([seg_mem["memory_id"]])
            assert 'mode="segmented"' in seg_block
            assert "<segment" in seg_block
            assert "第一段正文" in seg_block
            assert seg_mem["content_mode"] == "segmented"
            assert len(seg_mem["segments"]) == 2

            # _apply_memory_block 注入缝
            injected = await service._apply_memory_block("BASEPROMPT", [mem["memory_id"]])
            assert injected.startswith("BASEPROMPT") and "<memory>" in injected
            assert await service._apply_memory_block("BASEPROMPT", []) == "BASEPROMPT"

            # start_main_run 持久化 memory_ids → ui_state
            await service.start_main_run(
                task_id, "请开始", None, ["read"], [], None, [mem["memory_id"]],
            )
            async with service.session_factory() as session:
                task = await session.get(DesktopThread, task_id)
                assert (task.ui_state or {}).get(_MAIN_RUNTIME_MEMORY_KEY) == [mem["memory_id"]]

            # 端到端：最终 system_prompt 含 <memory> 且为编辑后文本
            import backend.app.desktop.service as svc

            captured = {}

            async def fake_make_lead_agent(**kwargs):
                captured["system_prompt"] = kwargs.get("system_prompt", "")
                return create_agent(model=FakeListChatModel(responses=["unused"]), tools=[])

            original = svc.make_lead_agent
            svc.make_lead_agent = fake_make_lead_agent
            try:
                prepared2 = await service.start_main_run(
                    task_id, "请再开始", None, ["read"], [], None, [mem["memory_id"]],
                )
                graph = await prepared2.agent_factory()
                assert "<memory>" in captured["system_prompt"]
                assert "编辑后的最终内容" in captured["system_prompt"]
                assert graph is not None
            finally:
                svc.make_lead_agent = original

            # text 来源按 ranges 切片（真实 message_id）
            text_selection = MemorySelection(sources=[MemorySource(
                type="text", context_id=task_id,
                parts=[{"message_id": first_id, "ranges": [{"start": 0, "end": 4}]}],
            )])
            sliced = await service.memory.resolve_selection(text_selection)
            assert "关键" in sliced

            # messages 来源：非连续 message_ids 切片
            second_id = messages[1]["id"] if len(messages) > 1 else first_id
            msg_selection = MemorySelection(sources=[MemorySource(
                type="messages", context_id=task_id, message_ids=[first_id, second_id],
            )])
            msg_sliced = await service.memory.resolve_selection(msg_selection)
            assert "关键" in msg_sliced

            # 组合多来源：完整会话 + 手动输入
            combo = await service.memory.resolve_selection(MemorySelection(
                sources=[
                    MemorySource(type="session", context_id=task_id),
                    MemorySource(type="manual", text="一段补充说明"),
                ]
            ))
            assert "关键" in combo
            assert "补充说明" in combo

            await service.memory.delete(mem["memory_id"])
            assert not any(
                m["memory_id"] == mem["memory_id"] for m in await service.memory.list()
            )
            return {"task_id": task_id, "thread_id": thread_id}

        try:
            portal.call(scenario)
        finally:
            portal.call(service.checkpointer.adelete_thread, thread_id)
            _remove_workspace_folder(folder)


def test_memory_summarize_empty_raises(monkeypatch):
    import backend.app.desktop.memory as memory_module

    class _FakeResponse:
        content = "x"

    async def fake_ainvoke(_messages, *_args, **_kwargs):
        return _FakeResponse()

    monkeypatch.setattr(
        memory_module,
        "create_chat_model",
        lambda *a, **k: type("M", (), {"ainvoke": fake_ainvoke})(),
    )

    folder = _make_workspace_folder()
    with _client() as client:
        service = app.state.desktop_service
        portal = client.portal
        workspace = client.post(
            "/desktop/api/workspaces", headers=SESSION, json={"path": str(folder)}
        ).json()
        root = client.post(
            f"/desktop/api/workspaces/{workspace['workspace_id']}/threads",
            headers=SESSION,
            json={"thread_id": uuid.uuid4().hex, "title": "空来源"},
        ).json()
        try:
            async def scenario() -> None:
                try:
                    await service.memory.summarize(MemorySummarizeRequest(
                        selection=MemorySelection(sources=[MemorySource(
                            type="manual", text="   ",
                        )])
                    ))
                except ValueError:
                    return
                raise AssertionError("空来源应抛 ValueError")

            portal.call(scenario)
        finally:
            portal.call(service.checkpointer.adelete_thread, root["thread_id"])
            _remove_workspace_folder(folder)


def test_patrol_and_swarm_do_not_inject_memory(monkeypatch):
    """派生 Agent（patrol/teammate/worker）装配时系统提示词不含 `<memory>`。"""
    import backend.app.desktop.service as svc

    captured = {}

    async def fake_make_lead_agent(**kwargs):
        captured["system_prompt"] = kwargs.get("system_prompt", "")
        return create_agent(model=FakeListChatModel(responses=["unused"]), tools=[])

    monkeypatch.setattr(svc, "make_lead_agent", fake_make_lead_agent)
    folder = _make_workspace_folder()
    with _client() as client:
        service = app.state.desktop_service
        portal = client.portal
        workspace = client.post(
            "/desktop/api/workspaces", headers=SESSION, json={"path": str(folder)}
        ).json()
        root = client.post(
            f"/desktop/api/workspaces/{workspace['workspace_id']}/threads",
            headers=SESSION,
            json={"thread_id": uuid.uuid4().hex, "title": "无记忆派生"},
        ).json()

        async def scenario() -> dict:
            equipment = {"model_name": None, "skills": [], "skill_snapshots": [], "permissions": ["read"]}
            # patrol
            patrol_factory = service._build_agent_factory(
                root["task_id"], "patrol:agent", folder, equipment, "你是小兵 PATROL_PROMPT", "", "patrol",
            )
            captured.pop("system_prompt", None)
            await patrol_factory()
            assert "<memory>" not in captured["system_prompt"]
            # teammate (swarm)
            teammate_factory = service._build_agent_factory(
                root["task_id"], "teammate:agent", folder, equipment, "你是 Teammate TEAMMATE_PROMPT", "", "teammate",
            )
            captured.pop("system_prompt", None)
            await teammate_factory()
            assert "<memory>" not in captured["system_prompt"]
            return {"thread_id": root["thread_id"]}

        try:
            portal.call(scenario)
        finally:
            portal.call(service.checkpointer.adelete_thread, root["thread_id"])
            _remove_workspace_folder(folder)
