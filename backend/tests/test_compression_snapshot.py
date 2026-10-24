"""验证压缩面板完整快照、关键词预览和应用均读取当前 Revision 的执行身份。"""

import backend.app.desktop.conversations.compression_service as _desktop_conversations_compression_service
import backend.app.desktop.runs.agent_factory as _desktop_runs_agent_factory


import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple

from backend.app.desktop.composition import service as desktop_service
from backend.app.desktop.api.compression import compression_router
from backend.app.desktop.composition.service import DesktopService
from focus.agents.compression import apply_compression_ranges
from focus.runtime.runs.events import serialize_message


@pytest.fixture
def compression_snapshot(monkeypatch):
    task = SimpleNamespace(task_id="task-1", thread_id="thread-1")
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.scalar.return_value = None
    service = object.__new__(DesktopService)
    service.session_factory = lambda: session
    service.workspaces._get_task_entities = AsyncMock(return_value=(task, object()))
    service.app_config = object()
    service.contexts = SimpleNamespace(
        ensure_runnable=AsyncMock(),
        evolution=SimpleNamespace(publish_checkpoint=AsyncMock()),
    )
    service.context_patrol = SimpleNamespace(notify_stable_context_checkpoint=AsyncMock())
    current = AsyncMock(return_value=None)
    monkeypatch.setattr(_desktop_conversations_compression_service.ContextRevisionRepository, "current", current)

    source = HumanMessage(content="earlier source", id="source-1")
    block = apply_compression_ranges(
        [source], [{"source_ids": [source.id], "replacement": "earlier summary"}]
    )["messages"][1]
    messages = [HumanMessage(content="target keyword", id="hit-1"), block]
    checkpoints = {}

    async def read_checkpoint(config):
        identity = config["configurable"]
        records = checkpoints.get((identity["thread_id"], identity.get("checkpoint_ns", "")))
        if records is None:
            return None
        return CheckpointTuple(
            config=config, checkpoint={"channel_values": {"messages": records}}, metadata={},
        )

    service.checkpointer = SimpleNamespace(
        serde=BaseCheckpointSaver().serde, aget_tuple=AsyncMock(side_effect=read_checkpoint),
    )

    async def update_state(config, update):
        checkpoint = await graph.checkpointer.aget_tuple(config)
        checkpoint.checkpoint["channel_values"]["messages"][:] = update["messages"][1:]
        return {"configurable": {"checkpoint_id": "compressed-1"}}

    graph = SimpleNamespace(aupdate_state=AsyncMock(side_effect=update_state))
    monkeypatch.setattr(_desktop_conversations_compression_service, 'make_lead_agent', AsyncMock(return_value=graph))
    monkeypatch.setattr(_desktop_runs_agent_factory, 'make_lead_agent', AsyncMock(return_value=graph))
    app = FastAPI()
    app.state.desktop_service = service
    app.include_router(compression_router)
    return SimpleNamespace(
        service=service, app=app, current=current, messages=messages,
        checkpoints=checkpoints, graph=graph,
    )


@pytest.mark.parametrize(
    "identity",
    [None, ("thread-1", ""), ("shadow:task-1:revision-2", "context-revision-shadow")],
    ids=["no-revision", "root-revision", "shadow-revision"],
)
def test_panel_snapshot_and_apply_share_current_revision(compression_snapshot, identity):
    fixture = compression_snapshot
    expected_identity = identity or ("thread-1", "")
    if identity is not None:
        fixture.current.return_value = SimpleNamespace(
            ref=SimpleNamespace(execution_thread_id=identity[0], checkpoint_ns=identity[1])
        )
    fixture.checkpoints[("thread-1", "")] = [HumanMessage(content="stale history", id="stale-1")]
    fixture.checkpoints[expected_identity] = fixture.messages
    expected_messages = [serialize_message(message) for message in fixture.messages]

    with TestClient(fixture.app) as client:
        response = client.get("/desktop/api/tasks/task-1/messages")
    assert response.status_code == 200
    assert response.json() == {"messages": expected_messages}
    assert response.json()["messages"][1]["compression"]["source"][0]["id"] == "source-1"

    preview = asyncio.run(fixture.service.quick_compression_preview("task-1", "keyword"))
    assert preview["hit_ids"] == ["hit-1"]
    assert preview["hit_messages"] == [expected_messages[0]]
    result = asyncio.run(fixture.service.quick_compression_apply(
        "task-1", [{"source_ids": ["hit-1"], "replacement": "new summary"}]
    ))
    assert result["messages"][0]["content"] == "new summary"
    assert result["messages"][0]["compression"]["source"] == [expected_messages[0]]
    assert result["messages"][1] == expected_messages[1]
    for call in fixture.service.checkpointer.aget_tuple.call_args_list:
        config = call.args[0]["configurable"]
        assert (config["thread_id"], config.get("checkpoint_ns", "")) == expected_identity
    fixture.graph.aupdate_state.assert_awaited_once()
    assert fixture.graph.aupdate_state.call_args.args[0] == {
        "configurable": {"thread_id": expected_identity[0]}
    }
    if expected_identity[1]:
        assert isinstance(fixture.graph.checkpointer, _desktop_conversations_compression_service.NamespacedCheckpointer)
    else:
        assert fixture.graph.checkpointer is fixture.service.checkpointer


def test_panel_snapshot_empty_checkpoint(compression_snapshot):
    fixture = compression_snapshot
    fixture.current.return_value = SimpleNamespace(ref=SimpleNamespace(
        execution_thread_id="shadow:task-1:empty", checkpoint_ns="context-revision-shadow",
    ))
    fixture.checkpoints[("thread-1", "")] = fixture.messages
    with TestClient(fixture.app) as client:
        response = client.get("/desktop/api/tasks/task-1/messages")
    assert response.status_code == 200
    assert response.json() == {"messages": []}


def test_panel_snapshot_missing_task(compression_snapshot):
    fixture = compression_snapshot
    fixture.service._get_task_entities.side_effect = HTTPException(404, "任务不存在")
    with TestClient(fixture.app) as client:
        response = client.get("/desktop/api/tasks/missing/messages")
    assert response.status_code == 404
    fixture.service.checkpointer.aget_tuple.assert_not_awaited()


def test_apply_rejects_snapshot_after_current_revision_changes(compression_snapshot):
    fixture = compression_snapshot
    fixture.checkpoints[("thread-1", "")] = fixture.messages
    with TestClient(fixture.app) as client:
        snapshot = client.get("/desktop/api/tasks/task-1/messages").json()
    fixture.current.return_value = SimpleNamespace(ref=SimpleNamespace(
        execution_thread_id="shadow:task-1:new", checkpoint_ns="context-revision-shadow",
    ))
    fixture.checkpoints[("shadow:task-1:new", "context-revision-shadow")] = [
        HumanMessage(content="changed context", id="new-1")
    ]
    with pytest.raises(HTTPException) as error:
        asyncio.run(fixture.service.quick_compression_apply(
            "task-1", [{"source_ids": [snapshot["messages"][0]["id"]], "replacement": "summary"}]
        ))
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "context_changed"
    fixture.graph.aupdate_state.assert_not_awaited()
