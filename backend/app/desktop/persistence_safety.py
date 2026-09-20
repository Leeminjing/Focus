r"""本文件对外提供 PersistencePayloadNormalizer 与 NormalizedPayload 公共持久化边界。

输入为即将写入 PostgreSQL text/JSON/JSONB 的任意嵌套值和来源标签；输出为数据库安全的 JSON 兼容值、受影响路径、
替换数量与原始逻辑值摘要。具体工作流为先稳定序列化计算摘要，再递归遍历 mapping/sequence/string 叶节点，
把 NUL 和未配对 surrogate 编码成可见转义并记录路径。示例：`safe = PersistencePayloadNormalizer.normalize(payload, "journal")`。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


@dataclass(frozen=True, slots=True)
class NormalizedPayload:
    value: Any
    source: str
    affected_paths: tuple[str, ...]
    replacement_count: int
    original_digest: str

    def metadata(self) -> dict[str, Any]:
        return {
            "normalized": self.replacement_count > 0,
            "source": self.source,
            "affected_paths": list(self.affected_paths),
            "replacement_count": self.replacement_count,
            "original_digest": self.original_digest,
        }


class PersistencePayloadNormalizer:
    @classmethod
    def normalize(cls, value: Any, source: str) -> NormalizedPayload:
        digest = hashlib.sha256(cls._stable_bytes(value)).hexdigest()
        paths: list[str] = []
        normalized, count = cls._normalize_value(value, "$", paths)
        return NormalizedPayload(
            value=normalized,
            source=source,
            affected_paths=tuple(paths),
            replacement_count=count,
            original_digest=digest,
        )

    @staticmethod
    def _stable_bytes(value: Any) -> bytes:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return serialized.encode("utf-8", errors="surrogatepass")

    @classmethod
    def _normalize_value(
        cls,
        value: Any,
        path: str,
        paths: list[str],
    ) -> tuple[Any, int]:
        if isinstance(value, str):
            return cls._normalize_text(value, path, paths)
        if isinstance(value, Mapping):
            output: dict[str, Any] = {}
            replacements = 0
            for key, item in value.items():
                key_text = str(key)
                child, count = cls._normalize_value(item, cls._child_path(path, key_text), paths)
                output[key_text] = child
                replacements += count
            return output, replacements
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            output = []
            replacements = 0
            for index, item in enumerate(value):
                child, count = cls._normalize_value(item, f"{path}[{index}]", paths)
                output.append(child)
                replacements += count
            return output, replacements
        if isinstance(value, (bytes, bytearray)):
            return value.hex(), 0
        if value is None or isinstance(value, (bool, int, float)):
            return value, 0
        return str(value), 0

    @staticmethod
    def _normalize_text(value: str, path: str, paths: list[str]) -> tuple[str, int]:
        replacements = 0
        output: list[str] = []
        for character in value:
            codepoint = ord(character)
            if codepoint == 0 or 0xD800 <= codepoint <= 0xDFFF:
                output.append(f"\\u{codepoint:04x}")
                replacements += 1
            else:
                output.append(character)
        if replacements:
            paths.append(path)
        return "".join(output), replacements

    @staticmethod
    def _child_path(parent: str, key: str) -> str:
        if key.replace("_", "").isalnum():
            return f"{parent}.{key}"
        return f"{parent}[{json.dumps(key, ensure_ascii=False)}]"
