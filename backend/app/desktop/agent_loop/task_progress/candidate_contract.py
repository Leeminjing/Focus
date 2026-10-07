"""本文件对外提供 candidate_schema、validate_candidate、structural_error、safe_reference 和 CandidateValidationError。

输入为完整冻结决策输入及模型候选；输出为绑定来源的请求模型、规范合法候选或安全类型化错误。
具体工作流为派生请求枚举、重新验证候选结构、统一验证来源覆盖和任务修正证据；不清理非法条目、不调用模型或数据库。
示例：schema = candidate_schema(inputs)；candidate = validate_candidate(inputs, response)。
"""

from typing import Literal

from pydantic import BaseModel, ValidationError, create_model, field_validator

from backend.app.desktop.agent_loop.task_progress.contracts import (
    CandidateIssue, ProgressCandidate, RoundDecisionInputs, SourceAssessment, TaskItem, canonical_hash,
)


_MESSAGES = {
    "duplicate_source_assessment": "来源解释重复",
    "unknown_assessment_source": "来源解释引用冻结范围之外的来源",
    "unknown_change_evidence": "任务变化必须仅引用本轮冻结领域来源",
    "uncovered_source": "全部冻结来源必须被解释，不能静默吸收",
    "duplicate_item": "同一候选重复修正事项",
    "superseded_item_changed": "被替代事项不能在同一候选中再次修改",
    "unknown_correction": "修正必须引用已存在的任务 identity",
    "invalid_supersession": "替代必须引用前序其他任务 identity",
    "missing_evidence": "任务变化必须仅引用本轮冻结领域来源",
    "missing_correction": "修改旧事项必须显式 corrects 原 identity",
    "unsupported_claim": "Agent 自述不能单独构成 supported",
    "unverified_completion": "失败或未知测试不能支持完成",
}
_FIELDS = frozenset(ProgressCandidate.model_fields) | frozenset(TaskItem.model_fields) | frozenset(SourceAssessment.model_fields)


class CandidateValidationError(ValueError):
    def __init__(self, issues: tuple[CandidateIssue, ...], candidate=None):
        self.issues = issues
        self.candidate = candidate
        message = _MESSAGES.get(issues[0].code, "候选结构不符合合同")
        super().__init__(f"{message}: {issues[0].code} ({len(issues)} issues)")


def safe_reference(value: str, trusted: set[str]) -> dict:
    if value in trusted:
        return {"value": value}
    return {"hash": canonical_hash(value), "length": len(value)}


def structural_error(error: ValidationError) -> CandidateValidationError:
    issues = []
    for item in error.errors(include_context=False, include_url=False):
        location = item["loc"]
        path = tuple(part if isinstance(part, int) or part in _FIELDS else "<field>" for part in location) or ("candidate",)
        code = f"schema_{item['type']}"
        refs = ()
        if item["type"] == "literal_error" and location:
            if location[0] == "source_assessments" and location[-1] == "source_key":
                code = "unknown_assessment_source"
            elif location[0] == "changes" and "evidence_keys" in location:
                code = "unknown_change_evidence"
            if code in {"unknown_assessment_source", "unknown_change_evidence"} and isinstance(item.get("input"), str):
                refs = (safe_reference(item["input"], set()),)
        issues.append(CandidateIssue(code=code, path=path, references=refs))
    return CandidateValidationError(tuple(issues))


def _canonical_nested_input(cls, value):
    if isinstance(value, (list, tuple)):
        return tuple(item.model_dump(warnings=False) if isinstance(item, BaseModel) else item for item in value)
    return value


def candidate_schema(inputs: RoundDecisionInputs) -> type[ProgressCandidate]:
    keys = tuple(source.source_key for source in inputs.task_delta.sources)
    if not keys:
        return ProgressCandidate
    reference = Literal[keys]
    assessment = create_model("FrozenSourceAssessment", __base__=SourceAssessment, source_key=(reference, ...))
    item = create_model("FrozenTaskItem", __base__=TaskItem, evidence_keys=(tuple[reference, ...], ()))
    return create_model("FrozenProgressCandidate", __base__=ProgressCandidate,
                        changes=(tuple[item, ...], ()), source_assessments=(tuple[assessment, ...], ()),
                        __validators__={"canonical_nested_input": field_validator("changes", "source_assessments", mode="before")(_canonical_nested_input)})


def validate_candidate(inputs: RoundDecisionInputs, value) -> ProgressCandidate:
    if not inputs.task_delta.complete:
        raise ValueError(inputs.task_delta.blocker or "任务增量不完整")
    try:
        raw = value.model_dump(warnings=False) if isinstance(value, BaseModel) else value
        candidate = ProgressCandidate.model_validate(raw)
    except ValidationError as error:
        raise structural_error(error) from None
    sources = {source.source_key: source for source in inputs.task_delta.sources}
    previous = {item.item_id: item for item in inputs.previous_progress.items}
    trusted = set(sources) | set(previous)
    issues = []

    def issue(code, path, *references):
        issues.append(CandidateIssue(code=code, path=path,
                                     references=tuple(safe_reference(ref, trusted) for ref in references)))

    assessed = set()
    for index, assessment in enumerate(candidate.source_assessments):
        key = assessment.source_key
        path = ("source_assessments", index, "source_key")
        if key in assessed:
            issue("duplicate_source_assessment", path, key)
        if key not in sources:
            issue("unknown_assessment_source", path, key)
        assessed.add(key)
    referenced = _change_references(candidate, sources, previous, issue)
    for key in sorted(set(sources) - (referenced | assessed)):
        issue("uncovered_source", ("task_delta", "sources"), key)
    if issues:
        raise CandidateValidationError(tuple(issues), candidate)
    return candidate


def _change_references(candidate, sources, previous, issue):
    changed = set()
    referenced = set()
    all_changed = {item.item_id for item in candidate.changes}
    for index, change in enumerate(candidate.changes):
        path = ("changes", index)
        if change.item_id in changed:
            issue("duplicate_item", (*path, "item_id"), change.item_id)
        changed.add(change.item_id)
        for position, key in enumerate(change.evidence_keys):
            if key not in sources:
                issue("unknown_change_evidence", (*path, "evidence_keys", position), key)
            referenced.add(key)
        if not change.evidence_keys:
            issue("missing_evidence", (*path, "evidence_keys"))
        for position, identity in enumerate(change.corrects):
            if identity not in previous:
                issue("unknown_correction", (*path, "corrects", position), identity)
        for position, identity in enumerate(change.supersedes):
            if identity == change.item_id or identity not in previous:
                issue("invalid_supersession", (*path, "supersedes", position), identity)
            if identity in all_changed:
                issue("superseded_item_changed", (*path, "supersedes", position), identity)
        old = previous.get(change.item_id)
        if old is not None and old != change and old.item_id not in change.corrects:
            issue("missing_correction", (*path, "corrects"), old.item_id)
        evidence = [sources[key] for key in change.evidence_keys if key in sources]
        if change.support == "supported" and not any(source.kind in {"test", "workspace", "artifact"} for source in evidence):
            issue("unsupported_claim", (*path, "support"))
        if change.state == "completed" and any(source.kind == "test" and source.payload.get("status") != "verified" for source in evidence):
            issue("unverified_completion", (*path, "state"))
    return referenced
