"""本文件对外提供以下合同的验证 typed authority、checkpoint 恢复和 semantic 资格的集成边界。

输入为真实 LangGraph 内存 checkpoint 与无网络消息 fixtures；输出为持久化一致性、篡改阻断和证据资格断言。
具体工作流为完成一次 sampling、读取并续跑精确 checkpoint，再验证显式新分支清理与 RSI fallback 规则。
示例：pytest backend/tests/test_typed_history_checkpoint.py。
"""

import asyncio
from itertools import cycle

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from focus.agents.lead_agent_state import LeadAgentState
from focus.history import FocusItem, messages_to_items, semantic_messages
from focus.history.bridge import branch_messages, replace_execution_items, synchronize_items
from focus.history.middleware import TypedHistoryMiddleware
from backend.app.desktop.context_evolution.checkpoint_writer import LangGraphContextCheckpointWriter
from backend.app.desktop.agent_loop.context_expansion.semantic_indexer import RevisionSemanticIndexer
from backend.app.desktop.context_evolution import ContextRevisionRef


def test_history_is_persisted_and_projection_tampering_blocks_sampling():
    async def run():
        graph = create_agent(
            GenericFakeChatModel(messages=cycle([AIMessage(content="answer")])), [],
            middleware=[TypedHistoryMiddleware()], state_schema=LeadAgentState,
            checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": "typed-history"}}
        await graph.ainvoke({"messages": [HumanMessage(content="task", id="u")]}, config)
        checkpoint = await graph.aget_state(config)
        assert checkpoint.values["execution_items"] == synchronize_items(None, checkpoint.values["messages"])
        altered = await graph.aupdate_state(config, {"messages": [HumanMessage(content="changed", id="u")]})
        with pytest.raises(ValueError, match="bridge"):
            await graph.ainvoke({"messages": [HumanMessage(content="continue", id="v")]}, altered)
    asyncio.run(run())


def test_reducer_is_idempotent_and_requires_explicit_reset():
    original = synchronize_items(None, [HumanMessage(content="a", id="u")])
    assert replace_execution_items(original, original) == original
    changed = synchronize_items(None, [HumanMessage(content="b", id="u")])
    with pytest.raises(ValueError, match="不可原位"):
        replace_execution_items(original, changed)
    assert replace_execution_items(original, None) is None
    assert replace_execution_items(None, changed) == changed


def test_new_branch_removes_runtime_and_opaque_continuation():
    native = AIMessage(content=[{"type": "text", "text": "result"}, {"type": "reasoning", "summary": []}],
                       additional_kwargs={"focus_response_items": [{"type": "reasoning", "encrypted_content": "opaque"}]})
    runtime = HumanMessage(content="old permission", additional_kwargs={"focus_context": {"scope": "runtime"}})
    rebuilt = branch_messages([HumanMessage(content="task"), native, runtime])
    assert len(rebuilt) == 2
    assert "focus_response_items" not in rebuilt[1].additional_kwargs
    assert rebuilt[1].content == [{"type": "text", "text": "result"}]
    assert "focus_response_items" in native.additional_kwargs


def test_only_closed_real_exchange_is_eligible_evidence():
    call = AIMessage(content="", id="a", tool_calls=[{"id": "c", "name": "read", "args": {"path": "test.txt"}}])
    orphan = ToolMessage(content="not proof", id="o", tool_call_id="other")
    assert semantic_messages(messages_to_items([call, orphan])) == ()
    repair = ToolMessage(content="interrupted", id="r", tool_call_id="c", additional_kwargs={"curation_synthetic": True})
    assert semantic_messages(messages_to_items([call, repair])) == ()


def test_evidence_only_has_no_automatic_hypothesis():
    messages = [AIMessage(content="", id="a", tool_calls=[{"id": "c", "name": "read", "args": {"path": "p"}}]),
                ToolMessage(content="evidence", id="o", tool_call_id="c")]
    source = ContextRevisionRef(context_id="ctx", revision_id="rev", generation=1,
                                execution_thread_id="t", checkpoint_id="cp", payload_mode="checkpoint")
    index = RevisionSemanticIndexer().index(
        source=source, source_content_hash="a" * 64, context_role="test", active_objective="test",
        raw_messages=semantic_messages(messages_to_items(messages)),
    )
    assert not index.fallback_segment_ids
    assert not index.semantic_units
    assert len(index.messages) == 2


def test_illegal_provenance_promotion_is_rejected():
    with pytest.raises(ValueError, match="提升"):
        FocusItem(item_id="r", kind="reasoning", origin="direct_user", payload={})
    with pytest.raises(ValueError, match="runtime"):
        FocusItem(item_id="w", kind="world_state_update", origin="provider", payload={})


def test_shadow_writer_preserves_exact_typed_state_and_active_namespace():
    async def run():
        saver = InMemorySaver()
        async def factory():
            return create_agent(GenericFakeChatModel(messages=cycle([AIMessage(content="unused")])), [],
                                state_schema=LeadAgentState)
        active = await factory()
        active.checkpointer = saver
        await active.aupdate_state({"configurable": {"thread_id": "active"}}, {"messages": [HumanMessage(id="keep", content="active")]})
        writer = LangGraphContextCheckpointWriter(factory, saver)
        shadow = ContextRevisionRef(context_id="c", revision_id="r", generation=1,
                                    execution_thread_id="shadow:c:r", checkpoint_ns="context-revision-shadow",
                                    payload_mode="definition")
        checkpoint, ids = await writer.write(shadow, ({"role": "human", "id": "u", "content": "definition"},))
        ref = shadow.model_copy(update={"checkpoint_id": checkpoint})
        assert ids == ("u",)
        records = await writer.read_records(ref)
        assert records[0]["id"] == "u" and records[0]["content"] == "definition"
        unchanged = await active.aget_state({"configurable": {"thread_id": "active"}})
        assert unchanged.values["messages"][0].id == "keep"
    asyncio.run(run())


def test_parallel_tools_keep_native_calls_and_results_in_checkpoint():
    class ToolModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self
    async def run():
        @tool
        async def lookup(query: str) -> str:
            """返回给定查询的证据。"""
            await asyncio.sleep(0.01 if query == "one" else 0)
            return "evidence:" + query
        native = [{"type": "reasoning", "encrypted_content": "opaque", "summary": []},
                  {"type": "function_call", "name": "lookup", "call_id": "c1", "arguments": '{"query":"one"}'},
                  {"type": "function_call", "name": "lookup", "call_id": "c2", "arguments": '{"query":"two"}'}]
        calls = AIMessage(id="calls", content="", additional_kwargs={"focus_response_items": native},
                          tool_calls=[{"id": "c1", "name": "lookup", "args": {"query": "one"}},
                                      {"id": "c2", "name": "lookup", "args": {"query": "two"}}])
        graph = create_agent(ToolModel(messages=iter([calls, AIMessage(content="done")])), [lookup],
                             middleware=[TypedHistoryMiddleware()], state_schema=LeadAgentState,
                             checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "parallel"}}
        state = await graph.ainvoke({"messages": [HumanMessage(id="u", content="lookup")]}, config)
        assert state["execution_items"] == synchronize_items(None, state["messages"])
        assert {message.tool_call_id for message in state["messages"] if isinstance(message, ToolMessage)} == {"c1", "c2"}
        assert [raw["payload"] for raw in state["execution_items"][1:4]] == native
    asyncio.run(run())
