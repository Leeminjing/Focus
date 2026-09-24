r"""本文件对外提供 ManifestUnitDraft、WorkerResultManifestProjector 与 CompositeSemanticManifestProjector。

输入为冻结 Loop observation 中服务端 retrieval session 已验证的 manifests，或显式 semantic-manifest worker unit drafts；输出为带精确
message refs 的 ManifestProjectionResult 或生产使用的 ContextSemanticManifest 集合。具体工作流为校验 retrieved manifest 的
Revision/hash 均属于 frozen frontier，并拒绝缺少 retrieval-backed units 的生产 observation；不从 bounded preview 重建 Planner 证据。
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
_MANIFESTS = TypeAdapter(tuple[ContextSemanticManifest, ...])


class WorkerResultManifestProjector:
    VERSION = "worker-semantic-manifest-v1"

    def project(self, observation: LoopObservationEnvelope) -> ManifestProjectionResult:
        results = tuple(
            item
            for item in observation.worker_results
            if item.get("kind") == "semantic_manifest_projector"
            or (
                item.get("kind") == "lane_curator"
                and (
                    "manifest_units" in (item.get("result") or {})
                    or "retrieved_manifests" in (item.get("result") or {})
                )
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
            retrieved = tuple(
                manifest
                for payload in payloads
                for manifest in _MANIFESTS.validate_python(payload.get("retrieved_manifests") or ())
            )
            if retrieved:
                return ManifestProjectionResult(
                    manifests=self._validated_retrieved(observation, retrieved)
                )
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

    @staticmethod
    def _validated_retrieved(
        observation: LoopObservationEnvelope,
        manifests: tuple[ContextSemanticManifest, ...],
    ) -> tuple[ContextSemanticManifest, ...]:
        frontier = {
            str(item.get("revision_id") or (item.get("revision") or {}).get("revision_id") or ""): item
            for item in observation.portfolio_frontier
        }
        grouped: dict[str, list[ContextSemanticManifest]] = {}
        for manifest in manifests:
            frozen = frontier.get(manifest.source.revision_id)
            if frozen is None:
                raise ValueError("retrieved manifest 引用了 frozen frontier 之外的 Revision")
            if str(frozen.get("content_hash") or "") != manifest.source_content_hash:
                raise ValueError("retrieved manifest content hash 已 stale")
            revision = frozen.get("revision")
            if revision is not None and ContextRevisionRef.model_validate(revision) != manifest.source:
                raise ValueError("retrieved manifest Revision identity 与 frontier 不一致")
            grouped.setdefault(manifest.source.revision_id, []).append(manifest)
        merged = []
        for revision_id, variants in sorted(grouped.items()):
            first = variants[0]
            if any(
                item.source != first.source
                or item.source_content_hash != first.source_content_hash
                or item.projector_version != first.projector_version
                for item in variants[1:]
            ):
                raise ValueError("同一 Revision 的 retrieved manifests 冻结 identity 不一致")
            units = {
                unit.unit_id: unit
                for item in variants
                for unit in item.units
            }
            merged.append(
                ContextSemanticManifest.create(
                    source=first.source,
                    source_content_hash=first.source_content_hash,
                    projector_version=first.projector_version,
                    role=first.role,
                    active_objective=first.active_objective,
                    units=tuple(units.values()),
                )
            )
        return tuple(merged)

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
    VERSION = "retrieval-only-semantic-manifest-v3"

    def __init__(
        self,
        worker: WorkerResultManifestProjector | None = None,
    ) -> None:
        self._worker = worker or WorkerResultManifestProjector()

    def project(self, observation: LoopObservationEnvelope) -> tuple[ContextSemanticManifest, ...]:
        has_worker_units = any(
            item.get("kind") == "lane_curator"
            and "retrieved_manifests" in (item.get("result") or {})
            for item in observation.worker_results
        )
        if not has_worker_units:
            if observation.portfolio_frontier:
                raise ValueError("context expansion 缺少 retrieval-backed semantic manifests")
            return ()
        projected = self._worker.project(observation)
        if projected.failure is not None:
            raise ValueError(projected.failure.summary)
        return projected.manifests
