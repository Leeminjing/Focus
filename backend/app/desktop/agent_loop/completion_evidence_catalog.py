"""本文件对外提供 CompletionEvidenceCatalog 的冻结完成证据读面。

输入为冻结 Worker payload、同一 Loop/revision、实际来源和秘密值；输出为完整历史目录、当前可采纳 result_key 及版本化输入 hash。
具体工作流为逐项复用 CompletionSourceValidator.qualify 分类，旧聚合结果仅从同一已完成工具审计解析派生元数据；
当前 Artifact 派生完整脱敏正文，历史比较明确禁用新正文补读；保持原 payload/历史不变。
不裁目录、不自行认定检查通过、不提交领域记录。示例：view = await catalog.project(session, frozen, loop_id, revision, secrets=keys)。
"""

from copy import deepcopy

from sqlalchemy import select

from backend.app.desktop.agent_loop.completion_sources import CompletionSourceValidator, CompletionSourceRejection
from backend.app.desktop.agent_loop.artifact_content import ArtifactContentProjection
from backend.app.desktop.domain_evidence.identity import canonical_hash
from backend.app.desktop.domain_evidence.tests import TestResultParser
from backend.app.desktop.models import ToolExecutionAttempt


class CompletionEvidenceCatalog:
    async def project(self, session, frozen, loop_id, revision, *, secrets=(), include_artifact_content=True):
        view = deepcopy(frozen)
        sources, eligible = [], []
        for raw in frozen.get("completion_sources", ()):
            entry = deepcopy(raw)
            entry.pop('artifact_content', None)
            key, kind = entry["source_id"], entry["kind"]
            try:
                source = await CompletionSourceValidator().qualify(session, loop_id, revision, key,
                    "user" if kind == "user_revision" else kind, user_verification=kind == "user_revision", frozen_payload=raw["payload"])
                entry["eligibility"] = {"status": "available", "code": None}
                entry["allowed_evidence_kinds"] = ["test", "tool", "fact"] if kind == "test" else ["user"] if kind == "user_revision" else [kind]
                if kind == "test":
                    entry["payload"] = await self._named_results(session, source, secrets)
                elif kind == "artifact" and include_artifact_content:
                    entry["artifact_content"] = await ArtifactContentProjection().project(session, source, revision, secrets=secrets)
                eligible.append(key)
            except CompletionSourceRejection as exc:
                state = {"source_stale": "stale", "workspace_changed": "stale", "source_unsettled": "unsettled", "source_failed": "failed"}.get(exc.code, "unsupported")
                entry["eligibility"] = {"status": state, "code": exc.code, "expected_revision": revision, "actual_revision": exc.actual_revision}
                entry["allowed_evidence_kinds"] = []
            sources.append(entry)
        view["completion_sources"] = sources
        view["available_source_ids"] = eligible
        view["completion_view"] = {"contract": "completion-current-sources-v2" if include_artifact_content else "completion-current-sources-v1", "frozen_input_hash": canonical_hash(frozen)}
        view["completion_view"]["input_hash"] = canonical_hash(view)
        return view

    async def _named_results(self, session, source, secrets):
        payload = deepcopy(source.payload)
        if "named_results" in payload:
            return payload
        payload["named_results"] = {"status": "unknown", "cases": [], "suites": []}
        proof = payload.get("execution_proof") or {}
        attempts = tuple(await session.scalars(select(ToolExecutionAttempt).where(
            ToolExecutionAttempt.run_id == source.run_id, ToolExecutionAttempt.call_id == proof.get("call_id"),
            ToolExecutionAttempt.status == "completed")))
        if len(attempts) != 1:
            return payload
        message = attempts[0].result or {}
        content = message.get("content")
        if not isinstance(content, str):
            return payload
        parsed = TestResultParser.parse(message.get("name"), content, command=proof.get("command"),
            run_id=source.run_id, call_id=proof.get("call_id"), secrets=secrets)
        if parsed is not None and parsed.get("execution_proof") == proof and "named_results" in parsed:
            payload["named_results"] = parsed["named_results"]
            payload["metadata_derivation"] = {"attempt_id": attempts[0].attempt_id, "call_hash": attempts[0].call_hash,
                "source_result_key": source.result_key, "contract": "trusted-test-report-v1"}
        return payload
