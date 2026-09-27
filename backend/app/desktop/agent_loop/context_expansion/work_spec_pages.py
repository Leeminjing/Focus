r"""本文件对外提供 combine_page_work_specs。

输入为同一冻结 planning session 中各证据页产生的 WorkContextDraft；输出为不丢失已引用证据的跨页工作草案。
具体工作流为仅归并目标、分离理由和 workspace 职责相同的草案；同名但内容不同的 requirement 各获稳定身份并保持独立覆盖，其他职责交给后续受监督 reconciliation。
示例：`drafts = combine_page_work_specs(tuple(page_drafts))`。
"""

from __future__ import annotations

from backend.app.desktop.agent_loop.context_expansion.contracts import EvidenceRequirement, WorkContextDraft, stable_expansion_hash


def combine_page_work_specs(drafts: tuple[WorkContextDraft, ...]) -> tuple[WorkContextDraft, ...]:
    groups: dict[tuple[str, str, str], list[WorkContextDraft]] = {}
    for draft in drafts:
        key = (
            " ".join(draft.objective.split()).casefold(),
            " ".join(draft.separation_reason.split()).casefold(),
            draft.workspace_requirement,
        )
        groups.setdefault(key, []).append(draft)
    combined: list[WorkContextDraft] = []
    for group in groups.values():
        combined.append(_combine_group(tuple(group)))
    return tuple(combined)


def _combine_group(group: tuple[WorkContextDraft, ...]) -> WorkContextDraft:
    if len(group) == 1:
        return group[0]
    requirements: dict[str, list[EvidenceRequirement]] = {}
    for draft in group:
        for requirement in draft.evidence_requirements:
            variants = requirements.setdefault(requirement.requirement_id, [])
            if requirement not in variants:
                variants.append(requirement)
    resolved: list[EvidenceRequirement] = []
    for requirement_id, variants in sorted(requirements.items()):
        if len(variants) == 1:
            resolved.append(variants[0])
            continue
        for variant in variants:
            suffix = stable_expansion_hash("page-evidence-requirement", variant.model_dump(mode="json"))[:12]
            resolved.append(variant.model_copy(update={"requirement_id": f"{requirement_id[:107]}-{suffix}"}))
    first = group[0]
    return WorkContextDraft(
        objective=first.objective,
        separation_reason=first.separation_reason,
        questions=tuple(sorted({item for draft in group for item in draft.questions})),
        completion_criteria=tuple(sorted({item for draft in group for item in draft.completion_criteria})),
        workspace_requirement=first.workspace_requirement,
        evidence_requirements=tuple(sorted(resolved, key=lambda item: item.requirement_id)),
        required=any(draft.required for draft in group),
    )
