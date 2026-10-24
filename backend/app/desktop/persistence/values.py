r"""
本文件对外提供 normalize_json_storage_value，作为桌面领域写入 PostgreSQL JSONB/text 前的
统一值规范化边界。输入为任意 JSON-like 值；输出保留 tab/换行/回车并将其余 C0 控制字符
替换为 U+FFFD 的可存储副本。示例：`safe = normalize_json_storage_value(payload)`。
"""

from __future__ import annotations

from typing import Any


def normalize_json_storage_value(value: Any) -> Any:
    if isinstance(value, str):
        return "".join(
            char if char in "\t\n\r" or ord(char) >= 32 else "\ufffd"
            for char in value
        )
    if isinstance(value, dict):
        return {
            str(normalize_json_storage_value(key)): normalize_json_storage_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [normalize_json_storage_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return normalize_json_storage_value(str(value))
