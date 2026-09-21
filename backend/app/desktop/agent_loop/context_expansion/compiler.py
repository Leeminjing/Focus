r"""本文件对外提供 ExpansionPlanCompilerPort、ContextExpansionPlanCompiler 与 DeterministicExpansionPlanCompiler。

输入为冻结 observation、ExpansionOpportunity 与 SpawnContextIntent；输出为 CompiledExpansion 或 ExpansionBlocker。
具体工作流为 production adapter 精确读取不可变 Revision，纯 compiler 从冻结 opportunity 取用派生语义与工作指令、选择最小相关证据、补齐完整 Tool Exchange、追加工作指令，
调用既有 Lane compiler 验证 shadow checkpoint 后返回内部 CreateLanePlan；派生意图只用于记录模型选择，失败只返回稳定 blocker。
示例：`compiled = await compiler.compile(observation, opportunity, intent)`。
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    CompiledExpansion,
    ExpansionBlocker,
    ExpansionOpportunity,
    SpawnContextIntent,
    stable_expansion_hash,
)
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.context_curation import (
    ComposeMessage,
    CopyMessage,
    CreateLanePlan,
    MultiSourceEvidence,
    NamespacedMessageRef,
    SourceMessageEvidence,
    SourceRevisionEvidence,
    ToolExchange,
    ToolExchangeCall,
    compile_lane,
)
from backend.app.desktop.context_evolution import ContextRevisionReader, ContextRevisionRepository
from backend.app.desktop.context_evolution.repository import ContextRevisionNotFound


class ExpansionPlanCompilerPort(Protocol):
    async def compile(
        self,
        observation: LoopObservationEnvelope,
        opportunity: ExpansionOpportunity,
        intent: SpawnContextIntent,
    ) -> CompiledExpansion | ExpansionBlocker: ...


class ContextExpansionPlanCompiler:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], checkpointer: Any) -> None:
        self._sessions = sessions
        self._repository = ContextRevisionRepository()
        self._reader = ContextRevisionReader(self._repository, checkpointer)
        self._compiler = DeterministicExpansionPlanCompiler()

    async def compile(
        self,
        observation: LoopObservationEnvelope,
        opportunity: ExpansionOpportunity,
        intent: SpawnContextIntent,
    ) -> CompiledExpansion | ExpansionBlocker:
        blocker = self._validate_frozen_source(observation, opportunity, intent)
        if blocker is not None:
            return blocker
        try:
            async with self._sessions() as session:
                revision = await self._repository.get(session, opportunity.source)
                view = await self._reader.read(session, opportunity.source, "execution")
        except ContextRevisionNotFound:
            return self._blocked(opportunity, "source_unreadable", "冻结来源 Revision 或 Checkpoint 已不可读取")
        return self._compiler.compile(opportunity, intent, revision, tuple(view.messages))

    @staticmethod
    def _validate_frozen_source(
        observation: LoopObservationEnvelope,
        opportunity: ExpansionOpportunity,
        intent: SpawnContextIntent,
    ) -> ExpansionBlocker | None:
        if intent.opportunity_id != opportunity.opportunity_id:
            return ContextExpansionPlanCompiler._blocked(opportunity, "stale_source", "semantic intent 与冻结 opportunity 不一致")
        frontier = {
            (
                str(item.get("context_id")),
                str(item.get("revision_id")),
                str(item.get("checkpoint_id") or (item.get("revision") or {}).get("checkpoint_id") or ""),
            )
            for item in observation.portfolio_frontier
        }
        identity = (
            opportunity.source.context_id,
            opportunity.source.revision_id,
            opportunity.source.checkpoint_id or "",
        )
        if identity not in frontier:
            return ContextExpansionPlanCompiler._blocked(opportunity, "stale_source", "冻结来源已不在 observation frontier")
        scope = set((observation.grant or {}).get("context_scope") or ())
        if scope and opportunity.source.context_id not in scope:
            return ContextExpansionPlanCompiler._blocked(opportunity, "source_out_of_scope", "冻结来源超出 delegation scope")
        return None

    @staticmethod
    def _blocked(opportunity: ExpansionOpportunity, code: str, summary: str) -> ExpansionBlocker:
        return ExpansionBlocker(code=code, summary=summary, opportunity_id=opportunity.opportunity_id)


class DeterministicExpansionPlanCompiler:
    VERSION = "context-expansion-compiler-v1"

    def compile(
        self,
        opportunity: ExpansionOpportunity,
        intent: SpawnContextIntent,
        revision: Any,
        messages: tuple[dict[str, Any], ...],
    ) -> CompiledExpansion | ExpansionBlocker:
        try:
            evidence = self._evidence(opportunity, revision, messages)
            plan = self._plan(opportunity, evidence)
            compiled_lane = compile_lane(plan.model_dump(mode="json"), evidence)
        except (ValueError, KeyError, TypeError) as exc:
            return ExpansionBlocker(
                code="compiler_failed",
                summary=f"无法从冻结来源编译可运行 Context：{str(exc)[:1200]}",
                opportunity_id=opportunity.opportunity_id,
            )
        expansion_id = stable_expansion_hash(
            "compiled-expansion",
            opportunity.opportunity_id,
            intent.model_dump(mode="json"),
            compiled_lane.definition_hash,
            self.VERSION,
        )
        return CompiledExpansion(
            expansion_id=expansion_id,
            opportunity=opportunity,
            intent=intent,
            plan=plan,
            definition_hash=compiled_lane.definition_hash,
            compiler_version=self.VERSION,
        )

    def _evidence(
        self,
        opportunity: ExpansionOpportunity,
        revision: Any,
        messages: tuple[dict[str, Any], ...],
    ) -> MultiSourceEvidence:
        normalized = tuple(self._message(opportunity, item) for item in messages if item.get("id") and item.get("role"))
        if not normalized:
            raise ValueError("来源 Revision 没有可引用消息")
        selected = self._selected_indexes(opportunity, normalized)
        closed = self._protocol_closure(normalized, selected)
        projection_hash = getattr(revision, "projection_hash", None) or self._hash(messages)
        content_hash = getattr(revision, "content_hash", None) or self._hash((opportunity.source.model_dump(mode="json"), messages))
        return MultiSourceEvidence(
            sources=(
                SourceRevisionEvidence(
                    source=opportunity.source,
                    projection_hash=projection_hash,
                    content_hash=content_hash,
                    messages=tuple(normalized[index] for index in sorted(closed)),
                ),
            )
        )

    def _plan(
        self,
        opportunity: ExpansionOpportunity,
        evidence: MultiSourceEvidence,
    ) -> CreateLanePlan:
        source_messages = evidence.sources[0].messages
        items: list[CopyMessage | ComposeMessage | ToolExchange] = []
        consumed: set[str] = set()
        by_call_id = {item.tool_call_id: item for item in source_messages if item.role == "tool" and item.tool_call_id}
        for message in source_messages:
            if message.ref.message_id in consumed:
                continue
            if message.role == "ai" and message.tool_calls:
                results = tuple(by_call_id.get(str(call.get("id"))) for call in message.tool_calls)
                if any(result is None for result in results):
                    raise ValueError("Tool Call 缺少对应 Tool Result")
                refs = (message.ref, *(result.ref for result in results if result is not None))
                calls = tuple(
                    ToolExchangeCall(
                        name=str(call.get("name") or ""),
                        args=dict(call.get("args") or {}),
                        result_content=result.content,
                        status=result.status or "success",
                    )
                    for call, result in zip(message.tool_calls, results, strict=True)
                    if result is not None
                )
                items.append(ToolExchange(type="tool_exchange", assistant_content=str(message.content or ""), calls=calls, sources=refs))
                consumed.update(ref.message_id for ref in refs)
            elif message.role != "tool":
                items.append(CopyMessage(type="copy_message", source=message.ref))
                consumed.add(message.ref.message_id)
        lineage = tuple(message.ref for message in source_messages)
        items.append(
            ComposeMessage(
                type="compose_message",
                role="human",
                content=(
                    f"Purpose: {opportunity.purpose}\n"
                    f"Work order: {opportunity.work_order}\n"
                    f"Completion check: {opportunity.completion_check}\n"
                    f"Workspace mode: {opportunity.workspace_mode}"
                ),
                sources=lineage,
            )
        )
        return CreateLanePlan(
            action="create",
            purpose=opportunity.purpose,
            source_frontier=(opportunity.source,),
            items=tuple(items),
            lane_policy={
                "expansion_id": opportunity.opportunity_id,
                "independence_key": opportunity.independence_key,
                "semantic_fingerprint": opportunity.semantic_fingerprint,
                "workspace_mode": opportunity.workspace_mode,
                "completion_check": opportunity.completion_check,
            },
        )

    @staticmethod
    def _message(opportunity: ExpansionOpportunity, message: dict[str, Any]) -> SourceMessageEvidence:
        return SourceMessageEvidence(
            ref=NamespacedMessageRef(source=opportunity.source, message_id=str(message["id"])),
            role=str(message["role"]),
            content=message.get("content", ""),
            tool_calls=tuple(message.get("tool_calls") or ()),
            tool_call_id=message.get("tool_call_id"),
            name=message.get("name"),
            status=message.get("status"),
        )

    @staticmethod
    def _selected_indexes(
        opportunity: ExpansionOpportunity,
        messages: tuple[SourceMessageEvidence, ...],
    ) -> set[int]:
        hints = tuple(item.casefold() for item in opportunity.evidence_hints if item)
        selected = {
            index
            for index, message in enumerate(messages)
            if message.ref.message_id.casefold() in hints
            or any(hint in str(message.content).casefold() for hint in hints)
        }
        if not selected:
            selected.add(len(messages) - 1)
            for index in range(len(messages) - 1, -1, -1):
                if messages[index].role in {"human", "system"}:
                    selected.add(index)
                    break
        return selected

    @staticmethod
    def _protocol_closure(
        messages: tuple[SourceMessageEvidence, ...],
        selected: set[int],
    ) -> set[int]:
        callers: dict[str, int] = {}
        results: dict[str, int] = {}
        caller_calls: dict[int, tuple[str, ...]] = {}
        for index, message in enumerate(messages):
            ids = tuple(str(call.get("id")) for call in message.tool_calls if call.get("id"))
            if ids:
                caller_calls[index] = ids
                callers.update({call_id: index for call_id in ids})
            if message.tool_call_id:
                results[message.tool_call_id] = index
        closed = set(selected)
        for index in tuple(selected):
            message = messages[index]
            caller_index = callers.get(message.tool_call_id or "") if message.role == "tool" else index if index in caller_calls else None
            if caller_index is None:
                continue
            closed.add(caller_index)
            for call_id in caller_calls[caller_index]:
                result_index = results.get(call_id)
                if result_index is None:
                    raise ValueError("来源 Tool Exchange 不完整")
                closed.add(result_index)
        return closed

    @staticmethod
    def _hash(value: Any) -> str:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return sha256(payload.encode("utf-8")).hexdigest()
