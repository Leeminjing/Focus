r"""本文件对外提供 SemanticEvidenceSelectorPort、IdentityBoundedEvidenceSelector 与 MultiSourceEvidenceResolver。

输入为冻结 WorkContextSpec、semantic manifests、授权 FrozenEvidenceCorpus 与 evidence item budget；输出为完整
ResolvedEvidenceBundle 或结构化 ExpansionBlocker。具体工作流为先验证 planner 明确指认的 semantic units，再仅以结构化权威
对象的精确 identity 补足未覆盖 requirement，按角色与 source constraints 验证 coverage，并扩展完整 Tool Exchange；
同 role 消息、关键词和最近消息都不能独立证明 coverage。
示例：`result = resolver.resolve(opportunity, manifests, corpus, max_items=64)`。
"""

from __future__ import annotations

from typing import Protocol

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    ContextSemanticManifest,
    EvidenceRequirement,
    ExpansionBlocker,
    ExpansionOpportunity,
    ResolvedEvidenceBundle,
    ResolvedEvidenceItem,
)
from backend.app.desktop.agent_loop.context_expansion.evidence_corpus import (
    CorpusEvidenceItem,
    FrozenEvidenceCorpus,
)
from backend.app.desktop.context_curation import (
    EvidenceRef,
    MaterialEvidenceRef,
    MissionEvidenceRef,
    MultiSourceEvidence,
    NamespacedMessageRef,
    RunResultEvidenceRef,
    SourceMessageEvidence,
    SourceRevisionEvidence,
    WorkspaceEffectEvidenceRef,
    evidence_ref_key,
)


class SemanticEvidenceSelectorPort(Protocol):
    def select(
        self,
        requirement: EvidenceRequirement,
        corpus: FrozenEvidenceCorpus,
        *,
        limit: int,
    ) -> tuple[CorpusEvidenceItem, ...]: ...


class IdentityBoundedEvidenceSelector:
    def select(
        self,
        requirement: EvidenceRequirement,
        corpus: FrozenEvidenceCorpus,
        *,
        limit: int,
    ) -> tuple[CorpusEvidenceItem, ...]:
        matches = tuple(
            item
            for item in corpus.items
            if self._identity(item.ref) == requirement.requirement_id
            and requirement.role in item.semantic_roles
            and self._allowed(requirement, item)
        )
        return tuple(sorted(matches, key=lambda item: evidence_ref_key(item.ref)))[: max(0, limit)]

    @staticmethod
    def _identity(ref: EvidenceRef) -> str | None:
        if isinstance(ref, MissionEvidenceRef):
            return ref.item_id
        if isinstance(ref, RunResultEvidenceRef):
            return ref.result_id
        if isinstance(ref, MaterialEvidenceRef):
            return ref.material_id
        if isinstance(ref, WorkspaceEffectEvidenceRef):
            return ref.effect_id
        return None

    @staticmethod
    def _allowed(requirement: EvidenceRequirement, item: CorpusEvidenceItem) -> bool:
        constraints = requirement.source_constraints
        if constraints.context_roles and (item.source_context_role or "").casefold() not in constraints.context_roles:
            return False
        if constraints.context_ids and (
            not isinstance(item.ref, NamespacedMessageRef)
            or item.ref.source.context_id.casefold() not in constraints.context_ids
        ):
            return False
        if constraints.evidence_kinds:
            kind = evidence_ref_key(item.ref)[0].casefold()
            if kind not in constraints.evidence_kinds:
                return False
        return True


