r"""本文件对外提供 FrozenEvidenceCorpus、CorpusEvidenceItem、FrozenEvidenceAuthority 与 FrozenEvidenceCorpusReader。

输入为冻结 Loop observation、semantic manifests、WorkContext opportunity、精确 Context Revision 存储与 checkpointer；
输出为只含授权版本的不可变 evidence corpus。具体工作流为校验 frontier/scope/hash，读取每个精确 Revision 的完整消息，
再把 Mission、Run 与 Workspace 事实编码为类型化 evidence，并用 manifest unit 保留语义角色。示例：
`corpus = await reader.read(observation, opportunity, manifests)`。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    ContextSemanticManifest,
    EvidenceRole,
    ExpansionOpportunity,
    stable_expansion_hash,
)
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.context_curation import (
    EvidenceRef,
    MaterialEvidenceRef,
    MissionEvidenceRef,
    MultiSourceEvidence,
    NamespacedMessageRef,
    RunResultEvidenceRef,
    SourceMessageEvidence,
    SourceRevisionEvidence,
    StructuredEvidence,
    WorkspaceEffectEvidenceRef,
    evidence_ref_key,
)
from backend.app.desktop.context_evolution import (
    ContextRevisionIdentityMismatch,
    ContextRevisionReader,
    ContextRevisionRepository,
)
from backend.app.desktop.context_evolution.repository import ContextRevisionNotFound


class _CorpusModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceCorpusReadError(RuntimeError):
    def __init__(self, code: str, summary: str) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary


class CorpusEvidenceItem(_CorpusModel):
    ref: EvidenceRef
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: Any
    semantic_roles: tuple[EvidenceRole, ...] = ()
    semantic_unit_ids: tuple[str, ...] = ()
    source_context_role: str | None = None

    @field_validator("semantic_roles", "semantic_unit_ids", mode="before")
    @classmethod
    def order_values(cls, values: Any) -> tuple[Any, ...]:
        return tuple(sorted(set(values or ())))


class FrozenEvidenceCorpus(_CorpusModel):
    corpus_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: MultiSourceEvidence
    items: tuple[CorpusEvidenceItem, ...]

    @model_validator(mode="after")
    def require_exact_inventory(self) -> FrozenEvidenceCorpus:
        available = {
            evidence_ref_key(message.ref)
            for source in self.evidence.sources
            for message in source.messages
        } | {evidence_ref_key(item.ref) for item in self.evidence.structured}
        item_keys = [evidence_ref_key(item.ref) for item in self.items]
        if len(item_keys) != len(set(item_keys)):
            raise ValueError("frozen evidence corpus 包含重复 evidence identity")
        if set(item_keys) != available:
            raise ValueError("frozen evidence corpus inventory 与 evidence payload 不一致")
        expected = stable_expansion_hash(
            "frozen-evidence-corpus",
            tuple(source.model_dump(mode="json") for source in self.evidence.sources),
            tuple(item.model_dump(mode="json") for item in self.items),
        )
        if self.corpus_id != expected:
            raise ValueError("frozen evidence corpus identity 不一致")
        return self

    @classmethod
    def create(
        cls,
        *,
        evidence: MultiSourceEvidence,
        items: tuple[CorpusEvidenceItem, ...],
    ) -> FrozenEvidenceCorpus:
        ordered = tuple(sorted(items, key=lambda item: evidence_ref_key(item.ref)))
        identity = stable_expansion_hash(
            "frozen-evidence-corpus",
            tuple(source.model_dump(mode="json") for source in evidence.sources),
            tuple(item.model_dump(mode="json") for item in ordered),
        )
        return cls(corpus_id=identity, evidence=evidence, items=ordered)

    def item(self, ref: EvidenceRef) -> CorpusEvidenceItem:
        key = evidence_ref_key(ref)
        for item in self.items:
            if evidence_ref_key(item.ref) == key:
                return item
        raise KeyError(key)


class FrozenEvidenceAuthority:
    def validate_source(
        self,
        observation: LoopObservationEnvelope,
        source,
        manifest: ContextSemanticManifest | None,
        stored_content_hash: str,
    ) -> ContextSemanticManifest:
        frontier = FrozenEvidenceCorpusReader._frontier(observation)
        frozen = frontier.get(source.revision_id)
        if frozen is None or frozen["identity"] != FrozenEvidenceCorpusReader._source_identity(source):
            raise EvidenceCorpusReadError("stale_source", "冻结 Context Revision 已不在 observation frontier")
        scope = set((observation.grant or {}).get("context_scope") or ())
        if scope and source.context_id not in scope:
            raise EvidenceCorpusReadError("source_out_of_scope", "冻结 Context Revision 超出 delegation scope")
        if manifest is None or manifest.source_content_hash != stored_content_hash:
            raise EvidenceCorpusReadError("stale_source", "semantic manifest hash 与冻结 Revision 不一致")
        if frozen["content_hash"] != stored_content_hash:
            raise EvidenceCorpusReadError("stale_source", "observation content hash 与冻结 Revision 不一致")
        return manifest


class FrozenEvidenceCorpusReader:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], checkpointer: Any) -> None:
        self._sessions = sessions
        self._repository = ContextRevisionRepository()
        self._reader = ContextRevisionReader(self._repository, checkpointer)
        self._authority = FrozenEvidenceAuthority()

    async def read(
        self,
        observation: LoopObservationEnvelope,
        opportunity: ExpansionOpportunity,
        manifests: tuple[ContextSemanticManifest, ...],
    ) -> FrozenEvidenceCorpus:
        manifest_by_revision = {manifest.source.revision_id: manifest for manifest in manifests}
        sources: list[SourceRevisionEvidence] = []
        context_roles: dict[str, str] = {}
        try:
            async with self._sessions() as session:
                for source in opportunity.manifest_sources:
                    revision = await self._repository.get(session, source)
                    manifest = manifest_by_revision.get(source.revision_id)
                    manifest = self._authority.validate_source(
                        observation,
                        source,
                        manifest,
                        revision.content_hash,
                    )
                    view = await self._reader.read(session, source, "execution")
                    messages = tuple(self._message(source, message) for message in view.messages)
                    sources.append(
                        SourceRevisionEvidence(
                            source=source,
                            projection_hash=revision.projection_hash or stable_expansion_hash("projection", messages),
                            content_hash=revision.content_hash,
                            messages=messages,
                        )
                    )
                    context_roles[source.revision_id] = manifest.role
        except ContextRevisionNotFound as exc:
            raise EvidenceCorpusReadError("source_unreadable", "冻结 Context Revision 或 checkpoint 无法读取") from exc
        except ContextRevisionIdentityMismatch as exc:
            raise EvidenceCorpusReadError("stale_source", "冻结 Context Revision identity 与存储不一致") from exc
        structured = self._structured(observation)
        evidence = MultiSourceEvidence(
            sources=tuple(sources),
            structured=structured,
            evidence_frontier=tuple(
                [message.ref for source in sources for message in source.messages]
                + [item.ref for item in structured]
            ),
        )
        items = self._items(evidence, manifests, context_roles)
        return FrozenEvidenceCorpus.create(evidence=evidence, items=items)

    @staticmethod
    def _frontier(observation: LoopObservationEnvelope) -> dict[str, dict[str, Any]]:
        result = {}
        for item in observation.portfolio_frontier:
            revision = item.get("revision") or {}
            revision_id = str(item.get("revision_id") or revision.get("revision_id") or "")
            if revision_id:
                result[revision_id] = {
                    "identity": (
                        str(item.get("context_id") or revision.get("context_id") or ""),
                        revision_id,
                        str(item.get("checkpoint_id") or revision.get("checkpoint_id") or ""),
                    ),
                    "content_hash": str(item.get("content_hash") or ""),
                }
        return result

    @staticmethod
    def _source_identity(source) -> tuple[str, str, str]:
        return source.context_id, source.revision_id, source.checkpoint_id or ""

    @staticmethod
    def _message(source, message: dict[str, Any]) -> SourceMessageEvidence:
        return SourceMessageEvidence(
            ref=NamespacedMessageRef(source=source, message_id=str(message["id"])),
            role=str(message["role"]),
            content=message.get("content", ""),
            tool_calls=tuple(message.get("tool_calls") or ()),
            tool_call_id=message.get("tool_call_id"),
            name=message.get("name"),
            status=message.get("status"),
        )

    @staticmethod
    def _structured(observation: LoopObservationEnvelope) -> tuple[StructuredEvidence, ...]:
        mission = observation.mission or observation.goal or {}
        mission_items = tuple(
            StructuredEvidence(
                ref=MissionEvidenceRef(
                    loop_id=observation.loop_id,
                    goal_revision=observation.goal_revision,
                    item_id=str(item.get("check_id") or item.get("criterion_id")),
                    content_hash=stable_expansion_hash("mission-evidence", item),
                ),
                content=item,
            )
            for item in tuple(mission.get("completion_checks") or ())
            if item.get("check_id") or item.get("criterion_id")
        )
        run_items = tuple(
            StructuredEvidence(
                ref=RunResultEvidenceRef(
                    run_id=str(item.get("run_id")),
                    context_id=str(item.get("context_id")),
                    result_id=str(item.get("result_id") or item.get("run_id")),
                    content_hash=stable_expansion_hash("run-result-evidence", item),
                ),
                content=item,
            )
            for item in observation.stable_results
            if item.get("run_id") and item.get("context_id")
        )
        workspace_items = tuple(
            StructuredEvidence(
                ref=WorkspaceEffectEvidenceRef(
                    workspace_id=str(observation.workspace.get("slot_id") or "workspace"),
                    revision=int(observation.workspace.get("revision") or 1),
                    effect_id=str(item.get("effect_id")),
                    content_hash=stable_expansion_hash("workspace-effect-evidence", item),
                ),
                content=item,
            )
            for item in tuple(observation.workspace.get("effects") or ())
            if item.get("effect_id")
        )
        material_payloads = tuple(observation.workspace.get("materials") or ()) + tuple(
            material
            for run in observation.stable_results
            for material in tuple(run.get("materials") or ())
        )
        material_scope = set((observation.grant or {}).get("material_scope") or ())
        if material_scope:
            outside = {
                str(item.get("material_id"))
                for item in material_payloads
                if item.get("material_id") and str(item.get("material_id")) not in material_scope
            }
            if outside:
                raise EvidenceCorpusReadError("source_out_of_scope", "冻结 material evidence 超出 delegation material scope")
        material_items = tuple(
            StructuredEvidence(
                ref=MaterialEvidenceRef(
                    material_id=str(item.get("material_id")),
                    version_id=str(item.get("version_id") or item.get("digest")),
                    content_hash=str(item.get("content_hash") or item.get("digest")),
                ),
                content=item.get("content") if "content" in item else item,
            )
            for item in material_payloads
            if item.get("material_id")
            and (item.get("version_id") or item.get("digest"))
            and len(str(item.get("content_hash") or item.get("digest") or "")) == 64
        )
        return (*mission_items, *run_items, *workspace_items, *material_items)

    @staticmethod
    def _items(
        evidence: MultiSourceEvidence,
        manifests: tuple[ContextSemanticManifest, ...],
        context_roles: dict[str, str],
    ) -> tuple[CorpusEvidenceItem, ...]:
        unit_map: dict[tuple[str, ...], tuple[set[EvidenceRole], set[str]]] = {}
        kind_roles: dict[str, tuple[EvidenceRole, ...]] = {
            "decision": ("decision",),
            "claim": ("conversation",),
            "hypothesis": ("conversation",),
            "unresolved_question": ("conversation",),
            "implementation_effect": ("implementation", "workspace_effect"),
            "verification_result": ("test",),
            "failure": ("failure",),
        }
        for manifest in manifests:
            for unit in manifest.units:
                for ref in unit.evidence_refs:
                    roles, unit_ids = unit_map.setdefault(evidence_ref_key(ref), (set(), set()))
                    roles.update(kind_roles[unit.kind])
                    unit_ids.add(unit.unit_id)
        items: list[CorpusEvidenceItem] = []
        role_aliases: dict[str, EvidenceRole] = {
            "implementation": "implementation",
            "testing": "test",
            "test": "test",
            "verification": "test",
            "requirements": "requirement",
            "requirement": "requirement",
            "failure": "failure",
            "decision": "decision",
        }
        for source in evidence.sources:
            for message in source.messages:
                roles, unit_ids = unit_map.get(evidence_ref_key(message.ref), (set(), set()))
                context_role = context_roles.get(source.source.revision_id)
                contextual_role = role_aliases.get((context_role or "").casefold())
                if contextual_role is not None:
                    roles.add(contextual_role)
                items.append(
                    CorpusEvidenceItem(
                        ref=message.ref,
                        content_hash=source.content_hash,
                        content=message.model_dump(mode="json"),
                        semantic_roles=tuple(roles or {"conversation"}),
                        semantic_unit_ids=tuple(unit_ids),
                        source_context_role=context_role,
                    )
                )
        for item in evidence.structured:
            role: EvidenceRole = "requirement"
            if isinstance(item.ref, RunResultEvidenceRef):
                status = str(item.content.get("status") if isinstance(item.content, dict) else "").casefold()
                role = "failure" if status in {"error", "failed", "failure"} else "test"
            elif isinstance(item.ref, WorkspaceEffectEvidenceRef):
                role = "workspace_effect"
            elif isinstance(item.ref, MaterialEvidenceRef):
                role = "material"
            items.append(
                CorpusEvidenceItem(
                    ref=item.ref,
                    content_hash=item.ref.content_hash,
                    content=item.content,
                    semantic_roles=(role,),
                )
            )
        return tuple(items)
