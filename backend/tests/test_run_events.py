import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import Command

from focus.runtime.runs.events import (
    build_envelope,
    chunk_to_events,
    serialize_message,
    serialize_value,
    stream_text,
)
from focus.runtime.runs.manager import RunManager
from focus.runtime.runs.schemas import RunStatus
from focus.runtime.runs.worker import run_agent


class _RecordingBridge:
    def __init__(self):
        self.events = []
        self.ended = False

    def publish(self, _run_id, event):
        self.events.append(event)

    def publish_end(self, _run_id):
        self.ended = True

    def cleanup(self, _run_id, delay):
        assert delay == 300


def test_serialize_message_shapes():
    human = serialize_message(HumanMessage(content="你好", id="m1"))
    assert human == {"role": "human", "content": "你好", "id": "m1"}

    ai = serialize_message(AIMessage(content="", id="m2", tool_calls=[{"id": "c1", "name": "read_file", "args": {}}]))
    assert ai["role"] == "ai" and ai["locked"] is True and ai["tool_calls"][0]["id"] == "c1"

    tool = serialize_message(ToolMessage(content="ok", tool_call_id="c1", name="read_file", id="m3"))
    assert tool["role"] == "tool" and tool["locked"] is True and tool["tool_call_id"] == "c1"

    system = serialize_message(SystemMessage(content="sys"))
    assert system["role"] == "system"

    files = serialize_message(HumanMessage(content="x", additional_kwargs={"files": [{"path": "a.md"}]}))
    assert files["files"] == [{"path": "a.md"}]


def test_serialize_value_recursive():
    value = {"messages": [HumanMessage(content="hi")], "n": 1, "tags": ["a", "b"]}
    result = serialize_value(value)
    assert result == {"messages": [{"role": "human", "content": "hi"}], "n": 1, "tags": ["a", "b"]}


def test_stream_text():
    assert stream_text("plain") == "plain"
    assert stream_text([{"type": "text", "text": "ab"}, {"type": "text", "text": "cd"}]) == "abcd"
    assert stream_text(123) == ""


def test_chunk_to_events_tokens():
    class FakeMessage:
        content = "增量"
        id = "chunk-1"

    base = {"workspace_id": "ws-1", "thread_id": "th-1", "agent_id": "main:th-1", "run_id": "run-1"}
    events = chunk_to_events("messages", (FakeMessage(), {"langgraph_node": "model"}), base)
    assert len(events) == 1
    assert events[0].event == "tokens"
    assert events[0].data["data"] == {"content": "增量", "message_id": "chunk-1", "node": "model"}
    assert events[0].data["event"] == "tokens"
    assert events[0].data["workspace_id"] == "ws-1"

    # 空文本 chunk 不发布
    class EmptyMessage:
        content = ""
        id = "chunk-empty"

    assert chunk_to_events("messages", (EmptyMessage(), {"langgraph_node": "model"}), base) == []
    assert chunk_to_events("messages", ("not-a-tuple",), base) == []


def test_chunk_to_events_values():
    base = {"workspace_id": "ws-1", "thread_id": "th-1", "agent_id": "main:th-1", "run_id": "run-1"}
    events = chunk_to_events("values", {"messages": [HumanMessage(content="hi")]}, base)
    assert len(events) == 1
    assert events[0].event == "events"
    assert events[0].data["data"]["messages"] == [{"role": "human", "content": "hi"}]
    assert events[0].data["event"] == "events"


def test_build_envelope():
    envelope = build_envelope("ws-1", "th-1", "main:th-1", "run-1", "status", {"status": "running"})
    assert envelope == {
        "workspace_id": "ws-1", "thread_id": "th-1", "agent_id": "main:th-1",
        "run_id": "run-1", "event": "status", "data": {"status": "running"},
    }


def test_start_run_uses_long_task_recursion_budget(monkeypatch):
    from backend.app.gateway import services

    captured = {}

    async def fake_run_agent(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(services, "run_agent", fake_run_agent)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                stream_bridge=object(),
                run_manager=RunManager(),
                checkpointer=None,
                store=None,
            )
        ),
        state=SimpleNamespace(current_user=None),
    )
    body = SimpleNamespace(
        context={"run_id": "run-long-task", "agent_id": "main:task-a"},
        input={"messages": [{"role": "human", "content": "build a web app"}]},
        resume=None,
        stream_mode=["messages-tuple", "values"],
    )

    async def exercise():
        record = await services.start_run(body, "thread-a", request)
        await record.task

    asyncio.run(exercise())

    assert captured["runnable_config"]["recursion_limit"] == 100


def _run_with_failing_agent_factory(graph_input):
    manager = RunManager()
    record = manager.create("thread-agent-factory", run_id="run-agent-factory")
    bridge = _RecordingBridge()

    async def failing_factory():
        raise RuntimeError("Context7 connection timeout")

    asyncio.run(
        run_agent(
            record=record,
            bridge=bridge,
            run_manager=manager,
            app_config=SimpleNamespace(
                models=[], commitment=SimpleNamespace(enabled=True)
            ),
            graph_input=graph_input,
            runnable_config={"configurable": {"thread_id": record.thread_id}},
            agent_factory=failing_factory,
            langgraph_context={
                "workspace_id": "workspace-agent-factory",
                "agent_id": "main:task-agent-factory",
            },
        )
    )
    return record, bridge


def test_resume_agent_factory_failure_preserves_interrupted_state():
    record, bridge = _run_with_failing_agent_factory(
        Command(resume={"decision": "revise", "feedback": "继续修订"})
    )

    assert record.status is RunStatus.interrupted
    assert record.error == "Context7 connection timeout"
    assert [event.event for event in bridge.events] == ["metadata", "error"]
    assert bridge.ended is True


def test_normal_agent_factory_failure_remains_error():
    record, bridge = _run_with_failing_agent_factory(
        {"messages": [HumanMessage(content="普通消息")]}
    )

    assert record.status is RunStatus.error
    assert record.error == "Context7 connection timeout"
    assert [event.event for event in bridge.events] == ["metadata", "error"]
    assert bridge.ended is True
