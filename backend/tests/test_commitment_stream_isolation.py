# -*- coding: utf-8 -*-
"""本文件对外提供承诺流隔离和具名角色正文模式的测试。

输入为离线承诺图、完整消息及会中断的模型增量；输出为原 lead 隔离、snapshot/delta 与重试身份断言。
具体工作流为运行承诺图及真实消息序列化，核对具名消息沿原 events 通道传递且不会丢失模式或流身份。
示例：python -m pytest backend/tests/test_commitment_stream_isolation.py。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "packages" / "harness"))
sys.path.insert(0, str(ROOT))

from langchain.agents import create_agent
from httpx import RemoteProtocolError
from langchain.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from focus.agents.commitment import CommitmentMiddleware
from focus.agents.commitment.schemas import WorkerOutput, ReviewOutput
from focus.agents.commitment.workflow import _build_supervisor
from focus.agents.commitment.delegation import ReviewedDelegator
from focus.agents.commitment import tracing
from focus.runtime.runs.events import chunk_to_events


def _governed_context(workspace: str = "C:/tmp") -> dict:
    """承诺层的父执行上下文：经与生产同源的组装入口，不手写受治理键。"""
    from backend.tests.runtime_context_support import runtime_context

    return runtime_context(
        agent_id="main:dbg-s1",
        task_id="dbg-s1",
        workspace=workspace,
        permissions=("read", "write", "host_command"),
        run_id="",
    )


class _ScriptedDelegator:
    def __init__(self):
        self.calls = []

    async def run(self, envelope, supervisor_messages=None):
        self.calls.append(envelope.stage)
        results = {
            1: {"goal": "完成 X"},
            2: {"requirements": ["使用 LangGraph 完成"], "discarded_requirements": [],
                "compatibility_checks": [{"technology": "LangGraph", "application_type": "desktop",
                                          "ui_surface": "local", "runtime_platform": "windows",
                                          "host_model": "m", "status": "verified"}], "conflicts": []},
            3: {"requirements": [{"requirement": "使用 LangGraph 完成", "priority": 3}]},
            4: {"files": [], "urls": []},
            5: {"technologies": [{"name": "LangGraph", "project_version": "unresolved", "version": "latest-stable",
                                  "library_id": "/langchain-ai/langgraph", "source_url": None,
                                  "version_basis": "latest_stable_policy", "version_evidence": "x"}]},
            6: {"knowledge": [{"technology": "LangGraph", "version": "latest-stable",
                               "source_url": "https://docs.langchain.com", "content": "正文"}]},
            7: {"contract_markdown": "# 任务合同\n\n目标：完成 X。"},
        }
        return WorkerOutput(result=results[envelope.stage]), ""

    async def _evaluator(self, envelope, worker_output, supervisor_messages=None,
                         attempt=1, structure_error="", reviewer_feedback=""):
        return ReviewOutput(approved=True, feedback="")


def _stream_summary(mode, chunk):
    if mode == "messages":
        msg = chunk[0] if isinstance(chunk, tuple) else chunk
        content = getattr(msg, "content", "")
        if isinstance(content, list):
            content = "".join(p if isinstance(p, str) else str(p.get("text", "")) for p in content if isinstance(p, (str, dict)))
        return f"messages token={content!r}"
    if mode == "values":
        msgs = chunk.get("messages", [])
        ai = [m for m in msgs if getattr(m, "type", "") in ("ai", "AIMessage")]
        human = [m for m in msgs if getattr(m, "type", "") == "human"]
        return f"values msgs={len(msgs)} ai={len(ai)} human={len(human)} last={getattr(msgs[-1], 'content', '')[:40]!r} interrupt={'__interrupt__' in chunk}"
    if mode == "custom":
        d = chunk if isinstance(chunk, dict) else {}
        return f"custom type={d.get('type')} actor={d.get('actor')} stage={d.get('stage')} msgs={len(d.get('messages', []))}"
    return f"{mode} {str(chunk)[:60]}"


def test_commitment_period_stream_modes():
    model = FakeListChatModel(responses=["ok"])
    delegator = _ScriptedDelegator()
    middleware = CommitmentMiddleware.__new__(CommitmentMiddleware)
    middleware._skill_names = frozenset()
    middleware._supervisor = _build_supervisor(delegator)
    middleware._context7_tools = []
    agent = create_agent(model=model, tools=[], middleware=[middleware])
    saver = InMemorySaver()
    agent.checkpointer = saver
    config = {"configurable": {"thread_id": "dbg-s1"}}
    ctx = _governed_context()

    async def collect(graph_input):
        out = []
        async for mode, chunk in agent.astream(
            graph_input, config=config, context=ctx,
            stream_mode=["messages", "values", "custom"],
        ):
            out.append((mode, chunk))
        return out

    def published_tokens(events):
        """模拟 worker 发布路径：chunk_to_events 后统计 tokens 事件。"""
        envelope = {"workspace_id": "ws", "thread_id": "th", "agent_id": "main:th", "run_id": "r"}
        count = 0
        for mode, chunk in events:
            for event in chunk_to_events(mode, chunk, envelope):
                if event.event == "tokens":
                    count += 1
        return count

    events1 = asyncio.run(collect({"messages": [HumanMessage(content="/commit 做X", id="m1")]}))
    tokens = published_tokens(events1)
    print(f"tokens 事件数: {tokens}")
    assert tokens == 0, f"承诺期间不应发布 lead token，实际 {tokens} 个"
    supervisor = [
        chunk for mode, chunk in events1
        if mode == "custom" and chunk.get("type") == "commitment_messages"
        and chunk.get("actor") == "supervisor"
    ]
    assert supervisor and all(batch["content_mode"] == "snapshot" for batch in supervisor)

    # resume 一轮
    events2 = asyncio.run(collect(Command(resume={"decision": "approve"})))
    tokens = published_tokens(events2)
    print(f"tokens 事件数: {tokens}")
    assert tokens == 0, f"resume 期间不应发布 lead token，实际 {tokens} 个"


def test_complete_named_messages_keep_roles_and_snapshot_mode(monkeypatch):
    published = []
    monkeypatch.setattr(tracing, "get_stream_writer", lambda: published.append)
    tracing.emit_commitment_messages(
        actor="evaluator", stage=2, attempt=3,
        messages=[
            HumanMessage(content="审核输入", id="input"),
            AIMessage(content="完整审核结果", additional_kwargs={"reasoning_content": "private"}),
        ],
    )
    event, = chunk_to_events("custom", published[0], {"run_id": "run-evaluator"})
    payload = event.data["data"]
    assert event.event == "events"
    assert payload == {
        "type": "commitment_messages", "actor": "evaluator", "stage": 2,
        "attempt": 3, "content_mode": "snapshot",
        "messages": [
            {"role": "human", "content": "审核输入", "id": "input"},
            {"role": "ai", "content": "完整审核结果"},
        ],
    }


def test_named_delta_identity_survives_serialization_and_transport_retry(monkeypatch):
    published = []
    monkeypatch.setattr(tracing, "get_stream_writer", lambda: published.append)

    class RetryingAgent:
        def __init__(self):
            self.calls = 0

        async def astream(self, _input, stream_mode):
            self.calls += 1
            for text in ("旧", "片段") if self.calls == 1 else ("新", "结果"):
                yield "messages", (AIMessageChunk(content=text), {})
            if self.calls == 1:
                raise RemoteProtocolError("stream interrupted")
            yield "values", {"messages": [AIMessage(content="新结果")]}

    agent = RetryingAgent()
    delegator = ReviewedDelegator.__new__(ReviewedDelegator)
    result = asyncio.run(delegator._stream_agent(
        agent, [HumanMessage(content="原输入")], actor="worker", stage=2,
        stream_id="worker-2", attempt=3,
    ))
    payloads = [
        chunk_to_events("custom", batch, {"run_id": "run-worker"})[0].data["data"]
        for batch in published
    ]
    assert result["messages"][-1].content == "新结果"
    assert payloads[0]["content_mode"] == "snapshot"
    assert payloads[0]["messages"][0]["role"] == "human"
    deltas = payloads[1:]
    assert [batch["content_mode"] for batch in deltas] == ["delta"] * 4
    assert [batch["stream_id"] for batch in deltas] == [
        "worker-2:3:1", "worker-2:3:1", "worker-2:3:2", "worker-2:3:2",
    ]
    assert [batch["messages"][0]["content"] for batch in deltas] == ["旧", "片段", "新", "结果"]
    assert all(batch["messages"][0]["role"] == "ai" for batch in deltas)
    assert all(batch["actor"] == "worker" and batch["attempt"] == 3 for batch in deltas)
