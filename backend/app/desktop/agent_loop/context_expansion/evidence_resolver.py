r"""本文件对外提供 SemanticEvidenceSelectorPort、IdentityBoundedEvidenceSelector 与 MultiSourceEvidenceResolver。

输入为冻结 WorkContextSpec、semantic manifests、授权 FrozenEvidenceCorpus 与 evidence item budget；输出为候选角色覆盖诊断或完整
ResolvedEvidenceBundle 或结构化 ExpansionBlocker。具体工作流为先验证 planner 明确指认的 semantic units，再仅以结构化权威
对象的精确 identity 补足未覆盖 requirement；新版 Mission candidate 直接按 revision/section/hash 解析，
再按角色与 source constraints 验证 coverage，保留同要求的联合绑定并按精确身份去重，与质量预检复用同一完整Tool Exchange闭包后统一检查预算；
同 role 消息、关键词和最近消息都不能独立证明 coverage。
示例：`candidates, covered = resolver.inspect_requirement(requirement, manifests, corpus)`；正式编译继续使用 resolve。
"""

from __future__ import annotations

from typing import Protocol

from backend.app.desktop.agent_loop.resource_limits import exceeds_limit
from backend.app.desktop.agent_loop.expansion_resource_policy import ExpansionResourcePolicy
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
from backend.app.desktop.agent_loop.context_expansion.tool_exchange_closure import tool_exchange_closure
from backend.app.desktop.context_curation import (
    EvidenceRef,
    MaterialEvidenceRef,
    MissionEvidenceRef,
    MultiSourceEvidence,
    NamespacedMessageRef,
    RunResultEvidenceRef,
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
        limit: int | None,
    ) -> tuple[CorpusEvidenceItem, ...]: ...


class IdentityBoundedEvidenceSelector:
    def select(
        self,
        requirement: EvidenceRequirement,
        corpus: FrozenEvidenceCorpus,
        *,
        limit: int | None,
    ) -> tuple[CorpusEvidenceItem, ...]:
        matches = tuple(
            item
            for item in corpus.items
            if self._identity(item.ref) == requirement.requirement_id
            and requirement.role in item.semantic_roles
            and self._allowed(requirement, item)
        )
        stop = None if limit is None else max(0, limit)
        return tuple(sorted(matches, key=lambda item: evidence_ref_key(item.ref)))[:stop]

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
    VERSION = "multi-source-evidence-resolver-v2"

    def __init__(self, selector: SemanticEvidenceSelectorPort | None = None) -> None:
        self._selector = selector or IdentityBoundedEvidenceSelector()

    def resolve(
        self,
        opportunity: ExpansionOpportunity,
        manifests: tuple[ContextSemanticManifest, ...],
        corpus: FrozenEvidenceCorpus,
        *,
        max_items: int | None = ExpansionResourcePolicy().max_compiled_evidence_items,
    ) -> ResolvedEvidenceBundle | ExpansionBlocker:
        resolved: list[ResolvedEvidenceItem] = []
        selected_refs: dict[tuple[str, ...], EvidenceRef] = {}
        for requirement in opportunity.work_spec.evidence_requirements:
            _, candidates = self.inspect_requirement(requirement, manifests, corpus)
            if not candidates:
                if requirement.necessity == "required":
                    return self._blocked(
                        opportunity,
                        "required_evidence_unresolved",
                        f"必需 evidence requirement 未解析：{requirement.requirement_id}",
                    )
                continue
            bindings = {evidence_ref_key(item.ref): item for item in candidates}
            for key, chosen in sorted(bindings.items()):
                selected_refs[key] = chosen.ref
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
            closed_refs = tool_exchange_closure(corpus.evidence, tuple(selected_refs.values()))
        except ValueError as exc:
            return self._blocked(opportunity, "required_evidence_unresolved", str(exc))
        if exceeds_limit(len(closed_refs), max_items):
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

    def inspect_requirement(self, requirement, manifests, corpus, *, max_items=None):
        candidates = self._candidate_items(requirement, self._unit_refs(manifests, corpus), corpus)
        if not candidates and not requirement.candidate_refs:
            candidates = self._selector.select(requirement, corpus, limit=max_items)
        return candidates, tuple(item for item in candidates if self._covers(requirement, item))

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
        for candidate in requirement.candidate_refs:
            if candidate.source_type == "mission":
                try:
                    item = corpus.item(candidate.mission_ref)
                except KeyError:
                    continue
                if item.content_hash == candidate.source_content_hash:
                    result.append(item)
                continue
            result.extend(
                item
                for item in corpus.items
                if candidate.entry_id in item.semantic_unit_ids
                and isinstance(item.ref, NamespacedMessageRef)
                and item.ref.source == candidate.source
                and item.content_hash == candidate.source_content_hash
            )
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
