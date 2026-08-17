"""本文件对外提供压缩门 spike 与 gate 单测。

输入为 create_agent 装配的极小 Agent 图与 InMemorySaver；输出为对 before_model 内
interrupt 暂停/恢复语义的断言。具体工作流为：以超小阈值触发压缩门中断，断言 values
chunk 含 __interrupt__ 且 messages 不变；再以 Command(resume=...) 分别提交 apply 与
cancel，断言替换/原样继续语义。示例：
`py -3.12 -m pytest backend/tests/test_compression_gate.py -v`
"""

import asyncio
import itertools

import pytest

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.messages import RemoveMessage
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt

from focus.agents.compression.gate import (
    CompressionGate,
    _apply_plan,
    _repair_protocol,
    _strip_compression_kwargs,
    build_compression_gate,
)
from focus.agents.compression.schemas import validate_apply_decision
from focus.agents.compression.tokens import estimate_raw_tokens
from focus.runtime.runs.events import serialize_message, validate_messages


def _recording_model(calls: list) -> GenericFakeChatModel:
    """构造记录每次模型调用 messages 的 fake model（闭包收集，绕开 pydantic 字段限制）。"""

    class RecordingModel(GenericFakeChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            calls.append(list(messages))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    return RecordingModel(messages=itertools.cycle([AIMessage(content="回答")]))


class _SpikeGate(AgentMiddleware):
    """spike 专用压缩门：usage 以消息文本长度近似，超阈值即 interrupt。"""

    def __init__(self, threshold: int) -> None:
        super().__init__()
        self.threshold = threshold

    def before_model(self, state, runtime):
        messages = state["messages"]
        usage = sum(len(str(getattr(message, "content", ""))) for message in messages)
        context = runtime.context
        if usage < self.threshold or (isinstance(context, dict) and context.get("compression_suppressed")):
            return None
        decision = interrupt({"type": "compression_request", "usage": usage})
        if decision is None or decision.get("decision") == "cancel":
            if isinstance(context, dict):
                context["compression_suppressed"] = True
            return None
        removed = [
            RemoveMessage(id=message_id)
            for message_range in decision["ranges"]
            for message_id in message_range["source_ids"]
        ]
        blocks = [
            HumanMessage(
                content=message_range["replacement"],
                additional_kwargs={
                    "compression": {
                        "block_id": f"b{index}",
                        "source": message_range["source_ids"],
                    }
                },
            )
            for index, message_range in enumerate(decision["ranges"])
        ]
        return {"messages": [*removed, *blocks]}


def _make_agent(threshold: int):
    model = GenericFakeChatModel(messages=itertools.cycle([AIMessage(content="回答")]))
    agent = create_agent(
        model=model,
        tools=[],
        middleware=[_SpikeGate(threshold=threshold)],
        system_prompt="spike",
    )
    agent.checkpointer = InMemorySaver()
    return agent


async def _collect(graph, graph_input, config, context):
    chunks = []
    async for _mode, chunk in graph.astream(
        graph_input, config=config, context=context, stream_mode=["values"]
    ):
        chunks.append(chunk)
    return chunks


def test_spike_interrupt_pauses_and_messages_unchanged():
    """1.1 超阈时图暂停并产生 compression_request interrupt，messages 不变。"""
    agent = _make_agent(threshold=5)
    original = HumanMessage(content="这是一条很长的消息内容用来触发阈值")
    config = {"configurable": {"thread_id": "spike-pause"}, "recursion_limit": 50}

    chunks = asyncio.run(
        _collect(agent, {"messages": [original]}, config, {"compression_suppressed": False})
    )
    last = chunks[-1]
    interrupts = last.get("__interrupt__")
    assert interrupts, "最后一帧 values 应含 __interrupt__"
    payload = interrupts[0].value
    assert payload["type"] == "compression_request"
    assert payload["usage"] >= 5
    assert [message.id for message in last["messages"]] == [original.id]


def test_spike_resume_apply_replaces_range_with_block():
    """1.2a resume apply 后范围替换为压缩块，模型以替换后 messages 继续。"""
    agent = _make_agent(threshold=5)
    original = HumanMessage(content="这是一条很长的消息内容用来触发阈值")
    config = {"configurable": {"thread_id": "spike-apply"}, "recursion_limit": 50}
    context = {"compression_suppressed": False}

    asyncio.run(_collect(agent, {"messages": [original]}, config, context))
    chunks = asyncio.run(
        _collect(
            agent,
            Command(
                resume={
                    "type": "compression",
                    "decision": "apply",
                    "ranges": [
                        {"source_ids": [original.id], "replacement": "压缩后的摘要"}
                    ],
                }
            ),
            config,
            context,
        )
    )
    messages = chunks[-1]["messages"]
    assert [message.content for message in messages] == ["压缩后的摘要", "回答"]
    block = messages[0]
    assert isinstance(block, HumanMessage)
    assert block.additional_kwargs["compression"]["source"] == [original.id]
    assert block.additional_kwargs["compression"]["block_id"] == "b0"


def test_spike_resume_cancel_keeps_messages_and_continues():
    """1.2b resume cancel 后 messages 原样，模型继续执行且不再次中断。"""
    agent = _make_agent(threshold=5)
    original = HumanMessage(content="这是一条很长的消息内容用来触发阈值")
    config = {"configurable": {"thread_id": "spike-cancel"}, "recursion_limit": 50}
    context = {"compression_suppressed": False}

    asyncio.run(_collect(agent, {"messages": [original]}, config, context))
    chunks = asyncio.run(
        _collect(
            agent,
            Command(resume={"type": "compression", "decision": "cancel"}),
            config,
            context,
        )
    )
    last = chunks[-1]
    assert "__interrupt__" not in last, "cancel 后同一执行内不得再次中断"
    assert [message.content for message in last["messages"]] == [original.content, "回答"]
    assert last["messages"][0].id == original.id


# === 真实 CompressionGate 单测（tasks 2.5）===

def test_estimate_raw_tokens_formula():
    """启发式公式与桌面 estimate_tokens 同口径。"""
    assert estimate_raw_tokens("你好", 0) == 2 + 12 * 2
    assert estimate_raw_tokens("abcd", 0) == (4 + 3) // 4 + 12 * 2


def test_validate_apply_decision_normalizes_ranges():
    messages = [
        HumanMessage(content="a", id="m1"),
        HumanMessage(content="b", id="m2"),
    ]
    decision = {
        "type": "compression",
        "decision": "apply",
        "ranges": [{"source_ids": ["m1"], "replacement": "  摘要 "}],
    }
    ranges, error = validate_apply_decision(decision, messages)
    assert error is None
    assert ranges == [{"source_ids": ["m1"], "replacement": "摘要"}]


def test_validate_apply_decision_rejects_bad_payloads():
    messages = [
        HumanMessage(content="a", id="m1"),
        HumanMessage(content="b", id="m2"),
    ]
    cases = [
        ({"decision": "cancel"}, "type"),
        ({"type": "compression", "decision": "cancel"}, "apply"),
        ({"type": "compression", "decision": "apply", "ranges": []}, "非空"),
        (
            {"type": "compression", "decision": "apply",
             "ranges": [{"source_ids": ["missing"], "replacement": "x"}]},
            "不存在",
        ),
        (
            {"type": "compression", "decision": "apply",
             "ranges": [
                 {"source_ids": ["m1"], "replacement": "x"},
                 {"source_ids": ["m1"], "replacement": "y"},
             ]},
            "重叠",
        ),
        (
            {"type": "compression", "decision": "apply",
             "ranges": [{"source_ids": ["m1"], "replacement": " "}]},
            "非空",
        ),
        (
            {"type": "compression", "decision": "apply",
             "ranges": [{"source_ids": ["m1"], "restore": True}]},
            "压缩块",
        ),
    ]
    for decision, keyword in cases:
        ranges, error = validate_apply_decision(decision, messages)
        assert error is not None and keyword in error, (decision, error)
        assert ranges == []


def test_validate_apply_decision_allows_tool_group_split():
    """选择自由化：拆散 tool-call 组的范围不拒绝，由 _repair_protocol 兜底。"""
    messages = [
        AIMessage(content="", id="a1", tool_calls=[{"id": "c1", "name": "t", "args": {}}]),
        ToolMessage(content="结果", id="t1", tool_call_id="c1", name="t"),
    ]
    decision = {
        "type": "compression",
        "decision": "apply",
        "ranges": [{"source_ids": ["a1"], "replacement": "x"}],
    }
    ranges, error = validate_apply_decision(decision, messages)
    assert error is None
    assert ranges == [{"source_ids": ["a1"], "replacement": "x"}]


def test_apply_plan_positions_blocks_and_restores_originals():
    messages = [
        HumanMessage(content="m1", id="m1"),
        HumanMessage(content="m2", id="m2"),
        HumanMessage(content="m3", id="m3"),
        HumanMessage(content="m4", id="m4"),
        HumanMessage(content="m5", id="m5"),
        HumanMessage(content="m6", id="m6"),
    ]
    update = _apply_plan(
        messages,
        [
            {"source_ids": ["m2", "m3", "m4"], "replacement": "summary A"},
            {"source_ids": ["m6"], "replacement": "summary B"},
        ],
    )
    rebuilt = update["messages"][1:]
    contents = [message.content for message in rebuilt]
    assert contents == ["m1", "summary A", "m5", "summary B"]
    block_a = rebuilt[1]
    assert block_a.additional_kwargs["compression"]["block_id"]
    assert block_a.additional_kwargs["compression"]["compressed_at"]
    assert [item["id"] for item in block_a.additional_kwargs["compression"]["source"]] == ["m2", "m3", "m4"]

    # 撤销：restore 将块原位展开为来源原文
    restore = _apply_plan(
        rebuilt,
        [{"source_ids": [block_a.id], "restore": True}],
    )
    restored = restore["messages"][1:]
    assert [message.content for message in restored] == ["m1", "m2", "m3", "m4", "m5", "summary B"]
    assert [message.id for message in restored[:5]] == ["m1", "m2", "m3", "m4", "m5"]
    assert restored[5].id  # 块 B 自带 id


def test_apply_plan_degrades_dangling_tool_message():
    """范围压掉 AI 调用方后，悬空 ToolMessage 文本化降级且最终协议合法。"""
    messages = [
        AIMessage(content="", id="a1", tool_calls=[{"id": "c1", "name": "t", "args": {}}]),
        ToolMessage(content="结果", id="t1", tool_call_id="c1", name="t"),
    ]
    update = _apply_plan(messages, [{"source_ids": ["a1"], "replacement": "summary"}])
    rebuilt = update["messages"][1:]
    assert rebuilt[0].content == "summary"
    degraded = rebuilt[1]
    assert isinstance(degraded, HumanMessage)
    assert degraded.id == "t1"
    assert 'role="tool"' in degraded.content and "结果" in degraded.content
    validate_messages([serialize_message(message) for message in rebuilt])


def test_apply_plan_adds_synthetic_results_for_dangling_tool_calls():
    """范围压掉 ToolMessage 后，悬空 tool_calls 补合成占位结果且最终协议合法。"""
    messages = [
        AIMessage(content="", id="a1", tool_calls=[{"id": "c1", "name": "t", "args": {}}]),
        ToolMessage(content="结果", id="t1", tool_call_id="c1", name="t"),
    ]
    update = _apply_plan(messages, [{"source_ids": ["t1"], "replacement": "summary"}])
    rebuilt = update["messages"][1:]
    assert rebuilt[0].id == "a1"
    synthetic = rebuilt[1]
    assert isinstance(synthetic, ToolMessage)
    assert synthetic.tool_call_id == "c1"
    assert synthetic.additional_kwargs["curation_synthetic"] is True
    assert rebuilt[2].content == "summary"
    validate_messages([serialize_message(message) for message in rebuilt])


def test_repair_protocol_passes_valid_lists_through():
    messages = [
        HumanMessage(content="你好", id="h1"),
        AIMessage(content="回答", id="a2"),
    ]
    assert _repair_protocol(messages) == messages


def test_strip_compression_kwargs_leaves_model_clean():
    block = HumanMessage(
        content="摘要", additional_kwargs={"compression": {"block_id": "b1"}}
    )
    plain = HumanMessage(content="原文")
    result = _strip_compression_kwargs([block, plain])
    assert result[0].additional_kwargs == {}
    assert result[1] is plain


def test_gate_end_to_end_strips_metadata_before_model():
    """真实 gate 端到端：apply 后模型收到的 messages 不含 compression 元数据。"""
    calls: list = []
    model = _recording_model(calls)
    agent = create_agent(
        model=model,
        tools=[],
        middleware=[build_compression_gate(context_window=100, threshold_ratio=0.5)],
        system_prompt="gate",
    )
    agent.checkpointer = InMemorySaver()
    original = HumanMessage(content="很长的内容" * 20, id="g1")
    config = {"configurable": {"thread_id": "gate-e2e"}, "recursion_limit": 50}
    context = {}

    async def run():
        async for _mode, _chunk in agent.astream(
            {"messages": [original]}, config=config, context=context, stream_mode=["values"]
        ):
            pass
        async for _mode, _chunk in agent.astream(
            Command(resume={"type": "compression", "decision": "apply",
                            "ranges": [{"source_ids": ["g1"], "replacement": "摘要"}]}),
            config=config, context=context, stream_mode=["values"],
        ):
            pass

    asyncio.run(run())
    assert calls, "模型应至少被调用一次"
    for call_messages in calls:
        for message in call_messages:
            assert "compression" not in (message.additional_kwargs or {}), (
                "压缩元数据不得进入模型上下文"
            )


def test_gate_cancel_suppresses_within_run_with_tool_loop():
    """含工具的多轮模型调用中，cancel 后同一 run 不再触发压缩门。"""
    from langchain_core.language_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import tool as make_tool

    @make_tool
    def noop(query: str) -> str:
        """无操作工具。"""
        return "done"

    class ToolThenAnswerModel(BaseChatModel):
        """首轮返回工具调用、其后返回最终回答的 fake model。"""

        def __init__(self, calls: list) -> None:
            super().__init__()
            self._calls = calls

        @property
        def _llm_type(self) -> str:
            return "tool-then-answer"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self._calls.append(list(messages))
            if len(self._calls) == 1:
                return ChatResult(generations=[ChatGeneration(message=AIMessage(
                    content="",
                    tool_calls=[{"name": "noop", "args": {"query": "x"}, "id": "call-1"}],
                ))])
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="回答"))])

    calls: list = []
    model = ToolThenAnswerModel(calls)
    agent = create_agent(
        model=model,
        tools=[noop],
        middleware=[build_compression_gate(context_window=100, threshold_ratio=0.5)],
        system_prompt="gate",
    )
    agent.checkpointer = InMemorySaver()
    original = HumanMessage(content="很长的内容" * 20, id="g2")
    config = {"configurable": {"thread_id": "gate-cancel", "run_id": "run-cancel"}, "recursion_limit": 50}
    context = {}

    async def run():
        chunks = []
        async for _mode, chunk in agent.astream(
            {"messages": [original]}, config=config, context=context, stream_mode=["values"]
        ):
            chunks.append(chunk)
        assert "__interrupt__" in chunks[-1]
        final = []
        async for _mode, chunk in agent.astream(
            Command(resume={"type": "compression", "decision": "cancel"}),
            config=config, context=context, stream_mode=["values"],
        ):
            final.append(chunk)
        assert all("__interrupt__" not in chunk for chunk in final), "cancel 后同 run 不得再中断"
        return final

    final = asyncio.run(run())
    assert final[-1]["messages"][0].content == original.content
    assert len(calls) >= 2, "含工具的 run 应发生多轮模型调用"
    assert final[-1]["messages"][-1].content == "回答"


