r"""本文件对外提供 WorkSpecRelation、WorkSpecReconciliation、WorkSpecRelationEvaluatorPort 与 WorkSpecReconciler。

输入为同一冻结 observation 的 WorkContextSpec candidates、required identities 及受监督关系判定；输出为顺序无关的 canonical
specs、候选 lineage、显式冲突和稳定 reconciliation identity。具体工作流为先验证 relation 只引用已知候选及允许字段，再把
equivalent/compatible subsumption 形成连通组，以 required/evidence/questions/completion 的保守并集生成 canonical spec；workspace
或边界冲突保持分离并阻止自动糅合。示例：`result = reconciler.reconcile(specs, relations=relations)`。
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    EvidenceRequirement,
    WorkContextSpec,
    stable_expansion_hash,
)

WorkSpecRelationVerdict = Literal["equivalent", "subsumes", "distinct", "conflicting"]


class _ReconciliationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkSpecRelation(_ReconciliationModel):
    left_candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    right_candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: WorkSpecRelationVerdict
    rationale: str = Field(min_length=1, max_length=2000)
    compared_fields: tuple[str, ...] = Field(min_length=1)
    dominant_candidate_id: str | None = None

    @field_validator("compared_fields", mode="before")
    @classmethod
    def normalize_fields(cls, values):
        return tuple(sorted(set(values or ())))

    @model_validator(mode="after")
    def require_pair_contract(self) -> Self:
        if self.left_candidate_id == self.right_candidate_id:
            raise ValueError("WorkSpec relation 必须比较不同候选")
        if self.verdict == "subsumes":
            if self.dominant_candidate_id not in {self.left_candidate_id, self.right_candidate_id}:
                raise ValueError("subsumes relation 必须声明已知 dominant candidate")
        elif self.dominant_candidate_id is not None:
            raise ValueError("非 subsumes relation 不得声明 dominant candidate")
        return self


class CanonicalWorkSpecLineage(_ReconciliationModel):
    canonical_work_spec_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("candidate_ids", mode="before")
    @classmethod
    def normalize_candidates(cls, values):
        return tuple(sorted(set(values or ())))


class WorkSpecConflict(_ReconciliationModel):
    candidate_ids: tuple[str, str]
    rationale: str = Field(min_length=1, max_length=2000)

    @field_validator("candidate_ids", mode="before")
    @classmethod
    def normalize_pair(cls, values):
        pair = tuple(sorted(set(values or ())))
        if len(pair) != 2:
            raise ValueError("WorkSpec conflict 必须引用两个不同候选")
        return pair


class WorkSpecReconciliation(_ReconciliationModel):
    reconciliation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    reconciler_version: str = Field(min_length=1, max_length=64)
    input_candidate_ids: tuple[str, ...]
    relations: tuple[WorkSpecRelation, ...]
    canonical_specs: tuple[WorkContextSpec, ...]
    lineage: tuple[CanonicalWorkSpecLineage, ...]
    conflicts: tuple[WorkSpecConflict, ...]
    required_work_spec_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_complete_lineage(self) -> Self:
        known = set(self.input_candidate_ids)
        if len(known) != len(self.input_candidate_ids):
            raise ValueError("reconciliation input candidate identity 重复")
        if any({item.left_candidate_id, item.right_candidate_id} - known for item in self.relations):
            raise ValueError("reconciliation relation 引用了未知 candidate")
        canonical_ids = {item.work_spec_id for item in self.canonical_specs}
        if {item.canonical_work_spec_id for item in self.lineage} != canonical_ids:
            raise ValueError("reconciliation lineage 未覆盖 canonical specs")
        covered = [candidate for item in self.lineage for candidate in item.candidate_ids]
        if set(covered) != known or len(covered) != len(set(covered)):
            raise ValueError("reconciliation lineage 必须恰好覆盖全部 candidates")
        if not set(self.required_work_spec_ids).issubset(canonical_ids):
            raise ValueError("reconciliation required identity 不属于 canonical specs")
        expected = stable_expansion_hash(
            "work-spec-reconciliation",
            self.reconciler_version,
            self.input_candidate_ids,
            tuple(item.model_dump(mode="json") for item in self.relations),
            tuple(item.model_dump(mode="json") for item in self.canonical_specs),
            tuple(item.model_dump(mode="json") for item in self.lineage),
            tuple(item.model_dump(mode="json") for item in self.conflicts),
            self.required_work_spec_ids,
        )
        if self.reconciliation_id != expected:
            raise ValueError("reconciliation identity 与规范化结果不一致")
        return self


class WorkSpecRelationEvaluatorPort(Protocol):
    async def evaluate(
        self,
        candidates: tuple[WorkContextSpec, ...],
    ) -> tuple[WorkSpecRelation, ...]: ...


class WorkSpecRelationProposal(_ReconciliationModel):
    relations: tuple[WorkSpecRelation, ...]


class StructuredWorkSpecRelationEvaluator:
    VERSION = "structured-work-spec-relation-evaluator-v1"

    def __init__(self, model) -> None:
        self._model = model

    @property
    def attempt_records(self) -> tuple[dict[str, Any], ...]:
        return tuple(getattr(self._model, "last_attempt_records", ()))

    async def evaluate(
        self,
        candidates: tuple[WorkContextSpec, ...],
    ) -> tuple[WorkSpecRelation, ...]:
        if len(candidates) < 2:
            return ()
        result = await self._model.invoke(
            WorkSpecRelationProposal,
            "你是无权 WorkSpec Relation Evaluator。只比较输入 candidates 的 objective、questions、completion_criteria、workspace_requirement、evidence_requirements 与安全边界，对每一对返回 equivalent、subsumes、distinct 或 conflicting 及字段化理由。不得改写候选、创建 Context、选择证据或执行工作；相似主题但职责/写入边界不同必须 distinct，边界互斥必须 conflicting。",
            {"candidates": tuple(item.model_dump(mode="json") for item in candidates)},
        )
        return result.relations


class DistinctByDefaultRelationEvaluator:
    VERSION = "distinct-by-default-relation-evaluator-v1"

    async def evaluate(
        self,
        candidates: tuple[WorkContextSpec, ...],
    ) -> tuple[WorkSpecRelation, ...]:
        return ()


class WorkSpecReconciler:
    VERSION = "work-spec-reconciler-v1"

    def reconcile(
        self,
        candidates: tuple[WorkContextSpec, ...],
        *,
        relations: tuple[WorkSpecRelation, ...],
        required_candidate_ids: tuple[str, ...] = (),
    ) -> WorkSpecReconciliation:
        ordered = tuple(sorted(candidates, key=lambda item: item.work_spec_id))
        by_id = {item.work_spec_id: item for item in ordered}
        required = set(required_candidate_ids)
        if not required.issubset(by_id):
            raise ValueError("reconciliation required candidate identity 未知")
        normalized_relations = self._relations(relations, by_id)
        groups = self._groups(tuple(by_id), normalized_relations)
        group_by_candidate = {
            candidate_id: group
            for group in groups
            for candidate_id in group
        }
        if any(
            relation.verdict == "conflicting"
            and group_by_candidate[relation.left_candidate_id] == group_by_candidate[relation.right_candidate_id]
            for relation in normalized_relations
        ):
            raise ValueError("WorkSpec relation graph 同时要求合并与冲突")
        canonical_pairs = tuple(self._canonical(group, by_id) for group in groups)
        specs = tuple(sorted((pair[0] for pair in canonical_pairs), key=lambda item: item.work_spec_id))
        lineage = tuple(
            sorted(
                (
                    CanonicalWorkSpecLineage(
                        canonical_work_spec_id=spec.work_spec_id,
                        candidate_ids=group,
                    )
                    for spec, group in canonical_pairs
                ),
                key=lambda item: item.canonical_work_spec_id,
            )
        )
        required_canonical = tuple(
            sorted(
                item.canonical_work_spec_id
                for item in lineage
                if required.intersection(item.candidate_ids)
            )
        )
        conflicts = tuple(
            WorkSpecConflict(
                candidate_ids=(relation.left_candidate_id, relation.right_candidate_id),
                rationale=relation.rationale,
            )
            for relation in normalized_relations
            if relation.verdict == "conflicting"
        )
        inputs = tuple(by_id)
        payload = (
            self.VERSION,
            inputs,
            tuple(item.model_dump(mode="json") for item in normalized_relations),
            tuple(item.model_dump(mode="json") for item in specs),
            tuple(item.model_dump(mode="json") for item in lineage),
            tuple(item.model_dump(mode="json") for item in conflicts),
            required_canonical,
        )
        return WorkSpecReconciliation(
            reconciliation_id=stable_expansion_hash("work-spec-reconciliation", *payload),
            reconciler_version=self.VERSION,
            input_candidate_ids=inputs,
            relations=normalized_relations,
            canonical_specs=specs,
            lineage=lineage,
            conflicts=conflicts,
            required_work_spec_ids=required_canonical,
        )

    def _relations(
        self,
        relations: tuple[WorkSpecRelation, ...],
        candidates: dict[str, WorkContextSpec],
    ) -> tuple[WorkSpecRelation, ...]:
        by_pair: dict[tuple[str, str], WorkSpecRelation] = {}
        for relation in relations:
            left_id, right_id = sorted((relation.left_candidate_id, relation.right_candidate_id))
            pair = (left_id, right_id)
            if set(pair) - set(candidates):
                raise ValueError("WorkSpec relation 引用了未知 candidate")
            if pair in by_pair:
                raise ValueError("同一 WorkSpec pair 存在重复 relation")
            left, right = (candidates[pair[0]], candidates[pair[1]])
            if relation.verdict in {"equivalent", "subsumes"} and left.workspace_requirement != right.workspace_requirement:
                raise ValueError("workspace responsibility 不兼容的 candidates 不得自动合并")
            by_pair[pair] = relation.model_copy(
                update={"left_candidate_id": pair[0], "right_candidate_id": pair[1]}
            )
        ids = tuple(candidates)
        for left_index, left in enumerate(ids):
            for right in ids[left_index + 1 :]:
                pair = (left, right)
                if pair not in by_pair:
                    by_pair[pair] = WorkSpecRelation(
                        left_candidate_id=left,
                        right_candidate_id=right,
                        verdict="distinct",
                        rationale="没有受监督关系判定证明候选可以合并",
                        compared_fields=("objective", "questions", "completion_criteria", "workspace_requirement", "evidence_requirements"),
                    )
        return tuple(by_pair[pair] for pair in sorted(by_pair))

    @staticmethod
    def _groups(
        candidate_ids: tuple[str, ...],
        relations: tuple[WorkSpecRelation, ...],
    ) -> tuple[tuple[str, ...], ...]:
        parent = {candidate_id: candidate_id for candidate_id in candidate_ids}

        def find(candidate_id: str) -> str:
            while parent[candidate_id] != candidate_id:
                parent[candidate_id] = parent[parent[candidate_id]]
                candidate_id = parent[candidate_id]
            return candidate_id

        for relation in relations:
            if relation.verdict not in {"equivalent", "subsumes"}:
                continue
            left = find(relation.left_candidate_id)
            right = find(relation.right_candidate_id)
            if left != right:
                parent[max(left, right)] = min(left, right)
        groups: dict[str, list[str]] = {}
        for candidate_id in candidate_ids:
            groups.setdefault(find(candidate_id), []).append(candidate_id)
        return tuple(sorted((tuple(sorted(group)) for group in groups.values()), key=lambda item: item[0]))

    def _canonical(
        self,
        candidate_ids: tuple[str, ...],
        candidates: dict[str, WorkContextSpec],
    ) -> tuple[WorkContextSpec, tuple[str, ...]]:
        specs = tuple(candidates[candidate_id] for candidate_id in candidate_ids)
        if len(specs) == 1:
            return specs[0], candidate_ids
        requirements = self._requirements(specs)
        objective = self._joined(item.objective for item in specs)
        separation = self._joined(item.separation_reason for item in specs)
        canonical = WorkContextSpec.create(
            planner_version=self.VERSION,
            objective=objective,
            separation_reason=separation,
            questions=tuple(question for item in specs for question in item.questions),
            completion_criteria=tuple(criterion for item in specs for criterion in item.completion_criteria),
            workspace_requirement=specs[0].workspace_requirement,
            evidence_requirements=requirements,
        )
        return canonical, candidate_ids

    @staticmethod
    def _requirements(specs: tuple[WorkContextSpec, ...]) -> tuple[EvidenceRequirement, ...]:
        grouped: dict[str, list[EvidenceRequirement]] = {}
        for spec in specs:
            for requirement in spec.evidence_requirements:
                grouped.setdefault(requirement.requirement_id, []).append(requirement)
        result = []
        for requirement_id, variants in sorted(grouped.items()):
            first = variants[0]
            if any(
                item.role != first.role or item.source_constraints != first.source_constraints
                for item in variants[1:]
            ):
                suffix = stable_expansion_hash("requirement-variants", tuple(item.model_dump(mode="json") for item in variants))[:12]
                for index, item in enumerate(sorted(variants, key=lambda value: str(value.model_dump(mode="json")))):
                    result.append(item.model_copy(update={"requirement_id": f"{requirement_id}-{suffix}-{index}"}))
                continue
            result.append(
                first.model_copy(
                    update={
                        "question": WorkSpecReconciler._joined(item.question for item in variants),
                        "coverage_criterion": WorkSpecReconciler._joined(item.coverage_criterion for item in variants),
                        "necessity": "required" if any(item.necessity == "required" for item in variants) else "optional",
                        "candidate_unit_ids": tuple(
                            sorted({unit_id for item in variants for unit_id in item.candidate_unit_ids})
                        ),
                    }
                )
            )
        return tuple(result)

    @staticmethod
    def _joined(values) -> str:
        normalized = sorted({" ".join(str(value).split()) for value in values if str(value).strip()})
        return " / ".join(normalized)
