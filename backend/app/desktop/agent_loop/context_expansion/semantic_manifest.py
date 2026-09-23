r"""本文件对外提供 SemanticManifestProjector。

输入为冻结 `LoopObservationEnvelope` 中带精确 Revision、content hash 与 message evidence preview 的 Portfolio frontier；
输出为按 Revision hash/version 缓存的 `ContextSemanticManifest` 集合。具体工作流为先验证 scope、frontier identity、
hash 与 message identity，再把每条可引用预览保留为带 `NamespacedMessageRef` 的 semantic unit，保持显式 semantic kind
并拒绝无支撑、越界或不完整来源。
示例：`manifests = SemanticManifestProjector().project(observation)`。
"""

from __future__ import annotations

import json
from typing import Any

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    ContextSemanticManifest,
    SemanticEvidenceUnit,
    stable_expansion_hash,
)
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.context_curation import NamespacedMessageRef
from backend.app.desktop.context_evolution import ContextRevisionRef


class SemanticManifestProjector:
    VERSION = "semantic-manifest-projector-v1"

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, str], ContextSemanticManifest] = {}

    def project(self, observation: LoopObservationEnvelope) -> tuple[ContextSemanticManifest, ...]:
        objective = str((observation.mission or observation.goal or {}).get("outcome") or "Current mission")
        scope = set((observation.grant or {}).get("context_scope") or ())
        manifests = tuple(
            self._project_one(item, objective, scope)
            for item in observation.portfolio_frontier
            if item.get("revision") and item.get("content_hash")
        )
        return tuple(sorted(manifests, key=lambda item: item.source.revision_id))

    def _project_one(
        self,
        item: dict[str, Any],
        objective: str,
        scope: set[str],
    ) -> ContextSemanticManifest:
        source = ContextRevisionRef.model_validate(item["revision"])
        self._validate_frontier_identity(item, source, scope)
        content_hash = str(item["content_hash"])
        cache_key = (source.revision_id, content_hash, self.VERSION)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        messages = tuple(item.get("message_evidence_preview") or ())
        message_ids = [str(message.get("message_id") or message.get("id") or "") for message in messages]
        if any(not message_id for message_id in message_ids):
            raise ValueError("semantic manifest source 包含无 identity 的 message preview")
        if len(message_ids) != len(set(message_ids)):
            raise ValueError("semantic manifest source 的 message identity 重复")
        units = tuple(
            unit
            for message in messages
            if (unit := self._unit(source, message)) is not None
        )
        manifest = ContextSemanticManifest.create(
            source=source,
            source_content_hash=content_hash,
            projector_version=self.VERSION,
            role=str(item.get("role") or "context"),
            active_objective=objective,
            units=units,
        )
        self._cache[cache_key] = manifest
        return manifest

    def _unit(
        self,
        source: ContextRevisionRef,
        message: dict[str, Any],
    ) -> SemanticEvidenceUnit | None:
        message_id = str(message.get("message_id") or message.get("id") or "")
        if not message_id:
            return None
        statement = self._statement(message)
        if not statement:
            return None
        role = str(message.get("role") or "")
        status = str(message.get("status") or "").casefold()
        explicit_kind = str(message.get("semantic_kind") or "").casefold()
        supported = {
            "decision",
            "claim",
            "hypothesis",
            "unresolved_question",
            "implementation_effect",
            "verification_result",
            "failure",
        }
        if explicit_kind and explicit_kind not in supported:
            raise ValueError(f"semantic manifest 包含未知 semantic kind: {explicit_kind}")
        kind = explicit_kind or self._default_kind(role, status)
        authority = str(message.get("semantic_authority") or "confirmed").casefold()
        if authority not in {"confirmed", "hypothesis"}:
            raise ValueError(f"semantic manifest 包含未知 authority: {authority}")
        return SemanticEvidenceUnit.create(
            kind=kind,
            authority=authority,
            statement=statement,
            evidence_refs=(NamespacedMessageRef(source=source, message_id=message_id),),
        )

    @staticmethod
    def _default_kind(role: str, status: str) -> str:
        if role == "tool" and status in {"error", "failed", "failure"}:
            return "failure"
        if role == "tool":
            return "verification_result"
        return "claim"

    @staticmethod
    def _validate_frontier_identity(
        item: dict[str, Any],
        source: ContextRevisionRef,
        scope: set[str],
    ) -> None:
        if scope and source.context_id not in scope:
            raise ValueError("semantic manifest source 超出 delegation context scope")
        if str(item.get("context_id") or "") != source.context_id:
            raise ValueError("semantic manifest context identity 与 frontier 不一致")
        if str(item.get("revision_id") or "") != source.revision_id:
            raise ValueError("semantic manifest revision identity 与 frontier 不一致")

    @staticmethod
    def _statement(message: dict[str, Any]) -> str:
        content = message.get("content", "")
        if isinstance(content, str) and content.strip():
            return content.strip()[:4000]
        if content:
            return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)[:4000]
        tool_calls = tuple(message.get("tool_calls") or ())
        if tool_calls:
            return json.dumps(tool_calls, ensure_ascii=False, sort_keys=True, default=str)[:4000]
        return ""

    @classmethod
    def content_hash(cls, messages: tuple[dict[str, Any], ...]) -> str:
        return stable_expansion_hash("manifest-source", messages)