# === 桌面压缩模块单测（tasks 4.9）===

def test_format_transcript_skips_synthetic_and_strips_metadata():
    from backend.app.desktop.compression import _format_transcript

    text = _format_transcript([
        {"role": "human", "content": "讨论了数据库方案"},
        {"role": "tool", "name": "read_file", "content": "占位结果", "curation_synthetic": True},
        {"role": "ai", "content": "最终决定使用 SQLite", "compression": {"block_id": "b"}},
    ])
    assert "讨论了数据库方案" in text
    assert "占位结果" not in text
    assert "SQLite" in text


def test_summarize_messages_returns_candidate(monkeypatch):
    from backend.app.desktop import compression as module

    seen = []

    class FakeModel:
        async def ainvoke(self, messages):
            seen.append(messages)
            return AIMessage(content="候选摘要")

    monkeypatch.setattr(module, "create_chat_model", lambda name, app_config: FakeModel())
    result = asyncio.run(module.summarize_messages(
        [{"role": "human", "content": "做数据库方案"}], None, None
    ))
    assert result == "候选摘要"
    assert seen
    assert "数据库方案" in str(seen[0][-1].content)


def test_summarize_messages_rejects_empty_range(monkeypatch):
    from backend.app.desktop import compression as module

    class FakeModel:
        async def ainvoke(self, messages):
            return AIMessage(content="不应被调用")

    monkeypatch.setattr(module, "create_chat_model", lambda name, app_config: FakeModel())
    with pytest.raises(ValueError):
        asyncio.run(module.summarize_messages(
            [{"role": "human", "content": "  "}], None, None
        ))


