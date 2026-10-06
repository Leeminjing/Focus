"""本文件对外提供修复态 Context 的真实执行分支回归。

输入为真实内存 checkpoint、已证明中断的非成功工具补全及新输入；输出为严格模型入口的闭合历史和原 checkpoint 不变证明。
具体工作流为保存未闭合源历史，通过统一 run_agent 新输入分支执行已发布投影，同时检查 typed authority、输入身份与状态镜像。
示例：python -m pytest backend/tests/test_repaired_execution_branch.py -q。
"""

import asyncio
import pytest

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from backend.app.desktop.context_projection import ProtocolRepairContext, compile_context_messages
from focus.agents.lead_agent_state import LeadAgentState
from focus.config.app_config import AppConfig
from focus.history import serialize_history_message, validate_items, messages_to_items
from focus.history.bridge import synchronize_items
from focus.runtime.runs.manager import RunManager
from focus.runtime.runs.schemas import RunStatus
from focus.runtime.runs.worker import run_agent
from focus.runtime.stream_bridge.memory import MemoryStreamBridge
from focus.security.context import AuthorizationIdentity, RoutingIdentity, SecurityContext
from focus.security.policy import AccessMode


@pytest.mark.parametrize("published", (True, False))
def test_new_input_consumes_published_repair_and_preserves_source(tmp_path, published):
    async def exercise():
        received = []

        async def strict_model(state):
            validate_items(messages_to_items(state["messages"]))
            received.extend(state["messages"])
            return {"messages": [AIMessage(id="answer", content="continued")]}

        builder = StateGraph(LeadAgentState)
        builder.add_node("model", strict_model)
        builder.add_edge(START, "model")
        builder.add_edge("model", END)
        saver = InMemorySaver()
        graph = builder.compile(checkpointer=saver)
        caller = AIMessage(id="caller", content="", tool_calls=[{"id": "unsafe-call", "name": "browser_run_code_unsafe", "args": {}}])
        source = [HumanMessage(id="original", content="task"), caller]
        config = await graph.aupdate_state({"configurable": {"thread_id": "branch"}}, {
            "messages": source, "execution_items": synchronize_items(None, source),
            "world_state_snapshot": {"old": True}, "request_manifest": {"old": True},
        }, as_node=START)
        frozen = await graph.aget_state(config)
        projection = compile_context_messages([serialize_history_message(m) for m in source],
            ProtocolRepairContext.interrupted("original-run", call_ids=("unsafe-call",), reason="human approval pending"))
        assert projection.status == "repaired"
        manager = RunManager()
        record = manager.create(thread_id="branch")
        security = SecurityContext(AuthorizationIdentity(tmp_path, (tmp_path,), (), AccessMode.READ_ONLY, "main"),
            RoutingIdentity("branch", "workspace", "main", "task", "", record.run_id))
        context = security.to_runtime_context()
        if published:
            context["context_execution_messages"] = projection.execution_messages

        async def factory():
            return graph

        await run_agent(record=record, bridge=MemoryStreamBridge(), run_manager=manager, app_config=AppConfig(models=[]),
            graph_input={"messages": [HumanMessage(id="directive", content="continue")]}, runnable_config=config,
            langgraph_context=context, agent_factory=factory, checkpointer=saver)
        unchanged = await graph.aget_state(config)
        assert unchanged.values == frozen.values
        if not published:
            assert record.status is RunStatus.error
            assert not received
            return
        assert record.status is RunStatus.success, record.error
        assert [m.id for m in received] == ["original", "caller", projection.execution_messages[-1]["id"], "directive"]
        assert received[2].status == "error"
        assert received[2].additional_kwargs["curation_synthetic"]
        unchanged = await graph.aget_state(config)
        assert unchanged.values == frozen.values
        latest = await graph.aget_state({"configurable": {"thread_id": "branch"}})
        assert latest.values["world_state_snapshot"] is None
        assert latest.values["request_manifest"] is None
        assert latest.values["execution_items"] == synchronize_items(None, received)

    asyncio.run(exercise())
