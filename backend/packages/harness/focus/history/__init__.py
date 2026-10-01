"""本文件对外提供 Focus history 的稳定公开接口。

输入为消息或 typed 历史；输出为无损编码、语义选择和严格协议合同。
具体工作流为由 codec、selection 和 protocol 模块独立承担；不依赖 Desktop ORM 或 Provider HTTP。
示例：from focus.history import HistoryPayload, messages_to_items。
"""

from focus.history.codec import (
    deserialize_history_message, deserialize_history_messages, items_to_messages,
    history_records, legacy_to_items, messages_to_items, serialize_history_message,
)
from focus.history.contracts import FocusItem, HistoryPayload, content_hash
from focus.history.protocol import validate_items
from focus.history.selection import SELECTION_VERSION, authored_items, semantic_messages, semantic_policy

__all__ = [
    "FocusItem", "HistoryPayload", "content_hash", "deserialize_history_message",
    "deserialize_history_messages", "items_to_messages", "legacy_to_items", "messages_to_items",
    "serialize_history_message", "history_records", "validate_items", "SELECTION_VERSION", "authored_items",
    "semantic_messages", "semantic_policy",
]
