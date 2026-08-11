# -*- coding: utf-8 -*-
"""承诺子图可恢复执行：父图 resume 不重放、残留 checkpoint 重置重跑。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "packages" / "harness"))
sys.path.insert(0, str(ROOT))

from langchain.agents import create_agent
from langchain.messages import AIMessage, HumanMessage
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from focus.agents.commitment import CommitmentMiddleware
from focus.agents.commitment.schemas import WorkerOutput
from focus.agents.commitment.workflow import _build_supervisor
from focus.agents.commitment.delegation import ReviewedDelegator


class _ScriptedDelegator:
    def __init__(self):
        self.calls = []

    async def run(self, envelope, supervisor_messages=None):
        self.calls.append(envelope.stage)
        results = {
            1: {"goal": "完成 X"},
            2: {
                "requirements": ["使用 LangGraph 完成"], "discarded_requirements": [],
                "compatibility_checks": [{"technology": "LangGraph", "application_type": "desktop",
                                          "ui_surface": "local", "runtime_platform": "windows",
                                          "host_model": "m", "status": "verified"}],
                "conflicts": [],
            },
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
        from focus.agents.commitment.schemas import ReviewOutput
        return ReviewOutput(approved=True, feedback="")


def test_parent_resume_detection():
    import asyncio
    from langgraph._internal._constants import CONFIG_KEY_SCRATCHPAD, CONFIG_KEY_CHECKPOINTER
    from focus.agents.commitment.middleware import _parent_resume_value, _parent_config

    model = FakeListChatModel(responses=["ok"])
    delegator = _ScriptedDelegator()
    middleware = CommitmentMiddleware.__new__(CommitmentMiddleware)
    middleware._skill_names = frozenset()
    middleware._supervisor = _build_supervisor(delegator)
    middleware._context7_tools = []

    agent = create_agent(model=model, tools=[], middleware=[middleware])
    saver = InMemorySaver()
    agent.checkpointer = saver
    config = {"configurable": {"thread_id": "dbg-1"}}

    # 首次执行：/commit 触发承诺 → 子图阶段 3 中断 → middleware raise GraphInterrupt → 父图中断
    ctx = {"workspace": "C:/tmp", "uploads": ""}
    result = asyncio.run(agent.ainvoke(
        {"messages": [HumanMessage(content="/commit 做X", id="m1")]},
        config=config,
        context=ctx,
    ))
    assert "__interrupt__" in result, f"父图未中断: {result}"

    # resume：Command(resume=approve)
    resume = {"decision": "approve"}
    result2 = asyncio.run(agent.ainvoke(Command(resume=resume), config=config, context=ctx))
    assert "__interrupt__" in result2, "resume 后应继续到下一个中断（阶段5）"
    assert delegator.calls == [1, 2, 3, 4, 5], f"resume 不应重放已完成阶段: {delegator.calls}"

    # 模拟刷新/重启后重发 /commit（非 resume 的全新执行，子图 checkpoint 残留）
    delegator2 = _ScriptedDelegator()
    middleware2 = CommitmentMiddleware.__new__(CommitmentMiddleware)
    middleware2._skill_names = frozenset()
    middleware2._supervisor = _build_supervisor(delegator2)
    middleware2._context7_tools = []
    agent2 = create_agent(model=model, tools=[], middleware=[middleware2])
    agent2.checkpointer = saver
    config2 = {"configurable": {"thread_id": "dbg-2"}}
    result3 = asyncio.run(agent2.ainvoke(
        {"messages": [HumanMessage(content="/commit 做X", id="m1")]},
        config=config2, context=ctx,
    ))
    assert "__interrupt__" in result3, "首次执行应正常中断"
    # 参考实现拒绝把残留子图 checkpoint 当成新任务清理重跑，否则会丢弃人工决定。
    with pytest.raises(RuntimeError, match="父图未处于 resume 状态"):
        asyncio.run(agent2.ainvoke(
            {"messages": [HumanMessage(content="/commit 做X", id="m1")]},
            config=config2, context=ctx,
        ))