def test_compression_recovery_payload_projects_interrupt():
    from types import SimpleNamespace

    from backend.app.desktop.compression import compression_recovery_payload

    class FakeInterrupt:
        def __init__(self, value):
            self.value = value

    checkpoint = SimpleNamespace(pending_writes=[
        ("t1", "__interrupt__", [FakeInterrupt({"type": "commitment_review", "stage": 3})]),
        ("t2", "__interrupt__", [FakeInterrupt({"type": "compression_request", "usage": 118000})]),
    ])
    checkpointer = SimpleNamespace()

    async def aget_tuple(config):
        return checkpoint

    checkpointer.aget_tuple = aget_tuple

    class FakeSession:
        async def scalar(self, *args, **kwargs):
            return None

    task = SimpleNamespace(task_id="task-1", thread_id="th-1")
    result = asyncio.run(compression_recovery_payload(FakeSession(), task, checkpointer))
    assert result is not None
    assert result["status"] == "orphaned"
    assert result["request"]["usage"] == 118000


def test_compression_recovery_payload_none_without_compression_interrupt():
    from types import SimpleNamespace

    from backend.app.desktop.compression import compression_recovery_payload

    class FakeInterrupt:
        def __init__(self, value):
            self.value = value

    checkpoint = SimpleNamespace(pending_writes=[
        ("t1", "__interrupt__", [FakeInterrupt({"type": "commitment_review", "stage": 3})]),
    ])
    checkpointer = SimpleNamespace()

    async def aget_tuple(config):
        return checkpoint

    checkpointer.aget_tuple = aget_tuple

    class FakeSession:
        async def scalar(self, *args, **kwargs):
            return None

    task = SimpleNamespace(task_id="task-1", thread_id="th-1")
    result = asyncio.run(compression_recovery_payload(FakeSession(), task, checkpointer))
    assert result is None


