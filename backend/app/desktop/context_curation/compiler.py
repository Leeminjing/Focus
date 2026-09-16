r"""本文件对外提供 compile_lane、CompiledLaneCandidate、MessageLineage 与 SourceDisposition。

输入为严格 create/update Lane plan 和命名空间 MultiSourceEvidence；输出为确定性 authored/execution
消息、投影哈希、逐消息外部 lineage 与完整来源处置。具体工作流为验证每个来源四元键，系统生成
消息及 tool-call 身份，调用统一 Context projection，且只接受无需隐藏修补的 valid 候选。
示例：`compiled = compile_lane(plan, evidence)`。
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from backend.app.desktop.context_curation.contracts import (
    ComposeMessage,
    CopyMessage,
    CreateLanePlan,
    LanePlan,
    MultiSourceEvidence,
    NamespacedMessageRef,
    SourceMessageEvidence,
    ToolExchange,
    ToolExchangeCall,
    UpdateLanePlan,
)
from backend.app.desktop.context_evolution import ContextRevisionRef
from backend.app.desktop.context_projection import compile_context_messages


class LaneCompilationError(ValueError):
    pass


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MessageLineage(_FrozenModel):
    target_message_id: str
    operation: Literal["copy_message", "compose_message", "tool_exchange"]
    sources: tuple[NamespacedMessageRef, ...]


class SourceDisposition(_FrozenModel):
    source: NamespacedMessageRef
    action: Literal["used", "discarded"]
    target_message_ids: tuple[str, ...]
    plan_item_indexes: tuple[int, ...]


class CompiledLaneCandidate(_FrozenModel):
    action: Literal["create", "update"]
    lane_id: str | None
    purpose: str
    source_frontier: tuple[ContextRevisionRef, ...]
    authored_messages: tuple[dict[str, Any], ...]
    execution_messages: tuple[dict[str, Any], ...]
    message_lineage: tuple[MessageLineage, ...]
    source_dispositions: tuple[SourceDisposition, ...]
    definition_hash: str
    projection_hash: str
    content_hash: str
    semantic_fingerprint: str
    projection_status: Literal["valid"]


def compile_lane(
    plan: LanePlan | dict[str, Any],
    evidence: MultiSourceEvidence | dict[str, Any],
) -> CompiledLaneCandidate:
    parsed_plan = plan if isinstance(plan, LanePlan) else LanePlan.model_validate(plan)
    parsed_evidence = (
        evidence
        if isinstance(evidence, MultiSourceEvidence)
        else MultiSourceEvidence.model_validate(evidence)
    )
    mutation = parsed_plan.root
    if not isinstance(mutation, (CreateLanePlan, UpdateLanePlan)):
        raise LaneCompilationError(f"{mutation.action} 不生成新的 Context projection")
    available = _selected_evidence(mutation.source_frontier, parsed_evidence)
    messages: list[dict[str, Any]] = []
    lineage: list[MessageLineage] = []
    usage: dict[tuple[str, str, str, str], list[tuple[str, int]]] = {}
    for item_index, item in enumerate(mutation.items):
        compiled = _compile_item(mutation, item_index, item, available)
        messages.extend(message for message, _ in compiled)
        for message, sources in compiled:
            target_id = str(message["id"])
            lineage.append(
                MessageLineage(
                    target_message_id=target_id,
                    operation=item.type,
                    sources=sources,
                )
            )
            for source in sources:
                usage.setdefault(source.key, []).append((target_id, item_index))
    projection = compile_context_messages(messages)
    if projection.status != "valid":
        raise LaneCompilationError(
            f"自动 Lane candidate 必须直接 valid，实际为 {projection.status}"
        )
    dispositions = _dispositions(available, usage)
    content_hash = _hash(
        {
            "action": mutation.action,
            "lane_id": mutation.lane_id,
            "purpose": mutation.purpose,
            "source_frontier": [
                source.model_dump(mode="json") for source in mutation.source_frontier
            ],
            "authored_messages": projection.authored_messages,
            "lineage": [item.model_dump(mode="json") for item in lineage],
        }
    )
    semantic_fingerprint = _hash(_semantic_messages(projection.authored_messages))
    return CompiledLaneCandidate(
        action=mutation.action,
        lane_id=mutation.lane_id,
        purpose=mutation.purpose,
        source_frontier=mutation.source_frontier,
        authored_messages=tuple(projection.authored_messages),
        execution_messages=tuple(projection.execution_messages),
        message_lineage=tuple(lineage),
        source_dispositions=dispositions,
        definition_hash=projection.definition_hash,
        projection_hash=projection.projection_hash,
        content_hash=content_hash,
        semantic_fingerprint=semantic_fingerprint,
        projection_status="valid",
    )


def _selected_evidence(
    frontier: tuple[ContextRevisionRef, ...],
    evidence: MultiSourceEvidence,
) -> dict[tuple[str, str, str, str], SourceMessageEvidence]:
    bundles = {bundle.source: bundle for bundle in evidence.sources}
    missing = [source for source in frontier if source not in bundles]
    if missing:
        raise LaneCompilationError(
            "Lane candidate 缺少 source frontier evidence: "
            + ", ".join(source.revision_id for source in missing)
        )
    return {
        message.ref.key: message
        for source in frontier
        for message in bundles[source].messages
    }


def _compile_item(
    mutation: CreateLanePlan | UpdateLanePlan,
    item_index: int,
    item: CopyMessage | ComposeMessage | ToolExchange,
    available: dict[tuple[str, str, str, str], SourceMessageEvidence],
) -> list[tuple[dict[str, Any], tuple[NamespacedMessageRef, ...]]]:
    if isinstance(item, CopyMessage):
        source = _require_source(item.source, available, "CopyMessage")
        if source.role == "tool" or source.tool_calls:
            raise LaneCompilationError("CopyMessage 不能拆散来源工具交换")
        message = _message(
            mutation,
            item_index,
            0,
            source.role,
            deepcopy(source.content),
            (item.source,),
        )
        return [(message, (item.source,))]
    if isinstance(item, ComposeMessage):
        _require_sources(item.sources, available, "ComposeMessage")
        message = _message(
            mutation,
            item_index,
            0,
            item.role,
            item.content,
            item.sources,
        )
        return [(message, item.sources)]
    return _compile_tool_exchange(mutation, item_index, item, available)


def _compile_tool_exchange(
    mutation: CreateLanePlan | UpdateLanePlan,
    item_index: int,
    item: ToolExchange,
    available: dict[tuple[str, str, str, str], SourceMessageEvidence],
) -> list[tuple[dict[str, Any], tuple[NamespacedMessageRef, ...]]]:
    sources = _require_sources(item.sources, available, "ToolExchange")
    callers = [source for source in sources if source.role == "ai" and source.tool_calls]
    if len(callers) != 1:
        raise LaneCompilationError("ToolExchange 必须引用且只能引用一个 tool-calling AIMessage")
    caller = callers[0]
    source_results = {
        source.tool_call_id: source
        for source in sources
        if source.role == "tool" and source.tool_call_id
    }
    if len(item.calls) != len(caller.tool_calls):
        raise LaneCompilationError("ToolExchange 必须覆盖来源 AIMessage 的全部工具调用")
    verified = tuple(
        _verify_call(declared, call, source_results, position)
        for position, (declared, call) in enumerate(zip(item.calls, caller.tool_calls))
    )
    call_ids = tuple(
        _stable_id("focus-curation-call", mutation, item_index, position, declared.model_dump(mode="json"))
        for position, (declared, _) in enumerate(verified)
    )
    assistant = _message(
        mutation,
        item_index,
        0,
        "ai",
        item.assistant_content,
        item.sources,
        tool_calls=[
            {"id": call_id, "name": declared.name, "args": deepcopy(declared.args)}
            for call_id, (declared, _) in zip(call_ids, verified)
        ],
    )
    results = [
        _message(
            mutation,
            item_index,
            position + 1,
            "tool",
            deepcopy(declared.result_content),
            item.sources,
            tool_call_id=call_id,
            name=declared.name,
            status=declared.status,
        )
        for position, (call_id, (declared, _)) in enumerate(zip(call_ids, verified))
    ]
    return [(assistant, item.sources), *((message, item.sources) for message in results)]


def _verify_call(
    declared: ToolExchangeCall,
    source_call: dict[str, Any],
    results: dict[str, SourceMessageEvidence],
    position: int,
) -> tuple[ToolExchangeCall, SourceMessageEvidence]:
    result = results.get(source_call.get("id"))
    if result is None:
        raise LaneCompilationError("ToolExchange 缺少来源 ToolMessage")
    source_status = result.status or "success"
    if (
        declared.name != source_call.get("name")
        or declared.args != source_call.get("args", {})
        or declared.result_content != result.content
        or declared.status != source_status
    ):
        raise LaneCompilationError(f"ToolExchange 第 {position + 1} 个调用与来源 evidence 不一致")
    return declared, result


def _require_source(
    ref: NamespacedMessageRef,
    available: dict[tuple[str, str, str, str], SourceMessageEvidence],
    operation: str,
) -> SourceMessageEvidence:
    source = available.get(ref.key)
    if source is None:
        raise LaneCompilationError(f"{operation} 引用不存在的命名空间 evidence: {ref.key}")
    return source


def _require_sources(
    refs: tuple[NamespacedMessageRef, ...],
    available: dict[tuple[str, str, str, str], SourceMessageEvidence],
    operation: str,
) -> tuple[SourceMessageEvidence, ...]:
    return tuple(_require_source(ref, available, operation) for ref in refs)


def _message(
    mutation: CreateLanePlan | UpdateLanePlan,
    item_index: int,
    message_index: int,
    role: str,
    content: Any,
    sources: tuple[NamespacedMessageRef, ...],
    **extra: Any,
) -> dict[str, Any]:
    message_id = _stable_id(
        "focus-curation",
        mutation,
        item_index,
        message_index,
        role,
        content,
        [source.model_dump(mode="json") for source in sources],
        extra,
    )
    return {"id": message_id, "role": role, "content": content, **extra}


def _dispositions(
    available: dict[tuple[str, str, str, str], SourceMessageEvidence],
    usage: dict[tuple[str, str, str, str], list[tuple[str, int]]],
) -> tuple[SourceDisposition, ...]:
    result: list[SourceDisposition] = []
    for key, source in available.items():
        uses = usage.get(key, [])
        result.append(
            SourceDisposition(
                source=source.ref,
                action="used" if uses else "discarded",
                target_message_ids=tuple(dict.fromkeys(target for target, _ in uses)),
                plan_item_indexes=tuple(sorted({index for _, index in uses})),
            )
        )
    return tuple(result)


def _stable_id(prefix: str, *parts: Any) -> str:
    normalized = [
        part.model_dump(mode="json") if isinstance(part, BaseModel) else part
        for part in parts
    ]
    return f"{prefix}-{_hash(normalized)[:24]}"


def _semantic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        normalized = {
            key: deepcopy(value)
            for key, value in message.items()
            if key not in {"id", "tool_call_id"}
        }
        if isinstance(normalized.get("tool_calls"), list):
            normalized["tool_calls"] = [
                {key: deepcopy(value) for key, value in call.items() if key != "id"}
                for call in normalized["tool_calls"]
            ]
        result.append(normalized)
    return result


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
