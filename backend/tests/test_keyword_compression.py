"""focus.agents.compression 关键字快捷压缩单测（f32）。

覆盖机械命中、范围组装、关键词范围编译（块来源保留）与禁提词摘要提示词。
"""

from langchain_core.messages import AIMessage, HumanMessage

from backend.app.desktop.compression import (
    _SUMMARY_SYSTEM_PROMPT,
    _build_summary_prompt,
)
from focus.agents.compression import (
    apply_compression_ranges,
    build_keyword_ranges,
    hit_keyword_message_ids,
)
from focus.agents.compression.gate import _strip_compression_kwargs
from focus.runtime.runs.events import serialize_message, validate_messages


def test_hit_keyword_message_ids_hits_and_case():
    messages = [
        HumanMessage(content="我们讨论过胡萝卜方案，直接不考虑", id="h1"),
        HumanMessage(content="今天关注方向 A", id="h2"),
    ]
    assert hit_keyword_message_ids(messages, "胡萝卜") == ["h1"]
    # 大小写不敏感默认；敏感时精确匹配
    assert hit_keyword_message_ids(messages, "胡萝卜", case_sensitive=True) == ["h1"]
    assert hit_keyword_message_ids(messages, "carrot") == []


def test_hit_keyword_message_ids_cases_sensitive():
    messages = [HumanMessage(content="Carrot 是关键词", id="c1"), HumanMessage(content="carrot", id="c2")]
    assert hit_keyword_message_ids(messages, "carrot", case_sensitive=True) == ["c2"]
    assert hit_keyword_message_ids(messages, "carrot", case_sensitive=False) == ["c1", "c2"]


def test_hit_keyword_message_ids_content_blocks():
    messages = [
        HumanMessage(content=[{"type": "text", "text": "这里埋着胡萝卜"}], id="b1"),
        HumanMessage(content="无关内容", id="b2"),
    ]
    assert hit_keyword_message_ids(messages, "胡萝卜") == ["b1"]


def test_hit_keyword_message_ids_no_keyword():
    assert hit_keyword_message_ids([], "胡萝卜") == []


def test_build_keyword_ranges_group_all_and_subset():
    assert build_keyword_ranges(["a", "b", "c"], group_all=True) == [{"source_ids": ["a", "b", "c"]}]
    assert build_keyword_ranges(["a", "b"], group_all=False) == [
        {"source_ids": ["a"]},
        {"source_ids": ["b"]},
    ]
    assert build_keyword_ranges([], group_all=True) == []
    # 去重保序
    assert build_keyword_ranges(["a", "a", "b"], group_all=True) == [{"source_ids": ["a", "b"]}]


def test_apply_compression_ranges_from_keyword_builds_block_keeps_others():
    """关键词命中范围编译为块：命中消息替换、未命中保留、块携带来源元数据。"""
    messages = [
        HumanMessage(content="提到胡萝卜的旧事", id="h1"),
        HumanMessage(content="需要保留的关键信息", id="h2"),
    ]
    ranges = [{"source_ids": hit_keyword_message_ids(messages, "胡萝卜"), "replacement": "旧方向摘要"}]
    update = apply_compression_ranges(messages, ranges)
    rebuilt = update["messages"][1:]

    # 命中消息被块替换，未命中保留
    block = next(m for m in rebuilt if m.id != "h2")
    assert block.content == "旧方向摘要"
    assert any(m.content == "需要保留的关键信息" for m in rebuilt)
    # 块携带来源元数据，模型调用前剥离
    assert block.additional_kwargs["compression"]["source"]
    stripped = _strip_compression_kwargs(rebuilt)
    assert "compression" not in stripped[0].additional_kwargs
    # 协议合法
    validate_messages([serialize_message(m) for m in rebuilt])


def test_apply_compression_ranges_subset_selects_only_chosen():
    """粒度：仅压缩用户选中的命中消息，未选中命中消息保留原文。"""
    messages = [
        HumanMessage(content="含胡萝卜 m1", id="h1"),
        HumanMessage(content="含胡萝卜 m2", id="h2"),
        HumanMessage(content="不含目标", id="h3"),
    ]
    # 用户只选 h2 压缩（default 组合为单条 range 也可，这里显式传子集）
    update = apply_compression_ranges(
        messages, [{"source_ids": ["h2"], "replacement": "子集摘要"}]
    )
    rebuilt = update["messages"][1:]
    contents = [m.content for m in rebuilt]
    assert "子集摘要" in contents
    assert "含胡萝卜 m1" in contents  # 未选中的命中消息保留原文
    assert "不含目标" in contents


def test_apply_compression_ranges_delete_tombstone():
    """delete 范围生成删除墓碑：模型不可见、来源保留。"""
    messages = [
        HumanMessage(content="这里含胡萝卜且需抹除", id="h1"),
        HumanMessage(content="保留", id="h2"),
    ]
    update = apply_compression_ranges(messages, [{"source_ids": ["h1"], "delete": True}])
    rebuilt = update["messages"][1:]
    tombstone = next(m for m in rebuilt if m.id != "h2")
    assert tombstone.content == ""
    assert tombstone.additional_kwargs["compression"]["deleted"] is True
    assert tombstone.additional_kwargs["compression"]["source"]
    validate_messages([serialize_message(m) for m in rebuilt])


def test_build_summary_prompt_forbid_terms():
    """禁提词条款仅在非空 forbid_terms 时追加。"""
    assert _build_summary_prompt() == _SUMMARY_SYSTEM_PROMPT
    prompt = _build_summary_prompt(["胡萝卜"])
    assert "明确禁止出现" in prompt
    assert "「胡萝卜」" in prompt
