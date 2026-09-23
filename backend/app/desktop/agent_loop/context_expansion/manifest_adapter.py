r"""本文件对外提供 ManifestUnitDraft、WorkerResultManifestProjector 与 CompositeSemanticManifestProjector。

输入为冻结 Loop observation 中受监督 semantic-manifest worker 的结构化 unit drafts；输出为带精确 message refs 的
ManifestProjectionResult 或生产使用的 ContextSemanticManifest 集合。具体工作流为严格解析 schema、绑定 frozen
revision/message identity、验证 confirmed statement 可在所引原文中逐字找到，再用受监督分类补充保留稳定 candidate identity 的
基础 evidence units；malformed、未知引用或 unsupported confirmation 返回 projection failure。
示例：`manifests = CompositeSemanticManifestProjector().project(observation)`。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    ContextSemanticManifest,
    DerivationStageFailure,
    ManifestProjectionResult,
    SemanticAuthority,
    SemanticEvidenceUnit,
    SemanticUnitKind,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_manifest import (
    SemanticManifestProjector,
)
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.context_curation import NamespacedMessageRef
from backend.app.desktop.context_evolution import ContextRevisionRef


class ManifestUnitDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision_id: str = Field(min_length=1)
    kind: SemanticUnitKind
    authority: SemanticAuthority
    statement: str = Field(min_length=1, max_length=4000)
    message_ids: tuple[str, ...] = Field(min_length=1)


_DRAFTS = TypeAdapter(tuple[ManifestUnitDraft, ...])


class WorkerResultManifestProjector:
    VERSION = "worker-semantic-manifest-v1"

    def project(self, observation: LoopObservationEnvelope) -> ManifestProjectionResult:
        results = tuple(
            item
            for item in observation.worker_results
            if item.get("kind") == "semantic_manifest_projector"
            or (
                item.get("kind") == "lane_curator"
                and "manifest_units" in (item.get("result") or {})
            )
        )
        if not results:
            return self._failure("manifest_result_missing", "冻结 observation 没有 semantic manifest worker 结果")
        failures = tuple(item for item in results if item.get("status") in {"error", "failed"})
        payloads = tuple(
            item.get("result") or {}
            for item in results
            if item.get("status") in {"success", "proposed", "consumed"}
        )
        if not payloads and failures:
            return self._failure("manifest_worker_failed", "全部 semantic manifest worker 均失败", retryable=True)
        try:
            drafts = tuple(
                draft
                for payload in payloads
                for draft in _DRAFTS.validate_python(
                    payload.get("units")
                    if payload.get("units") is not None
                    else payload.get("manifest_units") or ()
                )
            )
            manifests = self._manifests(observation, drafts)
        except (KeyError, TypeError, ValidationError, ValueError) as exc:
            return self._failure("manifest_contract_invalid", str(exc)[:1800])
        return ManifestProjectionResult(manifests=manifests)

    def _manifests(
        self,
        observation: LoopObservationEnvelope,
        drafts: tuple[ManifestUnitDraft, ...],
    ) -> tuple[ContextSemanticManifest, ...]:
        objective = str((observation.mission or observation.goal or {}).get("outcome") or "Current mission")
        frontier = {
            str(item.get("revision_id")): item
            for item in observation.portfolio_frontier
            if item.get("revision") and item.get("content_hash")
        }
        unknown_revisions = {draft.revision_id for draft in drafts} - set(frontier)
        if unknown_revisions:
            raise ValueError("manifest worker 引用了冻结 frontier 之外的 revision")
        manifests = []
        for revision_id, item in sorted(frontier.items()):
            source = ContextRevisionRef.model_validate(item["revision"])
            messages = {
                str(message.get("message_id") or message.get("id")): message
                for message in tuple(item.get("message_evidence_preview") or ())
            }
            units = tuple(
                self._unit(source, messages, draft)
                for draft in drafts
                if draft.revision_id == revision_id
            )
            manifests.append(
                ContextSemanticManifest.create(
                    source=source,
                    source_content_hash=str(item["content_hash"]),
                    projector_version=self.VERSION,
                    role=str(item.get("role") or "context"),
                    active_objective=objective,
                    units=units,
                )
            )
        return tuple(manifests)

    @staticmethod
    def _unit(
        source: ContextRevisionRef,
        messages: dict[str, dict[str, Any]],
        draft: ManifestUnitDraft,
    ) -> SemanticEvidenceUnit:
        unknown = set(draft.message_ids) - set(messages)
        if unknown:
            raise ValueError("manifest worker 引用了冻结 revision 中不存在的 message")
        contents = tuple(str(messages[message_id].get("content") or "") for message_id in draft.message_ids)
        if draft.authority == "confirmed" and not any(
            draft.statement.casefold() in content.casefold()
            for content in contents
        ):
            raise ValueError("confirmed manifest statement 无法由所引 message 原文支持")
        return SemanticEvidenceUnit.create(
            kind=draft.kind,
            authority=draft.authority,
            statement=draft.statement,
            evidence_refs=tuple(
                NamespacedMessageRef(source=source, message_id=message_id)
                for message_id in sorted(set(draft.message_ids))
            ),
        )

    @staticmethod
    def _failure(code: str, summary: str, retryable: bool = False) -> ManifestProjectionResult:
        return ManifestProjectionResult(
            failure=DerivationStageFailure(
                stage="portfolio_projection",
                code=code,
                summary=summary,
                retryable=retryable,
            )
        )


class CompositeSemanticManifestProjector:
    VERSION = "composite-semantic-manifest-v2"

    def __init__(
        self,
        base: SemanticManifestProjector | None = None,
        worker: WorkerResultManifestProjector | None = None,
    ) -> None:
        self._base = base or SemanticManifestProjector()
        self._worker = worker or WorkerResultManifestProjector()

    def project(self, observation: LoopObservationEnvelope) -> tuple[ContextSemanticManifest, ...]:
        base = self._base.project(observation)
        has_worker_units = any(
            item.get("kind") in {"semantic_manifest_projector", "lane_curator"}
            and (
                "units" in (item.get("result") or {})
                or "manifest_units" in (item.get("result") or {})
            )
            for item in observation.worker_results
        )
        if not has_worker_units:
            return base
        projected = self._worker.project(observation)
        if projected.failure is not None:
            raise ValueError(projected.failure.summary)
        worker_by_revision = {
            item.source.revision_id: item
            for item in projected.manifests
        }
        return tuple(
            self._merge(item, worker_by_revision.get(item.source.revision_id))
            for item in base
        )

    def _merge(
        self,
        base: ContextSemanticManifest,
        worker: ContextSemanticManifest | None,
    ) -> ContextSemanticManifest:
        if worker is None or not worker.units:
            return base
        units = {
            unit.unit_id: unit
            for unit in (*worker.units, *base.units)
        }
        return ContextSemanticManifest.create(
            source=base.source,
            source_content_hash=base.source_content_hash,
            projector_version=self.VERSION,
            role=base.role,
            active_objective=base.active_objective,
            units=tuple(units.values()),
        )
