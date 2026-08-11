# -*- coding: utf-8 -*-
"""承诺流程期间消息流隔离：子图冒泡消息不得发布为 lead tokens 事件。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "packages" / "harness"))
sys.path.insert(0, str(ROOT))

from langchain.agents import create_agent
from langchain.messages import AIMessage, HumanMessage
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from focus.agents.commitment import CommitmentMiddleware
from focus.agents.commitment.schemas import WorkerOutput, ReviewOutput
from focus.agents.commitment.workflow import _build_supervisor
from focus.agents.commitment.delegation import ReviewedDelegator


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
    import asyncio

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
    ctx = {"workspace": "C:/tmp", "uploads": ""}

    from focus.runtime.runs.events import chunk_to_events

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

    # resume 一轮
    events2 = asyncio.run(collect(Command(resume={"decision": "approve"})))
    tokens = published_tokens(events2)
    print(f"tokens 事件数: {tokens}")
    assert tokens == 0, f"resume 期间不应发布 lead token，实际 {tokens} 个"