def test_apply_plan_delete_leaves_tombstone_with_source():
    """delete 范围生成删除墓碑（来源保留、模型不可见），最终协议合法。"""
    messages = [
        HumanMessage(content="m1", id="m1"),
        HumanMessage(content="m2", id="m2"),
        HumanMessage(content="m3", id="m3"),
    ]
    update = _apply_plan(messages, [{"source_ids": ["m2"], "delete": True}])
    rebuilt = update["messages"][1:]
    assert [message.content for message in rebuilt] == ["m1", "", "m3"]
    tombstone = rebuilt[1]
    assert tombstone.additional_kwargs["compression"]["deleted"] is True
    assert [item["id"] for item in tombstone.additional_kwargs["compression"]["source"]] == ["m2"]
    validate_messages([serialize_message(message) for message in rebuilt])


def test_strip_compression_kwargs_drops_deleted_tombstones():
    """删除墓碑不进入模型请求；普通块仅剥离元数据。"""
    tombstone = HumanMessage(
        content="",
        id="t1",
        additional_kwargs={"compression": {"block_id": "t1", "deleted": True, "source": []}},
    )
    block = HumanMessage(content="摘要", additional_kwargs={"compression": {"block_id": "b1"}})
    plain = HumanMessage(content="原文")
    result = _strip_compression_kwargs([tombstone, block, plain])
    assert [message.content for message in result] == ["摘要", "原文"]
    assert result[0].additional_kwargs == {}
    assert result[1] is plain


