"""本文件对外提供 Patrol 内部自动压缩与工具协议修复单测。

输入为 create_agent 装配的极小 Agent 图与 InMemorySaver；输出为对 before_model 内
interrupt 暂停/恢复语义的断言。具体工作流为：以超小阈值触发压缩门中断，断言 values
chunk 含 __interrupt__ 且 messages 不变；再以内部 Command(resume=...) 提交 apply，
断言摘要应用、原文隔离与工具协议修复。示例：
`py -3.12 -m pytest backend/tests/test_compression_gate.py -v`
"""

import asyncio
import itertools

import pytest
from focus.agents.compression.gate import (
    _strip_compression_kwargs,
    apply_compression_ranges,
    build_compression_gate,
)
from focus.agents.compression.schemas import validate_apply_decision
from focus.history.repair import repair_tool_messages
from focus.messages import estimate_raw_tokens
from focus.runtime.runs.events import serialize_message, validate_messages
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command


def _recording_model(calls: list) -> GenericFakeChatModel:
    """构造记录每次模型调用 messages 的 fake model（闭包收集，绕开 pydantic 字段限制）。"""

    class RecordingModel(GenericFakeChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            calls.append(list(messages))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    return RecordingModel(messages=itertools.cycle([AIMessage(content="回答")]))














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
            "仅支持",
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


def test_apply_plan_positions_automatic_summary_blocks():
    messages = [
        HumanMessage(content="m1", id="m1"),
        HumanMessage(content="m2", id="m2"),
        HumanMessage(content="m3", id="m3"),
        HumanMessage(content="m4", id="m4"),
        HumanMessage(content="m5", id="m5"),
        HumanMessage(content="m6", id="m6"),
    ]
    update = apply_compression_ranges(
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



def test_apply_plan_degrades_dangling_tool_message():
    """范围压掉 AI 调用方后，悬空 ToolMessage 文本化降级且最终协议合法。"""
    messages = [
        AIMessage(content="", id="a1", tool_calls=[{"id": "c1", "name": "t", "args": {}}]),
        ToolMessage(content="结果", id="t1", tool_call_id="c1", name="t"),
    ]
    update = apply_compression_ranges(messages, [{"source_ids": ["a1"], "replacement": "summary"}])
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
    update = apply_compression_ranges(messages, [{"source_ids": ["t1"], "replacement": "summary"}])
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
    assert repair_tool_messages(messages, cause="compression") == messages


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




# === 桌面压缩模块单测（tasks 4.9）===

def test_format_transcript_skips_synthetic_and_strips_metadata():
    from backend.app.desktop.conversations.compression import _format_transcript

    text = _format_transcript([
        {"role": "human", "content": "讨论了数据库方案"},
        {"role": "tool", "name": "read_file", "content": "占位结果", "curation_synthetic": True},
        {
            "role": "ai",
            "content": "可见策展总结",
            "curation_synthetic": True,
            "curation_source_message_ids": ["source-1"],
        },
        {"role": "ai", "content": "最终决定使用 SQLite", "reasoning_content": "不可进入摘要的内部思考", "compression": {"block_id": "b"}},
    ])
    assert "讨论了数据库方案" in text
    assert "占位结果" not in text
    assert "可见策展总结" in text
    assert "SQLite" in text
    assert "内部思考" not in text


def test_summarize_messages_returns_candidate(monkeypatch):
    from backend.app.desktop.conversations import compression as module

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
    from backend.app.desktop.conversations import compression as module

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

    from backend.app.desktop.conversations.compression import (
        compression_recovery_payload,
    )

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

    from backend.app.desktop.conversations.compression import (
        compression_recovery_payload,
    )

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
