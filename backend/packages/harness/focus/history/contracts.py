"""本文件对外提供 FocusItem、HistoryPayload 与稳定内容身份函数。

输入为宿主确定的来源、作用域和 typed payload；输出为严格版本化历史合同与 canonical hash。
具体工作流为校验 envelope、保留 Provider 原始载荷、按稳定 JSON 计算身份并拒绝未知 schema。
示例：HistoryPayload(execution_items=(FocusItem(item_id="i1", kind="message", payload={}),))。
user_authored 表示用户编写而非消息角色；authored_instruction 表示有序行为指令，新增语义选择版本不重写旧 HistoryPayload/hash。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Origin = Literal["direct_user", "user_authored", "curator", "delegated", "runtime", "provider", "tool", "collaborator", "legacy_unknown"]
Scope = Literal["revision", "execution", "runtime", "run", "round"]
SemanticPolicy = Literal["index", "evidence_only", "reference_only", "exclude"]
ItemKind = Literal[
    "message", "authored_instruction", "reasoning", "function_call", "function_call_output", "custom_tool_call",
    "custom_tool_call_output", "world_state_update", "selected_context", "agent_collaboration",
    "round_decision_context", "task_contract", "compaction", "projection_repair", "unknown",
]


def content_hash(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class FocusItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str = Field(min_length=1)
    kind: ItemKind
    origin: Origin = "legacy_unknown"
    scope: Scope = "execution"
    payload: dict[str, Any]
    source_refs: tuple[dict[str, Any], ...] = ()
    message_id: str | None = None
    provider: str | None = None
    projection_version: str | None = None
    bridge: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_payload(self):
        if self.kind in {"world_state_update", "round_decision_context", "projection_repair"} and self.origin != "runtime":
            raise ValueError("控制 Item 必须来自宿主 runtime")
        if self.origin == "direct_user" and self.kind not in {"message", "task_contract"}:
            raise ValueError("Provider 或控制 Item 不可提升为直接用户输入")
        if "message" in self.payload:
            if not isinstance(self.payload["message"], dict):
                raise ValueError("message bridge 必须是消息对象")
            if self.origin == "direct_user" and self.payload["message"].get("role") not in {"human", "user"}:
                raise ValueError("非用户消息不可提升为直接用户输入")
            return self
        if self.kind in {"function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output"}:
            if not isinstance(self.payload.get("call_id"), str) or not self.payload["call_id"]:
                raise ValueError("原生工具 Item 必须声明 call_id")
        return self


class HistoryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    authored_items: tuple[FocusItem, ...] = ()
    execution_items: tuple[FocusItem, ...] = ()
    binding_refs: tuple[dict[str, Any], ...] = ()
    selection_version: str = "focus-semantic-selection-v2"

    @model_validator(mode="after")
    def validate_inventories(self):
        for items in (self.authored_items, self.execution_items):
            if len({item.item_id for item in items}) != len(items):
                raise ValueError("history Item identity 重复")
        if any(item.origin == "runtime" or item.scope in {"runtime", "round", "run"}
               or item.kind in {"reasoning", "unknown", "compaction"} for item in self.authored_items):
            raise ValueError("authored history 不可含运行控制或 opaque Provider continuity")
        return self
