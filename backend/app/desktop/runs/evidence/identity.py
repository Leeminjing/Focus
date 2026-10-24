"""本文件对外提供 canonical_hash 和 result_identity 两个纯身份函数。

输入为 JSON 内容或领域 kind/id/version；输出为稳定 SHA-256。
具体工作流为排序序列化后哈希，不读取数据库或展示/记忆状态。
示例：result_identity("test", "run:test", canonical_hash({"passed": 41}))。
"""

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def canonical_hash(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def result_identity(kind: str, source_id: str, version: str) -> str:
    return canonical_hash([kind, source_id, version])
