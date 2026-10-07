"""本文件对外提供 interpretation_payload、validation_diagnostic 与 legacy_candidate_failure。

输入为不可变决策输入、候选、类型化错误和已有尝试事件；输出为绑定原输入的请求与最多 64 KiB 安全诊断。
具体工作流为只投影可信引用、对其余引用保存 hash/长度、追加校验反馈并明确截断；不读数据库、不重试、不发布进度。
示例：payload = interpretation_payload(inputs, mission, events)；diagnostic = validation_diagnostic(inputs, fence, candidate, issues)。
"""

import json
from collections import Counter

from backend.app.desktop.agent_loop.task_progress.candidate_contract import safe_reference
from backend.app.desktop.agent_loop.task_progress.contracts import canonical_hash


DIAGNOSTIC_BYTES = 64 * 1024


def legacy_candidate_failure(error: str | None) -> bool:
    return error == "ValueError: 来源解释重复或引用冻结范围之外的来源"


def _encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _bounded_rows(rows, budget):
    retained = []
    size = 2
    for row in rows:
        cost = len(_encoded(row)) + 1
        if size + cost > budget:
            break
        retained.append(row)
        size += cost
    return retained


def validation_diagnostic(inputs, fence, candidate, issues):
    trusted = {source.source_key for source in inputs.task_delta.sources} | {item.item_id for item in inputs.previous_progress.items}
    projection = []
    if candidate is not None:
        for index, assessment in enumerate(candidate.source_assessments):
            projection.append({"path": ["source_assessments", index], "source_key": safe_reference(assessment.source_key, trusted),
                               "disposition": assessment.disposition})
        for index, change in enumerate(candidate.changes):
            projection.append({"path": ["changes", index], "item_id": safe_reference(change.item_id, trusted),
                               "state": change.state, "support": change.support,
                               **{field: [safe_reference(value, trusted) for value in getattr(change, field)]
                                  for field in ("evidence_keys", "corrects", "supersedes")}})
    errors = [issue.model_dump(mode="json") for issue in issues]
    budget = (DIAGNOSTIC_BYTES - 4096) // 2
    kept_errors = _bounded_rows(errors, budget)
    kept_projection = _bounded_rows(projection, budget)
    diagnostic = {"event": "candidate_validation", "diagnostic_version": 1,
                  "observation_id": inputs.observation_id, "inputs_hash": canonical_hash(inputs),
                  "manifest_hash": inputs.manifest_hash, "fence": fence, "usage_fence": fence if inputs.task_delta.sources else None,
                  "attempt_identity": f"{inputs.observation_id}:{fence}", "valid": not issues,
                  "errors": kept_errors, "error_count": len(errors), "error_counts": dict(Counter(issue.code for issue in issues)),
                  "retained_error_count": len(kept_errors), "candidate_available": candidate is not None,
                  "candidate_projection": kept_projection, "projection_count": len(projection),
                  "retained_projection_count": len(kept_projection), "projection_hash": canonical_hash(projection),
                  "truncated": len(errors) != len(kept_errors) or len(projection) != len(kept_projection)}
    if len(_encoded(diagnostic)) > DIAGNOSTIC_BYTES:
        raise ValueError("progress_memory_diagnostic_size_budget")
    return diagnostic


def interpretation_payload(inputs, mission, events=(), error=None):
    payload = {"previous_progress": inputs.previous_progress.model_dump(mode="json"),
               "task_delta": inputs.task_delta.model_dump(mode="json"), "mission": mission}
    for event in reversed(events):
        if event.get("event") == "candidate_validation":
            if event.get("diagnostic_version") != 1 or event.get("inputs_hash") != canonical_hash(inputs) or event.get("manifest_hash") != inputs.manifest_hash:
                raise ValueError("progress_memory_feedback_identity_mismatch")
            if not event["valid"]:
                payload["validation_feedback"] = {**event, "purpose": "候选修正诊断；不是新的任务来源。修正 errors 的字段，完整覆盖 task_delta，仅使用其 source_key。"}
            break
        if event.get("event") == "explicit_retry" and event.get("legacy_candidate_failure"):
            payload["validation_feedback"] = {"purpose": "候选来源引用校验曾失败；仅使用 task_delta 来源且按 source_key 唯一解释。",
                                               "evidence_missing": True, "failure_kind": "legacy_reference_validation"}
            break
    else:
        if legacy_candidate_failure(error):
            payload["validation_feedback"] = {"purpose": "候选来源引用校验曾失败；仅使用 task_delta 来源且按 source_key 唯一解释。",
                                               "evidence_missing": True, "failure_kind": "legacy_reference_validation"}
    if "validation_feedback" in payload and len(_encoded(payload["validation_feedback"])) > DIAGNOSTIC_BYTES:
        raise ValueError("progress_memory_feedback_size_budget")
    return payload
