r"""本文件对外提供 FactIdentity 与 FactIdentityResolver 的确定性事实身份规则。

输入为 Loop、事实类型、语义 subject 和一次观察的稳定 source key；输出为规范化 subject、identity key 与 fact_id。
具体工作流为折叠大小写和空白形成可比较 subject，再对版本化 canonical tuple 计算稳定 SHA-256 截断标识；相同输入在
实时投影、恢复和重建中始终得到相同结果。示例：`FactIdentityResolver.resolve("l1", "test", "pytest", "run-1:m1")`。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import unicodedata


_SPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class FactIdentity:
    fact_id: str
    identity_key: str
    normalized_subject: str


class FactIdentityResolver:
    VERSION = "fact-v1"

    @classmethod
    def resolve(cls, loop_id: str, fact_type: str, subject: str, source_key: str) -> FactIdentity:
        normalized = cls.normalize_subject(subject)
        canonical = "\x1f".join((cls.VERSION, loop_id, fact_type.casefold(), normalized, source_key))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return FactIdentity(fact_id=digest[:32], identity_key=digest, normalized_subject=normalized)

    @staticmethod
    def normalize_subject(subject: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(subject)).strip().casefold()
        return _SPACE.sub(" ", normalized)
