"""本文件对外提供 CandidateEvidenceAdmission 与 CandidateAdmissionResult。

输入为冻结 Observation、规划候选和精确 manifests；输出为可进入机会准入的候选、结构化角色拒绝或真实证据阻断。
具体工作流为读取授权 corpus，复用 resolver 诊断引用角色，再验证必需覆盖与协议闭包；不调用模型、改写候选或提交决策。
未来交付物放在 completion_criteria，Mission 仅证明要求；示例：review = await admission.review(observation, planned, manifests)。
"""

from dataclasses import dataclass

from backend.app.desktop.context_curation import evidence_ref_key
from backend.app.desktop.agent_loop.expansion_resource_policy import resolve_expansion_resources
from .contracts import CandidateEvidenceRejection, CognitivePlanResult, ExpansionBlocker, ExpansionOpportunity
from .evidence_corpus import EvidenceCorpusReadError, FrozenEvidenceCorpusReader
from .evidence_resolver import MultiSourceEvidenceResolver


@dataclass(frozen=True, slots=True)
class CandidateAdmissionResult:
    planned: CognitivePlanResult
    rejected: tuple[CandidateEvidenceRejection, ...] = ()
    blocker: ExpansionBlocker | None = None


class CandidateEvidenceAdmission:
    VERSION = "candidate-evidence-admission-v1"

    def __init__(self, sessions, checkpointer):
        self._reader = FrozenEvidenceCorpusReader(sessions, checkpointer)
        self._resolver = MultiSourceEvidenceResolver()

    async def review(self, observation, planned, manifests):
        if not planned.work_specs:
            return CandidateAdmissionResult(planned)
        opportunities = tuple(self._opportunity(observation, spec, manifests) for spec in planned.work_specs)
        try:
            corpus = await self._reader.read(observation, opportunities[0], manifests)
        except EvidenceCorpusReadError as exc:
            return CandidateAdmissionResult(planned, blocker=ExpansionBlocker(code=exc.code, summary=exc.summary))
        resources = resolve_expansion_resources(dict(observation.budget.get("limits") or {}),
            observation.authority_revision, dict(observation.budget.get("usage") or {}))
        accepted, rejected = [], []
        blocker = None
        for opportunity in opportunities:
            invalid = self._rejections(opportunity.work_spec, manifests, corpus)
            if invalid:
                rejected.extend(invalid)
                continue
            resolved = self._resolver.resolve(opportunity, manifests, corpus,
                max_items=resources.policy.max_compiled_evidence_items)
            if isinstance(resolved, ExpansionBlocker):
                blocker = blocker or resolved
            else:
                accepted.append(opportunity.work_spec)
        known = {spec.work_spec_id for spec in accepted}
        result = planned.model_copy(update={"work_specs": tuple(accepted),
            "required_work_spec_ids": tuple(key for key in planned.required_work_spec_ids if key in known)})
        return CandidateAdmissionResult(result, tuple(rejected), blocker)

    def _rejections(self, spec, manifests, corpus):
        rejected = []
        for requirement in spec.evidence_requirements:
            candidates, covered = self._resolver.inspect_requirement(requirement, manifests, corpus)
            if candidates and not covered:
                rejected.append(CandidateEvidenceRejection(work_spec_id=spec.work_spec_id,
                    requirement_id=requirement.requirement_id, requested_role=requirement.role,
                    actual_roles=tuple(sorted({role for item in candidates for role in item.semantic_roles})),
                    evidence_identities=tuple(sorted({evidence_ref_key(item.ref) for item in candidates}))))
        return tuple(rejected)

    @staticmethod
    def _opportunity(observation, spec, manifests):
        return ExpansionOpportunity.create(loop_id=observation.loop_id, round_id=observation.round_id,
            observation_hash=observation.observed_frontier_hash, work_spec=spec,
            manifest_sources=tuple(item.source for item in manifests),
            manifest_ids=tuple(item.manifest_id for item in manifests))
