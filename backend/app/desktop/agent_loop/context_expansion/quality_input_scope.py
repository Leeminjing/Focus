"""本文件对外提供 quality_input_scope 创建前质量审查的只读用途投影。

输入为同一冻结 WorkContextSpec、ResolvedEvidenceBundle、ValidatedContextDossier；输出为绑定三者身份的JSON读面，
说明当前评价的是执行工作的输入充分性、目标Context身份在发布时生成、完成判据在执行后核验，以及精确协议专用成员。
具体工作流为从实际requirement和claim引用派生用途，复用共享完整Tool Exchange闭包标记额外必需成员，保留原文、来源与所有冻结身份；
本模块不产生目标身份或语义verdict。示例：`payload["evaluation_scope"] = quality_input_scope(work_spec, bundle, dossier)`。
平台能力与问题调查计划同时投影；审计表示重复不等于语义冗余，研究结果归执行阶段，不在此宣布充分性通过。
"""

from __future__ import annotations

from typing import Any

from backend.app.desktop.agent_loop.context_expansion.contracts import ResolvedEvidenceBundle, WorkContextSpec
from backend.app.desktop.agent_loop.context_expansion.synthesis import ValidatedContextDossier
from backend.app.desktop.agent_loop.context_expansion.tool_exchange_closure import tool_exchange_closure
from backend.app.desktop.context_curation import evidence_ref_key


def quality_input_scope(
    work_spec: WorkContextSpec,
    bundle: ResolvedEvidenceBundle,
    dossier: ValidatedContextDossier,
) -> dict[str, Any]:
    required = {evidence_ref_key(item.ref) for item in bundle.items}
    cited = {evidence_ref_key(ref) for claim in dossier.claims for ref in claim.citations}
    seeds = required | cited
    selected = tuple(ref for ref in bundle.evidence_frontier if evidence_ref_key(ref) in seeds)
    closure = {evidence_ref_key(ref) for ref in tool_exchange_closure(bundle.evidence, selected)}
    return {
        "contract_version": "context-quality-input-scope-v2",
        "work_spec_id": work_spec.work_spec_id,
        "resolution_id": bundle.resolution_id,
        "dossier_id": dossier.dossier_id,
        "phase": "before_compilation_publication_execution",
        "evaluation_target": "input_adequacy_for_work_spec",
        "completion_criteria_phase": "after_execution",
        "destination_identity_phase": "publication",
        "existing_source_identities": [source.model_dump(mode="json") for source in bundle.source_frontier],
        "requirement_evidence_keys": [list(key) for key in sorted(required)],
        "claim_evidence_keys": [list(key) for key in sorted(cited)],
        "protocol_only_evidence_keys": [list(key) for key in sorted(closure - seeds)],
        "provenance_purpose": "Evidence, citations and support assessments preserve auditability; their required duplication alone is not semantic redundancy.",
        "semantic_minimality_target": "task-relevant dossier statements and necessary global boundaries",
        "execution_readiness": dossier.execution_readiness.model_dump(mode="json") if dossier.execution_readiness else None,
        "question_dispositions": [p.model_dump(mode="json") for p in dossier.question_dispositions],
        "research_result_phase": "after_execution_for_feasible_declared_research",
    }
