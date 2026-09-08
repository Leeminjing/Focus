r"""
本文件对外提供 CurationSourceProjector。

输入为某个已提交根 checkpoint 的通用序列化消息；输出为只含稳定身份、可见正文和必要
工具证据的 CurationSourceSnapshot。具体工作流为排除内部 reasoning/运行元数据/既有策展
合成消息与协议占位，规范化 JSONB 不安全控制字符，并对 canonical JSON 计算稳定哈希。
示例：`snapshot = CurationSourceProjector().project("cp-1", messages)`。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from backend.app.desktop.storage_values import normalize_json_storage_value

from .contract import CurationSourceMessage, CurationSourceSnapshot


_ROLE_MAP = {"user": "human", "assistant": "ai"}
_ALLOWED_ROLES = frozenset({"human", "ai", "system", "tool"})


class CurationSourceProjector:
    def project(
        self,
        source_checkpoint_id: str,
        source_messages: list[dict[str, Any]],
    ) -> CurationSourceSnapshot:
        projected: list[CurationSourceMessage] = []
        seen_ids: set[str] = set()
        for index, raw in enumerate(source_messages):
            if raw.get("curation_synthetic") or raw.get("curation_source_message_ids"):
                continue
            role = _ROLE_MAP.get(str(raw.get("role") or ""), str(raw.get("role") or ""))
            if role not in _ALLOWED_ROLES:
                continue
            safe_content = normalize_json_storage_value(raw.get("content", ""))
            raw_id = normalize_json_storage_value(str(raw.get("id") or ""))
            source_id = raw_id or self._derived_id(index, role, safe_content)
            if source_id in seen_ids:
                source_id = self._derived_id(index, role, [source_id, safe_content])
            seen_ids.add(source_id)
            projected.append(CurationSourceMessage(
                source_message_id=source_id,
                role=role,
                content=safe_content if isinstance(safe_content, (str, list)) else str(safe_content),
                tool_calls=self._tool_calls(raw.get("tool_calls")),
                tool_call_id=self._optional_text(raw.get("tool_call_id")),
                name=self._optional_text(raw.get("name")),
                status=self._optional_text(raw.get("status")),
            ))

        canonical_messages = [item.model_dump(mode="json") for item in projected]
        canonical = json.dumps(
            {"source_checkpoint_id": source_checkpoint_id, "messages": canonical_messages},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return CurationSourceSnapshot(
            source_checkpoint_id=source_checkpoint_id,
            messages=projected,
            projection_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _derived_id(index: int, role: str, content: Any) -> str:
        raw = json.dumps([index, role, content], ensure_ascii=False, sort_keys=True, default=str)
        return f"focus-source-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]}"

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        if value is None:
            return None
        normalized = normalize_json_storage_value(str(value))
        return normalized or None

    @staticmethod
    def _tool_calls(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, Any]] = []
        for raw in value:
            if not isinstance(raw, dict):
                continue
            call_id = normalize_json_storage_value(str(raw.get("id") or ""))
            name = normalize_json_storage_value(str(raw.get("name") or ""))
            if not call_id or not name:
                continue
            args = normalize_json_storage_value(raw.get("args") or {})
            result.append({"id": call_id, "name": name, "args": args if isinstance(args, dict) else {}})
        return result