def test_validate_apply_decision_accepts_delete_ranges():
    messages = [HumanMessage(content="a", id="m1"), HumanMessage(content="b", id="m2")]
    decision = {
        "type": "compression",
        "decision": "apply",
        "ranges": [{"source_ids": ["m1"], "delete": True}],
    }
    ranges, error = validate_apply_decision(decision, messages)
    assert error is None
    assert ranges == [{"source_ids": ["m1"], "delete": True}]
    bad = {
        "type": "compression",
        "decision": "apply",
        "ranges": [{"source_ids": ["m1"], "delete": True, "replacement": "x"}],
    }
    _ranges, error = validate_apply_decision(bad, messages)
    assert error is not None and "delete" in error


def test_restore_source_with_dangling_tool_message_is_repaired():
    """恢复来源切片以悬空 ToolMessage 开头时不报错，经统一修复后协议合法。"""
    messages = [
        AIMessage(content="", id="a1", tool_calls=[{"id": "c1", "name": "t", "args": {}}]),
        HumanMessage(content="", id="b1", additional_kwargs={"compression": {
            "block_id": "b1",
            "source": [
                {"role": "tool", "content": "结果", "id": "t1", "tool_call_id": "c1", "name": "t"},
                {"role": "human", "content": "后续", "id": "h1"},
            ],
        }}),
    ]
    update = _apply_plan(messages, [{"source_ids": ["b1"], "restore": True}])
    rebuilt = update["messages"][1:]
    assert [message.content for message in rebuilt] == ["", "结果", "后续"]
    # 修复后全量协议合法
    validate_messages([serialize_message(message) for message in rebuilt])