class MultiSourceEvidenceResolver:
    VERSION = "multi-source-evidence-resolver-v1"

    def __init__(self, selector: SemanticEvidenceSelectorPort | None = None) -> None:
        self._selector = selector or IdentityBoundedEvidenceSelector()

    def resolve(
        self,
        opportunity: ExpansionOpportunity,
        manifests: tuple[ContextSemanticManifest, ...],
        corpus: FrozenEvidenceCorpus,
        *,
        max_items: int = 128,
    ) -> ResolvedEvidenceBundle | ExpansionBlocker:
        unit_refs = self._unit_refs(manifests, corpus)
        resolved: list[ResolvedEvidenceItem] = []
        selected_refs: dict[tuple[str, ...], EvidenceRef] = {}
        for requirement in opportunity.work_spec.evidence_requirements:
            candidates = self._candidate_items(requirement, unit_refs, corpus)
            if not candidates:
                candidates = self._selector.select(requirement, corpus, limit=max_items)
            candidates = tuple(item for item in candidates if self._covers(requirement, item))
            if not candidates:
                if requirement.necessity == "required":
                    return self._blocked(
                        opportunity,
                        "required_evidence_unresolved",
                        f"必需 evidence requirement 未解析：{requirement.requirement_id}",
                    )
                continue
            chosen = min(candidates, key=lambda item: evidence_ref_key(item.ref))
            selected_refs[evidence_ref_key(chosen.ref)] = chosen.ref
            resolved.append(
                ResolvedEvidenceItem(
                    requirement_id=requirement.requirement_id,
                    ref=chosen.ref,
                    content_hash=chosen.content_hash,
                    relevance_reason=(
                        f"Exact semantic binding satisfies {requirement.requirement_id}: "
                        f"{requirement.question} Coverage criterion: {requirement.coverage_criterion}"
                    ),
                )
            )
        try:
            closed_refs = self._protocol_closure(corpus, tuple(selected_refs.values()))
        except ValueError as exc:
            return self._blocked(opportunity, "required_evidence_unresolved", str(exc))
        if len(closed_refs) > max_items:
            return self._blocked(
                opportunity,
                "evidence_budget_exhausted",
                f"协议闭包需要 {len(closed_refs)} 项 evidence，超过预算 {max_items}",
                retryable=True,
            )
        try:
            evidence = self._subset(corpus, closed_refs)
            return ResolvedEvidenceBundle.create(
                work_spec=opportunity.work_spec,
                evidence=evidence,
                items=tuple(resolved),
            )
        except (KeyError, TypeError, ValueError) as exc:
            return self._blocked(opportunity, "required_evidence_unresolved", str(exc))

    @staticmethod
    def _unit_refs(
        manifests: tuple[ContextSemanticManifest, ...],
        corpus: FrozenEvidenceCorpus,
    ) -> dict[str, tuple[EvidenceRef, ...]]:
        result = {
            unit.unit_id: unit.evidence_refs
            for manifest in manifests
            for unit in manifest.units
        }
        for item in corpus.items:
            for unit_id in item.semantic_unit_ids:
                result.setdefault(unit_id, (item.ref,))
        return result

    @staticmethod
    def _candidate_items(
        requirement: EvidenceRequirement,
        unit_refs: dict[str, tuple[EvidenceRef, ...]],
        corpus: FrozenEvidenceCorpus,
    ) -> tuple[CorpusEvidenceItem, ...]:
        refs = tuple(
            ref
            for unit_id in requirement.candidate_unit_ids
            for ref in unit_refs.get(unit_id, ())
        )
        result = []
        for ref in refs:
            try:
                result.append(corpus.item(ref))
            except KeyError:
                continue
        return tuple(result)

    @staticmethod
    def _covers(requirement: EvidenceRequirement, item: CorpusEvidenceItem) -> bool:
        return requirement.role in item.semantic_roles and IdentityBoundedEvidenceSelector._allowed(requirement, item)

    @staticmethod
    def _protocol_closure(
        corpus: FrozenEvidenceCorpus,
        selected: tuple[EvidenceRef, ...],
    ) -> tuple[EvidenceRef, ...]:
        closed = {evidence_ref_key(ref): ref for ref in selected}
        selected_message_keys = {
            ref.key for ref in selected if isinstance(ref, NamespacedMessageRef)
        }
        for source in corpus.evidence.sources:
            callers: dict[str, SourceMessageEvidence] = {}
            results: dict[str, SourceMessageEvidence] = {}
            for message in source.messages:
                for call in message.tool_calls:
                    if call.get("id"):
                        callers[str(call["id"])] = message
                if message.tool_call_id:
                    results[message.tool_call_id] = message
            relevant_callers = set()
            for message in source.messages:
                if message.ref.key not in selected_message_keys:
                    continue
                if message.tool_call_id:
                    caller = callers.get(message.tool_call_id)
                    if caller is None:
                        raise ValueError("选中的 Tool Result 缺少 Assistant caller")
                    relevant_callers.add(caller.ref.key)
                if message.tool_calls:
                    relevant_callers.add(message.ref.key)
            for message in source.messages:
                if message.ref.key not in relevant_callers:
                    continue
                closed[evidence_ref_key(message.ref)] = message.ref
                for call in message.tool_calls:
                    result = results.get(str(call.get("id") or ""))
                    if result is None:
                        raise ValueError("选中的 Tool Exchange 缺少 sibling Tool Result")
                    closed[evidence_ref_key(result.ref)] = result.ref
        return tuple(closed[key] for key in sorted(closed))

    @staticmethod
    def _subset(
        corpus: FrozenEvidenceCorpus,
        refs: tuple[EvidenceRef, ...],
    ) -> MultiSourceEvidence:
        keys = {evidence_ref_key(ref) for ref in refs}
        sources = tuple(
            SourceRevisionEvidence(
                source=source.source,
                projection_hash=source.projection_hash,
                content_hash=source.content_hash,
                messages=tuple(
                    message
                    for message in source.messages
                    if evidence_ref_key(message.ref) in keys
                ),
            )
            for source in corpus.evidence.sources
            if any(evidence_ref_key(message.ref) in keys for message in source.messages)
        )
        if not sources and corpus.evidence.sources:
            first = corpus.evidence.sources[0]
            sources = (
                SourceRevisionEvidence(
                    source=first.source,
                    projection_hash=first.projection_hash,
                    content_hash=first.content_hash,
                    messages=(),
                ),
            )
        structured = tuple(
            item
            for item in corpus.evidence.structured
            if evidence_ref_key(item.ref) in keys
        )
        return MultiSourceEvidence(
            sources=sources,
            structured=structured,
            evidence_frontier=tuple(sorted(refs, key=evidence_ref_key)),
        )

    @staticmethod
    def _blocked(
        opportunity: ExpansionOpportunity,
        code: str,
        summary: str,
        *,
        retryable: bool = False,
    ) -> ExpansionBlocker:
        return ExpansionBlocker(
            code=code,
            summary=summary[:2000],
            opportunity_id=opportunity.opportunity_id,
            retryable=retryable,
        )
