"""本文件提供合同镜像历史手术、真实 graph 恢复与附件展示一致性的回归。

输入为冻结 handoff、实际 CompressionGate、内存／隔离 PostgreSQL saver 和 Reader；输出为镜像同步、旧状态兼容及 canonical 不变断言。
工作流为触发真实压缩中断，删除或替代合同、恢复原文、重构图继续，并对比 Reader 与 live snapshot 的只读附件投影。
示例：pytest backend/tests/test_contract_history_repairs.py；不调用真实 Provider。
"""

import asyncio
from copy import deepcopy
from itertools import cycle
import os
from types import SimpleNamespace
import uuid

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.message import add_messages
from langgraph.types import Command

from backend.tests.test_commitment_handoff import _completed
from backend.tests.test_commitment_handoff_recovery import _middleware
from backend.tests.test_commitment_resume import _ScriptedDelegator
from backend.tests.test_context_revision_reader import _ref, _revision, _RevisionStore
from backend.app.desktop.context_evolution import ContextRevisionPayloadMode, ContextRevisionReader
from focus.agents.commitment.handoff import compile_handoff
from focus.agents.commitment.middleware import CommitmentMiddleware
from focus.agents.compression.gate import CompressionGate, apply_compression_ranges
from focus.agents.lead_agent_state import LeadAgentState
from focus.history import HistoryPayload, messages_to_items
from focus.history.bridge import synchronize_items
from focus.history.middleware import TypedHistoryMiddleware
from focus.history.task_contract import task_contract_state_update
from focus.runtime.runs.events import serialize_value


def test_typed_mirror_is_cleared_even_when_recovering_a_stale_scalar():
    async def run():
        state, trigger, checkpoint = _completed()
        delivery = compile_handoff(state, trigger, checkpoint, [])
        messages = [trigger, *delivery]
        update = apply_compression_ranges(messages, [{"source_ids": [delivery[0].id], "delete": True}])
        assert update["task_contract"] is None and update["task_contract_source"] == "typed"
        retained = add_messages(messages, update["messages"])
        middleware = CommitmentMiddleware.__new__(CommitmentMiddleware)
        middleware._skill_names = frozenset()
        result = await middleware._run({"messages": retained, "task_contract": state["task_contract"], "task_contract_source": "typed"}, SimpleNamespace(context={}))
        assert result == {"task_contract": None}
    asyncio.run(run())


def test_legacy_scalar_is_explicit_and_tags_do_not_create_typed_authority():
    state = {"messages": [HumanMessage(id="old", content="<task_contract>plain text</task_contract>")], "task_contract": "old approved string"}
    assert task_contract_state_update(state) == {"task_contract_source": "legacy"}
    assert task_contract_state_update({**state, "task_contract_source": "legacy"}) == {}


def _graph(saver):
    graph = create_agent(GenericFakeChatModel(messages=cycle([AIMessage(content="answer")])), [],
        middleware=[_middleware(_ScriptedDelegator()), CompressionGate(1, 1), TypedHistoryMiddleware()],
        state_schema=LeadAgentState, context_schema=dict)
    graph.checkpointer = saver
    return graph


async def _exercise_graph(saver, operation):
    state, trigger, checkpoint = _completed()
    delivery = compile_handoff(state, trigger, checkpoint, [])
    messages = [trigger, *delivery]
    graph = _graph(saver)
    config = {"configurable": {"thread_id": "mirror-compression-" + uuid.uuid4().hex}}
    before = await graph.aupdate_state(config, {"messages": messages, "execution_items": synchronize_items(None, messages),
        "task_contract": state["task_contract"], "task_contract_source": "typed"})
    original = deepcopy((await graph.aget_state(before)).values)
    first = await graph.ainvoke({"messages": [HumanMessage(id="later", content="Continue working.")]}, config, context={}, durability="sync")
    assert first["__interrupt__"][0].value["type"] == "compression_request"
    selected = {"source_ids": [delivery[0].id], **({"delete": True} if operation == "delete" else {"replacement": "Contract summarized."})}
    done = await graph.ainvoke(Command(resume={"type": "compression", "decision": "apply", "ranges": [selected]}), config, context={}, durability="sync")
    assert done["task_contract"] is None and done["task_contract_source"] == "typed"
    assert not any(item["kind"] == "task_contract" for item in done["execution_items"])
    rebuilt = _graph(saver)
    result = await rebuilt.ainvoke({"messages": [HumanMessage(id="next-run", content="Follow up.")]}, config, context={}, durability="sync")
    assert result["__interrupt__"][0].value["type"] == "compression_request" and result["task_contract"] is None
    block = next(message for message in result["messages"] if message.additional_kwargs.get("compression"))
    restored = await rebuilt.ainvoke(Command(resume={"type": "compression", "decision": "apply", "ranges": [{"source_ids": [block.id], "restore": True}]}), config, context={}, durability="sync")
    assert restored["task_contract"] == state["task_contract"] and restored["task_contract_source"] == "typed"
    assert sum(item["kind"] == "task_contract" for item in restored["execution_items"]) == 1
    assert (await rebuilt.aget_state(before)).values == original
    synchronize_items(restored["execution_items"], restored["messages"])


@pytest.mark.parametrize("operation", ["delete", "replace"])
def test_real_compression_graph_clears_and_restores_typed_mirror(operation):
    asyncio.run(_exercise_graph(InMemorySaver(), operation))


@pytest.mark.usefixtures("isolated_postgres_database")
def test_durable_compression_mirror_reconstructs_with_exact_old_checkpoint():
    async def run():
        async with AsyncPostgresSaver.from_conn_string(os.environ["FOCUS_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")) as saver:
            await _exercise_graph(saver, "delete")
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


@pytest.mark.parametrize("mode", [ContextRevisionPayloadMode.CHECKPOINT, ContextRevisionPayloadMode.DEFINITION])
def test_reader_and_live_contract_display_keep_attachments_without_mutation(mode):
    async def run():
        state, trigger, checkpoint = _completed()
        trigger.additional_kwargs["files"] = [{"path": "uploads/brief.png", "name": "brief.png", "type": "image"}]
        delivery = compile_handoff(state, trigger, checkpoint, [])
        messages = [trigger, *delivery]
        original = deepcopy(messages)
        items = messages_to_items(messages)
        ref = _ref("attachments", "attachments-r", mode)
        revision = _revision(ref).model_copy(update={"history_payload": HistoryPayload(authored_items=items, execution_items=items)})
        reader = ContextRevisionReader(_RevisionStore(revision), None)
        display = await reader.read(None, ref, "display")
        live = serialize_value({"messages": messages})["messages"]
        assert list(display.messages) == live and len(live) == 1
        assert live[0]["files"] == trigger.additional_kwargs["files"]
        live[0]["files"][0]["name"] = "display edit"
        assert messages == original and "files" not in delivery[0].additional_kwargs
    asyncio.run(run())
