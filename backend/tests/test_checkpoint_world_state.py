"""本文件对外提供以下合同的验证 WorldState 的基线依赖、权限 replacement 与 sampling 前耐久提交。

输入为纯 sections、精确 LangGraph checkpoint 和故障 checkpointer；输出为 full/diff、幂等与 sampling 阻断断言。
具体工作流为完整锚定、变化更新、删除依赖后重建，再证明 durable prepared 状态先于模型调用和权限变化。
示例：pytest backend/tests/test_checkpoint_world_state.py。
"""

import asyncio
from dataclasses import replace
from itertools import cycle
from pathlib import Path

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from focus.agents.lead_agent_state import LeadAgentState
from focus.context import WorldSection, compile_world_state
from focus.context.middleware import WorldStateMiddleware
from focus.history import messages_to_items, semantic_messages
from focus.history.middleware import TypedHistoryMiddleware
from focus.security.context import AuthorizationIdentity, RoutingIdentity, SecurityContext
from focus.security.policy import AccessMode


def test_full_diff_replacement_and_missing_dependency_reanchor():
    binding = {"thread_id": "t", "projection_version": "v1"}
    a = WorldSection("tools", "capability", {"read": {"version": 1}}, "catalog")
    initial, snapshot = compile_world_state((a,), None, set(), binding)
    retained = {message.id for message in initial}
    unchanged, same = compile_world_state((a,), snapshot, retained, binding)
    assert unchanged == [] and same == snapshot
    b = WorldSection("tools", "capability", {"read": {"version": 2}, "write": {}}, "catalog")
    delta, latest = compile_world_state((b,), snapshot, retained, binding)
    assert 'update="diff"' in delta[0].content
    assert '"changed": {"read"' in delta[0].content
    assert latest["sections"]["tools"]["retained_refs"] == [initial[0].id, initial[1].id, delta[0].id]
    rebuild, _ = compile_world_state((b,), latest, retained, binding)
    assert 'update="full"' in rebuild[0].content
    assert rebuild[0].id != initial[0].id
    permission = WorldSection("permissions", "policy", {"mode": "read-only"})
    full, baseline = compile_world_state((permission,), None, set(), binding)
    replacement, _ = compile_world_state((WorldSection("permissions", "policy", {"mode": "workspace-write"}),),
                                         baseline, {message.id for message in full}, binding)
    assert 'update="replacement"' in replacement[0].content
    assert semantic_messages(messages_to_items(initial + delta + full + replacement)) == ()


def test_renderer_binding_changes_and_removal_are_explicit():
    section = WorldSection("skills", "capability", {"one": {}}, "catalog")
    messages, snapshot = compile_world_state((section,), None, set(), {"thread_id": "a"})
    changed, _ = compile_world_state((section,), snapshot, {messages[0].id}, {"thread_id": "b"})
    assert 'update="full"' in changed[1].content
    removal, absent = compile_world_state((), snapshot, {message.id for message in messages}, {"thread_id": "a"})
    assert "no longer apply" in removal[0].content and absent["sections"] == {}


@pytest.mark.parametrize("fail_prepared", [False, True])
def test_sampling_waits_for_durable_prepared_checkpoint(tmp_path, fail_prepared):
    async def run():
        observed = []
        class Saver(InMemorySaver):
            async def aput(self, config, checkpoint, metadata, new_versions):
                if fail_prepared and checkpoint["channel_values"].get("request_manifest"):
                    raise RuntimeError("prepared write failed")
                return await super().aput(config, checkpoint, metadata, new_versions)
        saver = Saver()
        config = {"configurable": {"thread_id": "world"}}
        class Model(GenericFakeChatModel):
            async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
                checkpoint = await saver.aget_tuple(config)
                values = checkpoint.checkpoint["channel_values"]
                assert values["request_manifest"]["status"] == "prepared"
                ids = {item["item_id"] for item in values["execution_items"]}
                assert all(set(section["retained_refs"]).issubset(ids) for section in values["world_state_snapshot"]["sections"].values())
                observed.append(values)
                return await super()._agenerate(messages, stop, run_manager, **kwargs)
        model = Model(messages=cycle([AIMessage(content="answer")]))
        middleware = WorldStateMiddleware([], "base", frozenset({"read"}), "test", "test-v1")
        graph = create_agent(model, [], middleware=[middleware, TypedHistoryMiddleware()],
                             state_schema=LeadAgentState, checkpointer=saver, context_schema=dict)
        security = SecurityContext(
            authorization=AuthorizationIdentity(Path(tmp_path), (Path(tmp_path),), ("read",), AccessMode.READ_ONLY, "main"),
            routing=RoutingIdentity("world", "ws", "main", "task", "", "run"),
        )
        if fail_prepared:
            with pytest.raises(RuntimeError, match="prepared write"):
                await graph.ainvoke({"messages": [HumanMessage(content="task")]}, config,
                                    context=security.to_runtime_context(), durability="sync")
            assert observed == []
        else:
            state = await graph.ainvoke({"messages": [HumanMessage(content="task")]}, config,
                                       context=security.to_runtime_context(), durability="sync")
            first_ids = {message.id for message in state["messages"] if message.id and message.id.startswith("world:")}
            narrowed = replace(security, authorization=replace(security.authorization, access_mode=AccessMode.WORKSPACE_WRITE))
            state = await graph.ainvoke({"messages": [HumanMessage(content="continue")]}, config,
                                       context=narrowed.to_runtime_context(), durability="sync")
            second_ids = {message.id for message in state["messages"] if message.id and message.id.startswith("world:")}
            assert len(second_ids - first_ids) == 1
            assert state["request_manifest"]["status"] == "sampled"
            assert len(observed) == 2
    asyncio.run(run())
