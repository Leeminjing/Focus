r"""
本文件对外提供 Context 策展的类型化输入、自由计划与确定性合法消息编译器。

输入为 CurationSourceProjector 产生的稳定证据快照、冻结策略、当前已发布定义和模型返回的
CuratedContextPlan；输出为版本化模型 envelope、完整 authored_messages 与多对多来源审计。
具体工作流为严格解析判别联合，按计划顺序生成普通消息或原子工具交换，由系统分配稳定
消息/调用 ID，并验证所有工具结果与来源证据一致。示例：
`compiled = compile_curated_context(plan, snapshot)`。

另对外提供 `estimate_curation_tokens`，按与压缩触发判定相同的口径折算策展输入的用量：
内联图像载荷先替换为占位符再计入文本口径，图片本身按其尺寸单独折算。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.desktop.models import ContextCurationPolicy
from focus.messages import (
    estimate_images_tokens,
    estimate_raw_tokens,
    strip_image_payloads,
)
from focus.runtime.runs.events import validate_messages


CURATION_INPUT_TYPE = "focus.context_curator.input"
CURATION_INPUT_VERSION = 2


class CurationContractError(ValueError):
    """策展计划无法从明确来源证据确定性编译为合法 Context。"""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CurationSourceMessage(_StrictModel):
    source_message_id: str = Field(min_length=1)
    role: Literal["human", "ai", "system", "tool"]
    content: str | list[Any] = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    status: str | None = None


class CurationSourceSnapshot(_StrictModel):
    source_checkpoint_id: str = Field(min_length=1)
    messages: list[CurationSourceMessage] = Field(default_factory=list)
    projection_hash: str = Field(min_length=64, max_length=64)


class CopyMessage(_StrictModel):
    type: Literal["copy_message"]
    source_message_id: str = Field(min_length=1)


class ComposeMessage(_StrictModel):
    type: Literal["compose_message"]
    role: Literal["system", "human", "ai"]
    content: str = Field(min_length=1)
    source_message_ids: list[str] = Field(min_length=1)


class ToolExchangeCall(_StrictModel):
    name: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    result_content: str | list[Any]
    status: Literal["success", "error"] = "success"


class ToolExchange(_StrictModel):
    type: Literal["tool_exchange"]
    assistant_content: str = ""
    calls: list[ToolExchangeCall] = Field(min_length=1)
    source_message_ids: list[str] = Field(min_length=1)


CuratedContextPlanItem = Annotated[
    CopyMessage | ComposeMessage | ToolExchange,
    Field(discriminator="type"),
]


class CuratedContextPlan(_StrictModel):
    outcome: Literal["replace", "no_change"]
    items: list[CuratedContextPlanItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_outcome(self) -> "CuratedContextPlan":
        if self.outcome == "no_change" and self.items:
            raise ValueError("no_change 的 items 必须为空")
        if self.outcome == "replace" and not self.items:
            raise ValueError("replace 必须提供完整的非空 Context items")
        return self


@dataclass(frozen=True)
class CompiledCuratedContext:
    outcome: Literal["replace", "no_change"]
    authored_messages: list[dict[str, Any]]
    disposition_manifest: list[dict[str, Any]]


def build_curation_input(
    snapshot: CurationSourceSnapshot,
    base_binding_revision: int,
    policy: ContextCurationPolicy | dict[str, Any],
    published_authored_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    normalized_policy = (
        policy.model_dump(mode="json")
        if isinstance(policy, ContextCurationPolicy)
        else ContextCurationPolicy.model_validate(policy).model_dump(mode="json")
    )
    current_messages = [
        {
            key: deepcopy(item[key])
            for key in (
                "role", "content", "tool_calls", "tool_call_id", "name", "status",
                "curation_source_message_ids",
            )
            if key in item
        }
        for item in published_authored_messages
        if item.get("role") in {"human", "ai", "system", "tool"}
    ]
    return {
        "type": CURATION_INPUT_TYPE,
        "version": CURATION_INPUT_VERSION,
        "base_binding_revision": base_binding_revision,
        "current_published_context": {"messages": current_messages},
        "curation_policy": normalized_policy,
        "source_snapshot": snapshot.model_dump(mode="json"),
    }


def estimate_curation_tokens(payload: dict[str, Any]) -> int:
    source = payload.get("source_snapshot") or {}
    messages = source.get("messages") or []
    image_tokens = estimate_images_tokens(messages)
    counted = payload
    if image_tokens:
        counted = {
            **payload,
            "source_snapshot": {**source, "messages": strip_image_payloads(messages)},
        }
    raw = json.dumps(counted, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return estimate_raw_tokens(raw, len(messages)) + image_tokens


def compile_curated_context(
    plan: CuratedContextPlan | dict[str, Any],
    snapshot: CurationSourceSnapshot,
) -> CompiledCuratedContext:
    parsed = (
        plan if isinstance(plan, CuratedContextPlan)
        else CuratedContextPlan.model_validate(plan)
    )
    sources = {item.source_message_id: item for item in snapshot.messages}
    used_by: dict[str, list[dict[str, Any]]] = {}
    messages: list[dict[str, Any]] = []

    if parsed.outcome == "no_change":
        return CompiledCuratedContext(
            outcome="no_change",
            authored_messages=[],
            disposition_manifest=_dispositions(snapshot, used_by),
        )

    for plan_index, item in enumerate(parsed.items):
        start_index = len(messages)
        if isinstance(item, CopyMessage):
            source = _require_source(sources, item.source_message_id, "CopyMessage")
            if source.role == "tool" or source.tool_calls:
                raise CurationContractError(
                    "CopyMessage 只能复制不属于工具交换的独立普通消息"
                )
            evidence = [item.source_message_id]
            messages.append(_authored_message(
                snapshot, plan_index, 0, source.role, deepcopy(source.content), evidence
            ))
        elif isinstance(item, ComposeMessage):
            evidence = _evidence(item.source_message_ids, sources, "ComposeMessage")
            if not item.content.strip():
                raise CurationContractError("ComposeMessage content 不能为空白")
            messages.append(_authored_message(
                snapshot, plan_index, 0, item.role, item.content, evidence
            ))
        else:
            evidence = _evidence(item.source_message_ids, sources, "ToolExchange")
            messages.extend(_compile_tool_exchange(
                snapshot, plan_index, item, evidence, sources
            ))

        target_indexes = list(range(start_index, len(messages)))
        for source_id in evidence:
            used_by.setdefault(source_id, []).append({
                "plan_item_index": plan_index,
                "plan_item_type": item.type,
                "target_message_indexes": target_indexes,
            })

    try:
        validate_messages(messages)
    except ValueError as exc:
        raise CurationContractError(f"策展结果不是合法 Context: {exc}") from exc
    return CompiledCuratedContext(
        outcome="replace",
        authored_messages=messages,
        disposition_manifest=_dispositions(snapshot, used_by),
    )


def _require_source(
    sources: dict[str, CurationSourceMessage], source_id: str, item_type: str
) -> CurationSourceMessage:
    source = sources.get(source_id)
    if source is None:
        raise CurationContractError(f"{item_type} 引用未知来源消息: {source_id}")
    return source


def _evidence(
    source_ids: list[str],
    sources: dict[str, CurationSourceMessage],
    item_type: str,
) -> list[str]:
    if len(set(source_ids)) != len(source_ids):
        raise CurationContractError(f"{item_type} 包含重复来源消息")
    for source_id in source_ids:
        _require_source(sources, source_id, item_type)
    return list(source_ids)


def _authored_message(
    snapshot: CurationSourceSnapshot,
    plan_index: int,
    message_index: int,
    role: str,
    content: Any,
    evidence: list[str],
    **extra: Any,
) -> dict[str, Any]:
    identity = [
        snapshot.source_checkpoint_id,
        plan_index,
        message_index,
        role,
        content,
        evidence,
        extra,
    ]
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
    ).hexdigest()[:24]
    return {
        "id": f"focus-curation-{digest}",
        "role": role,
        "content": content,
        "curation_source_message_ids": list(evidence),
        **extra,
    }


def _compile_tool_exchange(
    snapshot: CurationSourceSnapshot,
    plan_index: int,
    item: ToolExchange,
    evidence: list[str],
    sources: dict[str, CurationSourceMessage],
) -> list[dict[str, Any]]:
    ai_sources = [sources[source_id] for source_id in evidence if sources[source_id].tool_calls]
    if len(ai_sources) != 1:
        raise CurationContractError(
            "ToolExchange 必须引用且只能引用一个包含 tool_calls 的来源 AIMessage"
        )
    source_ai = ai_sources[0]
    tool_results = {
        source.tool_call_id: source
        for source_id in evidence
        if (source := sources[source_id]).role == "tool" and source.tool_call_id
    }
    if len(item.calls) != len(source_ai.tool_calls):
        raise CurationContractError("ToolExchange 必须覆盖来源 AIMessage 的全部工具调用")

    verified: list[tuple[ToolExchangeCall, CurationSourceMessage]] = []
    for position, (declared, source_call) in enumerate(zip(item.calls, source_ai.tool_calls)):
        source_result = tool_results.get(source_call.get("id"))
        if source_result is None:
            raise CurationContractError("ToolExchange 来源证据缺少对应 ToolMessage")
        source_status = source_result.status or "success"
        if (
            declared.name != source_call.get("name")
            or declared.args != source_call.get("args", {})
            or declared.result_content != source_result.content
            or declared.status != source_status
        ):
            raise CurationContractError(
                f"ToolExchange 第 {position + 1} 个调用与来源工具证据不一致"
            )
        verified.append((declared, source_result))

    call_ids = [
        "focus-curation-call-" + hashlib.sha256(
            json.dumps(
                [snapshot.source_checkpoint_id, plan_index, index, call.model_dump(mode="json")],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:24]
        for index, (call, _) in enumerate(verified)
    ]
    assistant = _authored_message(
        snapshot,
        plan_index,
        0,
        "ai",
        item.assistant_content,
        evidence,
        tool_calls=[
            {"id": call_id, "name": call.name, "args": deepcopy(call.args)}
            for call_id, (call, _) in zip(call_ids, verified)
        ],
    )
    results = [
        _authored_message(
            snapshot,
            plan_index,
            index + 1,
            "tool",
            deepcopy(call.result_content),
            evidence,
            tool_call_id=call_id,
            name=call.name,
            status=call.status,
        )
        for index, (call_id, (call, _)) in enumerate(zip(call_ids, verified))
    ]
    return [assistant, *results]


def _dispositions(
    snapshot: CurationSourceSnapshot,
    used_by: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for source in snapshot.messages:
        usages = used_by.get(source.source_message_id, [])
        target_indexes = sorted({
            index
            for usage in usages
            for index in usage["target_message_indexes"]
        })
        result.append({
            "source_message_id": source.source_message_id,
            "action": "used" if usages else "discarded",
            "reason_category": "selected_by_plan" if usages else "not_selected_by_plan",
            "target_message_indexes": target_indexes,
            "plan_items": usages,
        })
    return result
