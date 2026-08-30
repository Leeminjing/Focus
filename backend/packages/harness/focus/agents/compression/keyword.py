"""本文件对外提供关键字快捷压缩的机械命中、范围组装与机械剥离纯函数。

对外提供:
    hit_keyword_message_ids — 用子串匹配找出含某关键词的消息 id 列表
    build_keyword_ranges — 把选中的消息 id 组装为压缩范围列表
    scrub_message_contents — 机械剥离消息文本中的禁用词（先抠词再压缩）

输入:
    hit_keyword_message_ids(messages, keyword, case_sensitive): messages 为
        BaseMessage 或前端消息 dict 的列表；keyword 为要命中的关键词；
        case_sensitive 为是否区分大小写
    build_keyword_ranges(selected_ids, group_all): selected_ids 为选中的消息 id 列表；
        group_all 为 True 时合并为单条 range，否则按 id 拆成多条 range
    scrub_message_contents(messages, terms): messages 为 BaseMessage 列表；
        terms 为明文禁止出现的词序列（可空）

输出:
    hit_keyword_message_ids → list[str]（命中的消息 id，含 BaseMessage.id / dict["id"]）
    build_keyword_ranges → list[dict]（与既有 validate_apply_decision 兼容的 range 列表）
    scrub_message_contents → list[BaseMessage]（内容已机械去除禁用词的新消息，id/role 不变）

具体工作流:
    (1) _message_text 从 BaseMessage / dict 提取 content 文本（str 或内容块列表兼容）
    (2) hit_keyword_message_ids 对每条消息做子串匹配（默认大小写不敏感），返回命中 id
    (3) build_keyword_ranges 将选中 id 组装为 {source_ids} 单条或多条互不重叠范围
    (4) scrub_message_contents 对每条消息的 content（str 或内容块）机械去除禁用词

示例:
    hits = hit_keyword_message_ids(messages, "胡萝卜")
    ranges = build_keyword_ranges(hits, group_all=True)
    clean = scrub_message_contents(messages, ["胡萝卜"])
"""

from __future__ import annotations

from typing import Any


def _message_text(message: Any) -> str:
    """从 BaseMessage / dict 提取 content 文本，str 与内容块列表兼容。"""
    content = (
        message.get("content", "")
        if isinstance(message, dict)
        else getattr(message, "content", "")
    )
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            if isinstance(item, dict)
            else ""
            for item in content
        )
    return str(content)


def hit_keyword_message_ids(
    messages: list[Any], keyword: str, case_sensitive: bool = False
) -> list[str]:
    """用子串匹配找出含某关键词的消息 id 列表（确定性、非 LLM）。

    输入:
        messages: list — BaseMessage 或前端消息 dict 列表
        keyword: str — 要命中的关键词（非空）
        case_sensitive: bool — 是否区分大小写，默认 False

    输出:
        list[str] — 命中消息的 id（无 id 的消息忽略）
    """
    if not keyword:
        return []
    needle = keyword if case_sensitive else keyword.lower()
    hits: list[str] = []
    for message in messages:
        message_id = (
            message.get("id")
            if isinstance(message, dict)
            else getattr(message, "id", None)
        )
        if not message_id:
            continue
        text = _message_text(message)
        haystack = text if case_sensitive else text.lower()
        if needle in haystack:
            hits.append(message_id)
    return hits


def build_keyword_ranges(
    selected_ids: list[str], group_all: bool = True
) -> list[dict[str, Any]]:
    """把选中的消息 id 组装为兼容既有校验的压缩范围列表。

    输入:
        selected_ids: list[str] — 用户选中的消息 id
        group_all: bool — True 合并为单条 range，False 拆成多条（每 id 一条）

    输出:
        list[dict] — [{source_ids: [...]}, ...]，空选择返回空列表

    具体工作流:
        group_all=True 时单条 range 覆盖全部选中；否则每个 id 独立一条 range。
        结果可直接交 validate_apply_decision / apply_compression_ranges 使用。
    """
    if not selected_ids:
        return []
    if group_all:
        return [{"source_ids": list(dict.fromkeys(selected_ids))}]
    return [{"source_ids": [message_id]} for message_id in dict.fromkeys(selected_ids)]


def _scrub_text(text: Any, terms: tuple[str, ...] | list[str]) -> Any:
    """机械去除 str 或内容块列表中的禁用词，保持原结构（str 仍 str，list 仍 list）。"""
    for term in dict.fromkeys(terms or ()):
        if not term:
            continue
        if isinstance(text, str):
            text = text.replace(term, "")
        elif isinstance(text, list):
            text = [
                item.replace(term, "")
                if isinstance(item, str)
                else (dict(item, text=item.get("text", "").replace(term, "")) if isinstance(item, dict) and "text" in item else item)
                for item in text
            ]
    return text


def scrub_message_contents(
    messages: list[Any], terms: tuple[str, ...] | list[str] | None
) -> list[Any]:
    """机械剥离消息文本中的禁用词（先抠词再压缩）。

    输入:
        messages: list — BaseMessage 列表
        terms: 序列 — 明文禁止出现的词（可空，空则原样返回）

    输出:
        list — 内容已去除禁用词的消息副本，id/role/tool_calls 等保持不变

    具体工作流:
        对每条消息的 content（str 或内容块列表）调用 _scrub_text 机械去除禁用词，
        经 model_copy / dict 复制返回新列表；terms 为空时不做任何改动。
    """
    if not terms:
        return messages
    result: list[Any] = []
    for message in messages:
        content = getattr(message, "content", None)
        scrubbed = _scrub_text(content, terms)
        if isinstance(message, dict):
            new_message = dict(message)
            new_message["content"] = scrubbed
            result.append(new_message)
        else:
            result.append(message.model_copy(update={"content": scrubbed}))
    return result
