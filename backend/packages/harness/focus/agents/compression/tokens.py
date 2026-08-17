"""本文件对外提供压缩门的 token 估算函数。

对外提供:
    estimate_raw_tokens — 对拼接文本做 CJK=1、其余字符÷4、每条消息 +12 的启发式估算
    estimate_messages_tokens — 对 messages（dict 或 BaseMessage 列表）做整体估算

输入:
    estimate_raw_tokens: raw: str — 拼接后的原始文本；message_count: int — 消息条数
    estimate_messages_tokens: messages — dict 或 BaseMessage 列表

输出:
    int — 估算的 token 数（与桌面 estimate_tokens 同公式，仅用于触发判定与界面展示）

具体工作流:
    (1) estimate_raw_tokens 统计 CJK 字符与非空白其他字符并套用启发式
    (2) estimate_messages_tokens 从每条消息提取 content 文本（str/list 内容块兼容）后拼接估算

示例:
    usage = estimate_messages_tokens(state["messages"])
"""

from typing import Any


def estimate_raw_tokens(raw: str, message_count: int) -> int:
    cjk = sum(1 for char in raw if "㐀" <= char <= "鿿")
    other = sum(
        1 for char in raw
        if not char.isspace() and not ("㐀" <= char <= "鿿")
    )
    return cjk + (other + 3) // 4 + 12 * (message_count + 2)


def _message_text(message: Any) -> str:
    content = message.get("content", "") if isinstance(message, dict) else getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            if isinstance(item, dict)
            else ""
            for item in content
        )
    return str(content)


def estimate_messages_tokens(messages: list[Any]) -> int:
    raw = "\n".join(_message_text(message) for message in messages)
    return estimate_raw_tokens(raw, len(messages))
