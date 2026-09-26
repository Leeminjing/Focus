r"""本文件对外提供完整 semantic Context derivation 的核心合同与纯领域回归测试。

输入为 150 条冻结历史、生产三消息、Tool Exchange、真实 model usage、retrieval budgets、受监督角色重试、关系变体、R3/R8/R5/F2
bundle 与 claim/quality fixtures；输出为完整覆盖、旧证据可达、版本 provenance、scope/budget fail-closed、受限 verifier 输入、
降级 catalog 的 Curator 连续性、顺序无关 reconciliation、attempt audit、claim graph identity 和 quality-gated compilation 断言。
具体工作流为仅调用公开领域接口，不依赖模型或权威提交；数据库恢复另由 persistence integration 覆盖。示例：
`pytest backend/tests/test_complete_semantic_context_derivation.py -q`。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from focus.runtime.runs.usage import ModelUsage

from backend.app.desktop.agent_loop.context_expansion.compiler import (
    DeterministicExpansionPlanCompiler,
)
from backend.app.desktop.agent_loop.context_expansion.contracts import (
    EvidenceRequirement,
    ExpansionOpportunity,
    ResolvedEvidenceBundle,
    SpawnContextIntent,
    WorkContextSpec,
)
from backend.app.desktop.agent_loop.context_expansion.portfolio_index import (
    PortfolioSemanticIndexService,
)
from backend.app.desktop.agent_loop.context_expansion.quality import (
    ContextQualityAssessment,
    ContextQualityVerifier,
    QualityDimensionVerdict,
)
from backend.app.desktop.agent_loop.context_expansion.quality_verifier import (
    DeterministicTestContextQualityService,
)
from backend.app.desktop.agent_loop.context_expansion.reconciliation import (
    WorkSpecReconciler,
    WorkSpecRelation,
)
from backend.app.desktop.agent_loop.context_expansion.retrieval_planner import (
    LaneAdviceProposal,
    PlannerQueryProposal,
    PlannerReadProposal,
    RetrievalBackedCognitiveAdvisor,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_indexer import (
    RevisionSemanticIndexer,
    SegmentSemanticUnitDraft,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_index import RevisionSemanticIndex
from backend.app.desktop.agent_loop.context_expansion.semantic_grounding import (
    SemanticClaimSupportAssessment,
    SemanticSupportSpan,
    SupervisedSemanticClaimSupportVerifier,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import (
    AuthorizedSemanticRetriever,
    PlanningRetrievalSession,
    PlanningRetrievalSessionController,
    PortfolioIndexCatalog,
    RetrievalBudget,
    SemanticRetrievalQuery,
)
from backend.app.desktop.agent_loop.context_expansion.synthesis import (
    ClaimSupportAssessment,
    ContextSynthesisClaim,
    ContextSynthesisDraft,
    ContextSynthesisValidator,
    SynthesisSection,
)
from backend.app.desktop.agent_loop.context_expansion.synthesis_service import (
    DeterministicTestContextSynthesisService,
)
from backend.app.desktop.agent_loop.derivation_worker import (
    RoleBoundStructuredModel,
    StructuredResultValidationError,
)
from backend.app.desktop.context_curation import evidence_ref_key
from backend.app.desktop.context_evolution import (
    ContextRevisionPayloadMode,
    ContextRevisionRef,
)
from backend.tests._semantic_context_fixtures import failure_analysis_fixture


def _source(name: str = "long") -> ContextRevisionRef:
    return ContextRevisionRef(
        context_id=f"context-{name}",
        revision_id=f"revision-{name}",
        generation=1,
        execution_thread_id=f"thread-{name}",
        checkpoint_id=f"checkpoint-{name}",
        payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
    )


def _long_index():
    messages = tuple(
        {
            "id": f"msg-{index}",
            "role": "human" if index % 2 == 0 else "ai",
            "content": "historical requirement R3 requires durable lock" if index == 30 else f"message {index}",
        }
        for index in range(150)
    )
    return RevisionSemanticIndexer().index(
        source=_source(),
        source_content_hash="a" * 64,
        context_role="requirements",
        active_objective="Investigate lock failure",
        raw_messages=messages,
    )


def test_complete_index_exposes_message_30_outside_recent_preview() -> None:
    index = _long_index()
    assert len(index.coverage.message_ids) == 150
    assert index.coverage.message_ids[30] == "msg-30"
    catalog = PortfolioIndexCatalog.create(frontier_hash="f" * 64, indexes=(index,))
    session = PlanningRetrievalSession.create(
        observation_hash="o" * 64,
        frontier_hash="f" * 64,
        catalog=catalog,
        planner_version="planner-v1",
        retrieval_version=AuthorizedSemanticRetriever.VERSION,
        budget=RetrievalBudget(
            max_queries=2,
            max_candidates=16,
            max_exact_reads=4,
            max_model_calls=3,
            max_tokens=1000,
        ),
    )
    query = SemanticRetrievalQuery.create(text="durable lock requirement", kinds=("semantic_unit",), limit=8)
    retriever = AuthorizedSemanticRetriever()
    candidates = retriever.retrieve(session, query, (index,))
    session = PlanningRetrievalSessionController().record_query(session, query, candidates)
    candidate = next(
        item
        for item in candidates
        if any(
            ref.message_id == "msg-30"
            for unit in index.semantic_units
            if unit.unit_id == item.entry_id
            for ref in unit.evidence_refs
        )
    )
    read = retriever.read(session, candidate, (index,))
    assert read.evidence_refs[0].message_id <= "msg-30" <= read.evidence_refs[-1].message_id
    assert (
        candidate.index_schema_version,
        candidate.segmenter_version,
        candidate.projector_version,
    ) == (
        index.index_schema_version,
        index.segmenter_version,
        index.projector_version,
    )
    assert (
        read.index_schema_version,
        read.segmenter_version,
        read.projector_version,
    ) == (
        index.index_schema_version,
        index.segmenter_version,
        index.projector_version,
    )


def test_segmenter_keeps_multi_tool_exchange_in_one_segment() -> None:
    messages = (
        {"id": "a", "role": "ai", "content": "", "tool_calls": ({"id": "c1", "name": "one"}, {"id": "c2", "name": "two"})},
        {"id": "t1", "role": "tool", "content": "one", "tool_call_id": "c1"},
        {"id": "t2", "role": "tool", "content": "two", "tool_call_id": "c2"},
        {"id": "h", "role": "human", "content": "continue"},
    )
    index = RevisionSemanticIndexer().index(
        source=_source("tools"),
        source_content_hash="b" * 64,
        context_role="implementation",
        active_objective="Inspect tools",
        raw_messages=messages,
    )
    containing = next(item for item in index.segments if "a" in item.message_ids)
    assert containing.message_ids == ("a", "t1", "t2", "h")


def test_index_contract_rejects_coverage_gap_and_broken_tool_boundary() -> None:
    index = _long_index()
    payload = index.model_dump(mode="python")
    payload["coverage"]["message_ids"] = payload["coverage"]["message_ids"][:-1]
    with pytest.raises(ValueError, match="coverage"):
        type(index).model_validate(payload)
    with pytest.raises(ValueError, match="Tool Result"):
        RevisionSemanticIndexer().index(
            source=_source("broken-tool"),
            source_content_hash="c" * 64,
            context_role="testing",
            active_objective="Verify",
            raw_messages=(
                {"id": "a", "role": "ai", "tool_calls": ({"id": "call", "name": "pytest"},)},
            ),
        )


def test_indexer_handles_empty_multimodal_and_rejects_duplicate_or_unsupported_units() -> None:
    empty = RevisionSemanticIndexer().index(
        source=_source("empty"),
        source_content_hash="d" * 64,
        context_role="context",
        active_objective="No messages yet",
        raw_messages=(),
    )
    assert empty.messages == empty.segments == ()
    multimodal = RevisionSemanticIndexer().index(
        source=_source("multimodal"),
        source_content_hash="e" * 64,
        context_role="requirements",
        active_objective="Read image evidence",
        raw_messages=({"id": "m", "role": "human", "content": [{"type": "image", "url": "frozen://image"}]},),
    )
    assert multimodal.coverage.message_ids == ("m",)
    with pytest.raises(ValueError, match="重复"):
        RevisionSemanticIndexer().index(
            source=_source("duplicate"),
            source_content_hash="f" * 64,
            context_role="context",
            active_objective="Reject duplicate",
            raw_messages=({"id": "same", "role": "human"}, {"id": "same", "role": "ai"}),
        )


def test_index_identity_changes_with_content_or_projector_version() -> None:
    messages = ({"id": "m", "role": "human", "content": "frozen"},)
    first = RevisionSemanticIndexer().index(source=_source("versioned"), source_content_hash="2" * 64, context_role="context", active_objective="Version", raw_messages=messages)
    replay = RevisionSemanticIndexer().index(source=_source("versioned"), source_content_hash="2" * 64, context_role="context", active_objective="Version", raw_messages=messages)

    class NewProjector(RevisionSemanticIndexer):
        PROJECTOR_VERSION = "supervised-segment-projector-v3"

    changed_content = RevisionSemanticIndexer().index(source=_source("versioned"), source_content_hash="3" * 64, context_role="context", active_objective="Version", raw_messages=messages)
    changed_version = NewProjector().index(source=_source("versioned"), source_content_hash="2" * 64, context_role="context", active_objective="Version", raw_messages=messages)
    assert first.index_id == replay.index_id
    assert len({first.index_id, changed_content.index_id, changed_version.index_id}) == 3
    unsupported = SegmentSemanticUnitDraft(
        kind="claim",
        authority="confirmed",
        statement="invented text",
        supports=(SemanticSupportSpan(message_id="m", quote="fabricated quote"),),
    )
    degraded = RevisionSemanticIndexer().index(
        source=_source("unsupported"),
        source_content_hash="1" * 64,
        context_role="context",
        active_objective="Reject unsupported",
        raw_messages=({"id": "m", "role": "human", "content": "actual text"},),
        unit_drafts=(unsupported,),
    )
    assert degraded.quality_state == "degraded"
    assert degraded.rejected_units[0].code == "semantic_support_error"
    assert degraded.coverage.message_ids == ("m",)


def test_production_intro_paraphrase_remains_grounded_by_frozen_messages() -> None:
    messages = (
        {"id": "human-1", "role": "human", "content": "你好"},
        {"id": "human-2", "role": "human", "content": "你好"},
        {
            "id": "assistant-1",
            "role": "assistant",
            "content": "我是 Focus 的主 Agent，一个编排你的通用工程助手，当前工作在你的真实宿主机工作区。",
        },
    )

    draft = SegmentSemanticUnitDraft(
        kind="claim",
        authority="confirmed",
        statement="Focus 主 Agent 是在用户真实宿主机工作区运行的通用工程编排助手。",
        supports=(
            SemanticSupportSpan(
                message_id="assistant-1",
                quote="我是 Focus 的主 Agent，一个编排你的通用工程助手，当前工作在你的真实宿主机工作区。",
            ),
        ),
    )
    index = RevisionSemanticIndexer().index(
        source=_source("production-intro"),
        source_content_hash="9" * 64,
        context_role="primary",
        active_objective="Build the requested Obsidian plugin",
        raw_messages=messages,
        unit_drafts=(draft,),
        claim_support_assessments=(
            SemanticClaimSupportAssessment(
                claim_key=draft.claim_key,
                verdict="supported",
                reason="The exact frozen introduction directly supports the paraphrase.",
            ),
        ),
    )

    assert any(unit.authority == "confirmed" for unit in index.semantic_units)
    assert index.coverage.message_ids == ("human-1", "human-2", "assistant-1")


def test_multi_message_claim_requires_all_exact_supports_and_supported_verdict() -> None:
    draft = SegmentSemanticUnitDraft(
        kind="claim",
        authority="confirmed",
        statement="The requirement and runtime observation disagree.",
        supports=(
            SemanticSupportSpan(message_id="requirement", quote="Lock after three failures"),
            SemanticSupportSpan(message_id="runtime", quote="remained unlocked"),
        ),
    )
    assessment = SemanticClaimSupportAssessment(
        claim_key=draft.claim_key,
        verdict="supported",
        reason="Both frozen excerpts are required for the comparison.",
    )

    index = RevisionSemanticIndexer().index(
        source=_source("multi-support"),
        source_content_hash="8" * 64,
        context_role="failure-analysis",
        active_objective="Compare requirement and runtime behavior",
        raw_messages=(
            {"id": "requirement", "role": "human", "content": "Lock after three failures"},
            {"id": "runtime", "role": "assistant", "content": "The account remained unlocked"},
        ),
        unit_drafts=(draft,),
        claim_support_assessments=(assessment,),
    )

    assert index.quality_state == "complete"
    confirmed = next(unit for unit in index.semantic_units if unit.authority == "confirmed")
    assert {ref.message_id for ref in confirmed.evidence_refs} == {"requirement", "runtime"}
    for remaining_support in draft.supports:
        reduced = draft.model_copy(update={"supports": (remaining_support,)})
        rejected = RevisionSemanticIndexer().index(
            source=_source(f"missing-{remaining_support.message_id}"),
            source_content_hash="3" * 64,
            context_role="failure-analysis",
            active_objective="Require both sides of the comparison",
            raw_messages=(
                {"id": "requirement", "role": "human", "content": "Lock after three failures"},
                {"id": "runtime", "role": "assistant", "content": "The account remained unlocked"},
            ),
            unit_drafts=(reduced,),
            claim_support_assessments=(
                SemanticClaimSupportAssessment(
                    claim_key=reduced.claim_key,
                    verdict="unsupported",
                    reason="One excerpt cannot establish the cross-message comparison.",
                ),
            ),
        )
        assert all(unit.authority != "confirmed" for unit in rejected.semantic_units)
        assert rejected.rejected_units[0].code == "claim_support_unsupported"


@pytest.mark.parametrize(
    ("verdict", "expected_code"),
    (("unsupported", "claim_support_unsupported"), ("unknown", "claim_support_unknown")),
)
def test_confirmed_claim_requires_supported_assessment(verdict: str, expected_code: str) -> None:
    draft = SegmentSemanticUnitDraft(
        kind="claim",
        authority="confirmed",
        statement="The source requires a stable interface.",
        supports=(SemanticSupportSpan(message_id="requirement", quote="stable interface"),),
    )
    assessment = SemanticClaimSupportAssessment(
        claim_key=draft.claim_key,
        verdict=verdict,
        reason=f"Verifier returned {verdict}.",
    )

    index = RevisionSemanticIndexer().index(
        source=_source(f"claim-{verdict}"),
        source_content_hash="6" * 64,
        context_role="requirements",
        active_objective="Validate claim support",
        raw_messages=({"id": "requirement", "role": "human", "content": "Preserve the stable interface."},),
        unit_drafts=(draft,),
        claim_support_assessments=(assessment,),
    )

    assert index.quality_state == "degraded"
    assert index.rejected_units[0].code == expected_code
    assert all(unit.authority != "confirmed" for unit in index.semantic_units)


def test_grounding_contract_round_trip_and_unknown_message_are_explicit() -> None:
    draft = SegmentSemanticUnitDraft.model_validate(
        {
            "kind": "claim",
            "authority": "confirmed",
            "statement": "A grounded paraphrase.",
            "supports": [{"message_id": "missing", "quote": "exact quote"}],
        }
    )
    assert SegmentSemanticUnitDraft.model_validate_json(draft.model_dump_json()) == draft
    assessment = SemanticClaimSupportAssessment(
        claim_key=draft.claim_key,
        verdict="supported",
        reason="The cited excerpt supports the claim.",
    )

    index = RevisionSemanticIndexer().index(
        source=_source("unknown-support"),
        source_content_hash="7" * 64,
        context_role="context",
        active_objective="Validate support identity",
        raw_messages=({"id": "known", "role": "human", "content": "exact quote"},),
        unit_drafts=(draft,),
        claim_support_assessments=(assessment,),
    )

    assert index.quality_state == "degraded"
    assert index.rejected_units[0].code == "unknown_message_ref"
    assert index.coverage.message_ids == ("known",)


def test_claim_support_verifier_receives_only_claim_and_verified_excerpts() -> None:
    captured = {}

    class Model:
        async def invoke(self, schema, system, payload):
            captured.update(payload)
            claim = payload["claims"][0]
            return schema.model_validate(
                {
                    "assessments": (
                        {
                            "claim_key": claim["claim_key"],
                            "verdict": "supported",
                            "reason": "The frozen excerpt directly supports the statement.",
                        },
                    )
                }
            )

    draft = SegmentSemanticUnitDraft(
        kind="claim",
        authority="confirmed",
        statement="Focus runs in the real host workspace.",
        supports=(
            SemanticSupportSpan(
                message_id="assistant-1",
                quote="当前工作在你的真实宿主机工作区",
            ),
        ),
    )

    assessments = asyncio.run(SupervisedSemanticClaimSupportVerifier(Model()).verify((draft,)))

    assert assessments[0].verdict == "supported"
    assert set(captured) == {"claims"}
    assert set(captured["claims"][0]) == {"claim_key", "statement", "supports"}
    assert captured["claims"][0]["supports"] == (
        {"message_id": "assistant-1", "quote": "当前工作在你的真实宿主机工作区"},
    )


def test_all_invalid_production_projection_still_supports_curator_planning() -> None:
    messages = (
        {"id": "human-1", "role": "human", "content": "你好"},
        {"id": "human-2", "role": "human", "content": "你好"},
        {
            "id": "assistant-1",
            "role": "assistant",
            "content": "我是 Focus 的主 Agent，一个编排你的通用工程助手，当前工作在你的真实宿主机工作区。",
        },
    )
    invalid = SegmentSemanticUnitDraft(
        kind="claim",
        authority="confirmed",
        statement="Focus is a desktop pet implementation.",
        supports=(SemanticSupportSpan(message_id="assistant-1", quote="不存在的原文"),),
    )
    index = RevisionSemanticIndexer().index(
        source=_source("production-degraded"),
        source_content_hash="4" * 64,
        context_role="primary",
        active_objective="Build the requested Obsidian plugin",
        raw_messages=messages,
        unit_drafts=(invalid,),
    )
    catalog = PortfolioIndexCatalog.create(frontier_hash="5" * 64, indexes=(index,))
    session = PlanningRetrievalSession.create(
        observation_hash="6" * 64,
        frontier_hash="5" * 64,
        catalog=catalog,
        planner_version=RetrievalBackedCognitiveAdvisor.VERSION,
        retrieval_version=AuthorizedSemanticRetriever.VERSION,
        budget=RetrievalBudget(
            max_queries=2,
            max_candidates=16,
            max_exact_reads=4,
            max_model_calls=3,
            max_tokens=1000,
        ),
    )

    class Model:
        async def invoke(self, schema, system, payload):
            if schema is PlannerQueryProposal:
                return schema.model_validate(
                    {
                        "rationale": "Find the surviving frozen source evidence.",
                        "queries": ({"text": "Focus 主 Agent", "limit": 8},),
                    }
                )
            if schema is PlannerReadProposal:
                candidate = next(
                    item for item in payload["candidates"] if item["entry_kind"] == "semantic_unit"
                )
                return schema(
                    rationale="Read one exact fallback candidate.",
                    candidate_ids=(candidate["candidate_id"],),
                )
            assert schema is LaneAdviceProposal
            unit_id = payload["allowed_candidate_unit_ids"][0]
            return schema.model_validate(
                {
                    "rationale": "The frozen primary context supports a bounded implementation lane.",
                    "work_specs": (
                        {
                            "objective": "Implement the Obsidian plugin from frozen requirements.",
                            "separation_reason": "The implementation can proceed from the preserved primary evidence.",
                            "questions": ("Which requirements remain authoritative?",),
                            "completion_criteria": ("The implementation follows the frozen requirements.",),
                            "workspace_requirement": "isolated_write",
                            "evidence_requirements": (
                                {
                                    "requirement_id": "primary",
                                    "role": "requirement",
                                    "question": "What did the primary Context establish?",
                                    "coverage_criterion": "The exact frozen primary evidence is available.",
                                    "candidate_unit_ids": (unit_id,),
                                },
                            ),
                        },
                    ),
                }
            )

    async def run():
        return await RetrievalBackedCognitiveAdvisor(Model()).plan(
            {
                "mission": {"outcome": "Build the requested Obsidian plugin"},
                "frontier_hash": "5" * 64,
                "scope": {"derivation_input": {"portfolio_index_catalog": catalog.model_dump(mode="json")}},
            },
            session,
            (index,),
        )

    result = asyncio.run(run())

    assert index.quality_state == "degraded"
    assert len(index.rejected_units) == 1
    assert index.coverage.message_ids == ("human-1", "human-2", "assistant-1")
    assert result.session.state == "planned", (result.blocker_code, result.blocker_summary)
    assert result.proposal is not None
    assert result.proposal.work_specs[0].objective.startswith("Implement the Obsidian plugin")


def test_segment_only_index_exposes_zero_projected_units_and_exact_fallback_coverage() -> None:
    index = RevisionSemanticIndexer().index(
        source=_source("fallback-only"),
        source_content_hash="7" * 64,
        context_role="primary",
        active_objective="Preserve the frozen task",
        raw_messages=(
            {"id": "first", "role": "human", "content": "Keep the original requirement."},
            {"id": "second", "role": "ai", "content": "I will keep it."},
        ),
    )

    assert index.quality_state == "degraded"
    assert index.projected_unit_ids == ()
    assert index.fallback_segment_ids == tuple(segment.segment_id for segment in index.segments)
    assert index.descriptor().projected_unit_count == 0
    assert index.descriptor().fallback_segment_count == len(index.segments)
    assert RevisionSemanticIndex.model_validate(index.model_dump(mode="json")) == index
    tampered = {**index.model_dump(mode="json"), "fallback_segment_ids": ["0" * 64]}
    with pytest.raises(ValueError, match="fallback segment inventory"):
        RevisionSemanticIndex.model_validate(tampered)


def test_portfolio_index_build_is_bounded_retried_and_atomically_published() -> None:
    class Transaction:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Sessions:
        @staticmethod
        def begin():
            return Transaction()

    class Artifacts:
        def __init__(self) -> None:
            self.published = []
            self.stages = []

        async def put_index(self, session, index, *, attempt_records=()):
            self.published.append(index.index_id)

        async def put_stage_artifact(self, session, **payload):
            self.stages.append(payload)

        async def stage_artifact(self, session, **payload):
            if not self.stages:
                return None
            return SimpleNamespace(payload=self.stages[-1]["payload"])

    class ProbeService(PortfolioSemanticIndexService):
        def __init__(
            self,
            *,
            permanently_fail: str | None = None,
            catalog_budget: int = 64000,
        ) -> None:
            self._sessions = Sessions()
            self._artifacts = Artifacts()
            self._concurrency = 2
            self._max_attempts = 2
            self._catalog_max_descriptor_chars = catalog_budget
            self._projection_attempts = {}
            self._active = 0
            self.max_active = 0
            self.attempts = {}
            self._permanently_fail = permanently_fail

        async def _record_attempt_usage(self, loop_id, attempts):
            return None

        async def _index_one(self, observation, frontier_item):
            name = frontier_item["revision"]["revision_id"]
            self.attempts[name] = self.attempts.get(name, 0) + 1
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            try:
                await asyncio.sleep(0.005)
                if name == self._permanently_fail or (name == "revision-retry" and self.attempts[name] == 1):
                    raise ValueError("transient projection failure")
                source = ContextRevisionRef.model_validate(frontier_item["revision"])
                drafts = ()
                if name == "revision-degraded":
                    drafts = (
                        SegmentSemanticUnitDraft(
                            kind="claim",
                            authority="confirmed",
                            statement="Invented optional projection.",
                            supports=(
                                SemanticSupportSpan(
                                    message_id=f"message-{name}",
                                    quote="fabricated quote",
                                ),
                            ),
                        ),
                    )
                return RevisionSemanticIndexer().index(
                    source=source,
                    source_content_hash=frontier_item["content_hash"],
                    context_role="context",
                    active_objective="Bounded build",
                    raw_messages=({"id": f"message-{name}", "role": "human", "content": name},),
                    unit_drafts=drafts,
                )
            finally:
                self._active -= 1

    def observation(names):
            return SimpleNamespace(
                loop_id="loop-index-build",
                round_id="round-index-build",
                portfolio_frontier=tuple(
                {
                    "revision": _source(name).model_dump(mode="json"),
                    "content_hash": f"{index + 1}" * 64,
                }
                for index, name in enumerate(names)
            ),
            observed_frontier_hash="9" * 64,
        )

    async def run():
        service = ProbeService()
        result = await service.build(observation(("one", "retry", "degraded", "four")))
        assert result.blocker_code is None
        assert len(result.indexes) == 4
        assert next(
            item for item in result.indexes if item.source.revision_id == "revision-degraded"
        ).quality_state == "degraded"
        assert service.max_active == 2
        assert service.attempts["revision-retry"] == 2
        assert len(service._artifacts.published) == 4
        replay = await service.build(observation(("one", "retry", "degraded", "four")))
        assert replay.stage_record == result.stage_record

        failed = ProbeService(permanently_fail="revision-fail")
        blocked = await failed.build(observation(("one", "fail", "three")))
        assert blocked.blocker_code == "portfolio_index_failed"
        assert failed.attempts["revision-fail"] == 2
        assert failed._artifacts.published == []

        overflow = ProbeService(catalog_budget=1)
        blocked = await overflow.build(observation(("one",)))
        assert blocked.blocker_code == "portfolio_catalog_overflow"
        assert overflow._artifacts.published == []

    asyncio.run(run())


def test_portfolio_index_cancellation_never_publishes_partial_ready_state() -> None:
    class Artifacts:
        def __init__(self) -> None:
            self.published = []

        async def put_index(self, session, index, *, attempt_records=()):
            self.published.append(index.index_id)

    class CancelService(PortfolioSemanticIndexService):
        def __init__(self) -> None:
            self._concurrency = 2
            self._max_attempts = 1
            self._artifacts = Artifacts()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def _index_one(self, observation, frontier_item):
            self.started.set()
            await self.release.wait()
            raise AssertionError("cancelled build must not resume")

    async def run():
        service = CancelService()
        observation = SimpleNamespace(
            portfolio_frontier=(
                {"revision": _source("cancel-a").model_dump(mode="json")},
                {"revision": _source("cancel-b").model_dump(mode="json")},
            ),
            observed_frontier_hash="8" * 64,
        )
        task = asyncio.create_task(service.build(observation))
        await service.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service._artifacts.published == []

    asyncio.run(run())


def test_retrieval_rejects_out_of_scope_index_and_budget_overrun() -> None:
    index = _long_index()
    catalog = PortfolioIndexCatalog.create(frontier_hash="f" * 64, indexes=(index,))
    session = PlanningRetrievalSession.create(
        observation_hash="o" * 64,
        frontier_hash="f" * 64,
        catalog=catalog,
        planner_version="planner-v1",
        retrieval_version=AuthorizedSemanticRetriever.VERSION,
        budget=RetrievalBudget(max_queries=1, max_candidates=1, max_exact_reads=0, max_model_calls=0, max_tokens=0),
    )
    retriever = AuthorizedSemanticRetriever()
    with pytest.raises(ValueError, match="scope"):
        retriever.retrieve(
            session,
            SemanticRetrievalQuery.create(text="x", index_ids=("b" * 64,)),
            (index,),
        )
    query = SemanticRetrievalQuery.create(text="durable", kinds=("semantic_unit",), limit=1)
    candidates = retriever.retrieve(session, query, (index,))
    session = PlanningRetrievalSessionController().record_query(session, query, candidates)
    with pytest.raises(ValueError, match="budget"):
        retriever.retrieve(session, query, (index,))

    with pytest.raises(ValueError):
        RetrievalBudget(max_queries=-1, max_candidates=1, max_exact_reads=1, max_model_calls=1, max_tokens=1)


def test_catalog_overflow_and_namespaced_duplicate_message_ids_fail_safely() -> None:
    first = RevisionSemanticIndexer().index(
        source=_source("namespace-a"),
        source_content_hash="4" * 64,
        context_role="requirements",
        active_objective="Find shared id",
        raw_messages=({"id": "shared", "role": "human", "content": "requirement source"},),
    )
    second = RevisionSemanticIndexer().index(
        source=_source("namespace-b"),
        source_content_hash="5" * 64,
        context_role="failure",
        active_objective="Find shared id",
        raw_messages=({"id": "shared", "role": "human", "content": "failure source"},),
    )
    with pytest.raises(ValueError, match="catalog"):
        PortfolioIndexCatalog.create(frontier_hash="f" * 64, indexes=(first, second), max_descriptor_chars=1)
    catalog = PortfolioIndexCatalog.create(frontier_hash="f" * 64, indexes=(first, second))
    session = PlanningRetrievalSession.create(
        observation_hash="o" * 64,
        frontier_hash="f" * 64,
        catalog=catalog,
        planner_version="planner-v1",
        retrieval_version=AuthorizedSemanticRetriever.VERSION,
        budget=RetrievalBudget(max_queries=2, max_candidates=8, max_exact_reads=4, max_model_calls=3, max_tokens=100),
    )
    query = SemanticRetrievalQuery.create(text="source", kinds=("semantic_unit",), limit=8)
    retriever = AuthorizedSemanticRetriever()
    first_run = retriever.retrieve(session, query, (first, second))
    second_run = retriever.retrieve(session, query, (second, first))
    assert tuple(item.candidate_id for item in first_run) == tuple(item.candidate_id for item in second_run)
    assert {item.source.context_id for item in first_run} == {first.source.context_id, second.source.context_id}
    assert len({(item.source.context_id, item.entry_id) for item in first_run}) == 2


def test_retrieval_backed_planner_only_cites_exactly_read_old_evidence() -> None:
    class FakeModel:
        async def invoke(self, schema, system, payload):
            if schema is PlannerQueryProposal:
                return schema.model_validate(
                    {
                        "rationale": "The mission requires the historical lock requirement.",
                        "queries": ({"text": "durable lock requirement", "kinds": ("semantic_unit",), "limit": 8},),
                    }
                )
            if schema is PlannerReadProposal:
                candidate = next(item for item in payload["candidates"] if "durable lock" in item["descriptor"])
                return schema(rationale="Read the matching historical unit.", candidate_ids=(candidate["candidate_id"],))
            assert schema is LaneAdviceProposal
            unit_id = payload["allowed_candidate_unit_ids"][0]
            return schema.model_validate(
                {
                    "rationale": "The requirement and failure analysis need an independent history.",
                    "work_specs": (
                        {
                            "objective": "Analyze the lock failure against R3.",
                            "separation_reason": "The causal investigation is independent.",
                            "questions": ("Why did the account remain unlocked?",),
                            "completion_criteria": ("The cause is supported by frozen evidence.",),
                            "workspace_requirement": "read_only",
                            "evidence_requirements": (
                                {
                                    "requirement_id": "r3",
                                    "role": "requirement",
                                    "question": "What does R3 require?",
                                    "coverage_criterion": "The exact historical requirement is available.",
                                    "candidate_unit_ids": (unit_id,),
                                },
                            ),
                        },
                    ),
                }
            )

    async def run():
        index = _long_index()
        catalog = PortfolioIndexCatalog.create(frontier_hash="f" * 64, indexes=(index,))
        session = PlanningRetrievalSession.create(
            observation_hash="o" * 64,
            frontier_hash="f" * 64,
            catalog=catalog,
            planner_version=RetrievalBackedCognitiveAdvisor.VERSION,
            retrieval_version=AuthorizedSemanticRetriever.VERSION,
            budget=RetrievalBudget(max_queries=2, max_candidates=16, max_exact_reads=4, max_model_calls=3, max_tokens=1000),
        )
        result = await RetrievalBackedCognitiveAdvisor(FakeModel()).plan(
            {
                "mission": {"outcome": "Investigate lock failure"},
                "frontier_hash": "f" * 64,
                "scope": {"derivation_input": {"portfolio_index_catalog": catalog.model_dump(mode="json")}},
            },
            session,
            (index,),
        )
        assert result.session.state == "planned"
        assert result.proposal is not None
        cited = result.proposal.work_specs[0].evidence_requirements[0].candidate_unit_ids
        assert cited == (result.reads[0].entry_id,)
        assert any(ref.message_id == "msg-30" for ref in result.reads[0].evidence_refs)

        class NoCallModel:
            async def invoke(self, schema, system, payload):
                raise AssertionError("planned session must be reused without another model call")

        resumed = await RetrievalBackedCognitiveAdvisor(NoCallModel()).plan(
            {"mission": {"outcome": "ignored"}},
            result.session,
            (index,),
        )
        assert resumed.proposal == result.proposal
        assert resumed.session == result.session

    asyncio.run(run())


def test_retrieval_planner_accounts_real_tokens_and_stops_before_followup_calls() -> None:
    class TokenModel:
        def __init__(self) -> None:
            self.calls = 0
            self.last_usage = ModelUsage()

        async def invoke(self, schema, system, payload):
            self.calls += 1
            self.last_usage = ModelUsage(model_calls=1, input_tokens=31, output_tokens=29)
            return PlannerQueryProposal.model_validate(
                {
                    "rationale": "Locate the historical lock requirement.",
                    "queries": (
                        {
                            "text": "durable lock requirement",
                            "kinds": ("semantic_unit",),
                            "limit": 8,
                        },
                    ),
                }
            )

    async def run():
        index = _long_index()
        catalog = PortfolioIndexCatalog.create(frontier_hash="f" * 64, indexes=(index,))
        session = PlanningRetrievalSession.create(
            observation_hash="o" * 64,
            frontier_hash="f" * 64,
            catalog=catalog,
            planner_version=RetrievalBackedCognitiveAdvisor.VERSION,
            retrieval_version=AuthorizedSemanticRetriever.VERSION,
            budget=RetrievalBudget(
                max_queries=2,
                max_candidates=16,
                max_exact_reads=4,
                max_model_calls=3,
                max_tokens=50,
            ),
        )
        checkpoints = []
        model = TokenModel()

        async def checkpoint(value):
            checkpoints.append(value)

        result = await RetrievalBackedCognitiveAdvisor(
            model,
            checkpoint=checkpoint,
        ).plan({"mission": {"outcome": "Investigate lock failure"}}, session, (index,))

        assert result.blocker_code == "retrieval_budget_exhausted"
        assert result.session.state == "blocked"
        assert result.session.usage.model_calls == 1
        assert result.session.usage.tokens == 60
        assert model.calls == 1
        assert checkpoints[-1].state == "blocked"

    asyncio.run(run())


def test_role_bound_worker_retries_with_per_attempt_usage_audit(monkeypatch) -> None:
    class Worker:
        created = 0
        payloads = []

        def __init__(self, app_config, model_name=None) -> None:
            type(self).created += 1
            self.ordinal = type(self).created
            self.usage = ModelUsage()

        async def invoke(self, schema, system, payload):
            type(self).payloads.append(payload)
            self.usage = ModelUsage(model_calls=1, input_tokens=10 * self.ordinal, output_tokens=2)
            if self.ordinal == 1:
                raise ValueError("first structured response was invalid")
            return schema.model_validate(
                {
                    "rationale": "Recovered with a valid structured result.",
                    "queries": ({"text": "lock", "limit": 1},),
                }
            )

    monkeypatch.setattr(
        "backend.app.desktop.agent_loop.derivation_worker.StructuredWorkerModel",
        Worker,
    )

    async def run():
        model = RoleBoundStructuredModel(
            SimpleNamespace(),
            "semantic_index_projector",
            max_attempts=2,
        )
        result = await model.invoke(PlannerQueryProposal, "authority", {"frozen": True})
        assert result.queries[0].text == "lock"
        assert tuple(item["outcome"] for item in model.last_attempt_records) == (
            "error",
            "success",
        )
        assert all(item["role"] == "semantic_index_projector" for item in model.last_attempt_records)
        assert "previous_attempt_failure" not in Worker.payloads[0]
        assert Worker.payloads[1]["previous_attempt_failure"]["category"] == "model_schema_error"
        assert "first structured response was invalid" in Worker.payloads[1]["previous_attempt_failure"]["message"]
        assert model.last_usage == ModelUsage(model_calls=2, input_tokens=30, output_tokens=4)

    asyncio.run(run())


def test_role_bound_worker_retries_result_validation_with_unit_feedback(monkeypatch) -> None:
    class Worker:
        payloads = []

        def __init__(self, app_config, model_name=None) -> None:
            self.usage = ModelUsage(model_calls=1, input_tokens=7, output_tokens=3)

        async def invoke(self, schema, system, payload):
            type(self).payloads.append(payload)
            return schema.model_validate(
                {
                    "rationale": "Return one bounded semantic query.",
                    "queries": ({"text": "frozen evidence", "limit": 1},),
                }
            )

    monkeypatch.setattr(
        "backend.app.desktop.agent_loop.derivation_worker.StructuredWorkerModel",
        Worker,
    )

    calls = 0

    def validate(result) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise StructuredResultValidationError(
                "semantic_support_error",
                "duplicate semantic identity",
                unit_identity="claim-1",
                violated_rule="each claim identity must appear exactly once",
            )

    async def run():
        model = RoleBoundStructuredModel(
            SimpleNamespace(),
            "semantic_index_projector",
            max_attempts=2,
        )
        result = await model.invoke_validated(
            PlannerQueryProposal,
            "authority",
            {"frozen": True},
            validate,
        )

        assert result.queries[0].text == "frozen evidence"
        assert tuple(item["outcome"] for item in model.last_attempt_records) == ("error", "success")
        assert model.last_attempt_records[0]["failure_category"] == "semantic_support_error"
        assert model.last_attempt_records[0]["validation_feedback"]["unit_identity"] == "claim-1"
        assert Worker.payloads[1]["previous_attempt_failure"] == {
            "category": "semantic_support_error",
            "error_type": "StructuredResultValidationError",
            "message": "duplicate semantic identity",
            "unit_identity": "claim-1",
            "violated_rule": "each claim identity must appear exactly once",
        }

    asyncio.run(run())


def test_reconciliation_is_order_independent_and_preserves_obligations() -> None:
    first = _spec("Analyze authentication failure root cause", "req", "What behavior is required?")
    second = _spec("Investigate login failure cause", "failure", "What failure occurred?")
    relation = WorkSpecRelation(
        left_candidate_id=min(first.work_spec_id, second.work_spec_id),
        right_candidate_id=max(first.work_spec_id, second.work_spec_id),
        verdict="equivalent",
        rationale="objective, responsibility, completion and evidence roles describe one investigation",
        compared_fields=("objective", "questions", "completion_criteria", "evidence_requirements"),
    )
    reconciler = WorkSpecReconciler()
    left = reconciler.reconcile((first, second), relations=(relation,), required_candidate_ids=(second.work_spec_id,))
    right = reconciler.reconcile((second, first), relations=(relation,), required_candidate_ids=(second.work_spec_id,))
    assert left.reconciliation_id == right.reconciliation_id
    assert len(left.canonical_specs) == 1
    assert {item.requirement_id for item in left.canonical_specs[0].evidence_requirements} == {"req", "failure"}
    assert left.required_work_spec_ids == (left.canonical_specs[0].work_spec_id,)


def test_reconciliation_owns_exact_duplicate_subsumption_distinct_and_conflict_semantics() -> None:
    first = _spec("Analyze authentication failure root cause", "req", "What behavior is required?")
    duplicate = first.model_copy(deep=True)
    second = _spec("Investigate login failure cause", "failure", "What failure occurred?")
    pair = tuple(sorted((first.work_spec_id, second.work_spec_id)))
    reconciler = WorkSpecReconciler()

    exact = reconciler.reconcile((first, duplicate), relations=())
    assert exact.input_candidate_ids == (first.work_spec_id,)
    assert exact.canonical_specs == (first,)

    subsumes = reconciler.reconcile(
        (first, second),
        relations=(
            WorkSpecRelation(
                left_candidate_id=pair[0],
                right_candidate_id=pair[1],
                verdict="subsumes",
                dominant_candidate_id=first.work_spec_id,
                rationale="One investigation contains the other and preserves both obligations.",
                compared_fields=("objective", "evidence_requirements"),
            ),
        ),
    )
    assert len(subsumes.canonical_specs) == 1
    assert {item.requirement_id for item in subsumes.canonical_specs[0].evidence_requirements} == {
        "req",
        "failure",
    }

    distinct = reconciler.reconcile((first, second), relations=())
    assert len(distinct.canonical_specs) == 2
    assert all(item.verdict == "distinct" for item in distinct.relations)

    conflict = reconciler.reconcile(
        (first, second),
        relations=(
            WorkSpecRelation(
                left_candidate_id=pair[0],
                right_candidate_id=pair[1],
                verdict="conflicting",
                rationale="The candidates require mutually exclusive safety boundaries.",
                compared_fields=("workspace_requirement",),
            ),
        ),
    )
    assert len(conflict.canonical_specs) == 2
    assert conflict.conflicts[0].candidate_ids == pair


def test_compilation_requires_matching_passing_quality_assessment() -> None:
    async def run():
        spec, evidence, items = failure_analysis_fixture()
        bundle = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence, items=items)
        opportunity = ExpansionOpportunity.create(
            loop_id="loop",
            round_id="round",
            observation_hash="a" * 64,
            work_spec=spec,
            manifest_sources=tuple(item.source for item in evidence.sources),
        )
        intent = SpawnContextIntent(opportunity_id=opportunity.opportunity_id)
        missing = DeterministicExpansionPlanCompiler().compile(opportunity, intent, bundle)
        assert missing.code == "compiler_failed"
        synthesis = await DeterministicTestContextSynthesisService().synthesize(None, spec, bundle)
        assert synthesis.dossier is not None
        quality = await DeterministicTestContextQualityService().verify(spec, bundle, synthesis.dossier)
        assert quality.assessment is not None and quality.assessment.passes
        compiled = DeterministicExpansionPlanCompiler().compile(
            opportunity,
            intent,
            bundle,
            dossier=synthesis.dossier,
            quality_assessment=quality.assessment,
        )
        assert compiled.quality_assessment_id == quality.assessment.assessment_id
        assert compiled.plan.items[2].content.startswith("Evidence-grounded Context Dossier")

    asyncio.run(run())


def test_claim_graph_and_quality_contracts_fail_closed() -> None:
    async def run():
        spec, evidence, items = failure_analysis_fixture()
        bundle = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence, items=items)
        synthesis = await DeterministicTestContextSynthesisService().synthesize(None, spec, bundle)
        dossier = synthesis.dossier
        assert dossier is not None
        first = dossier.claims[0]
        cyclic = first.model_copy(update={"authority": "inference", "citations": (), "premise_claim_ids": (first.claim_id,)})
        with pytest.raises(ValueError):
            ContextSynthesisDraft(
                sections=(SynthesisSection(title="Invalid", claim_ids=(first.claim_id,)),),
                claims=(cyclic,),
            )
        quality = await DeterministicTestContextQualityService().verify(spec, bundle, dossier)
        assert quality.assessment is not None
        with pytest.raises(ValueError, match="three|required dimensions|3"):
            ContextQualityAssessment.create(
                work_spec=spec,
                bundle=bundle,
                dossier=dossier,
                preflight=quality.preflight,
                verifier_version="test",
                policy_version="test",
                dimensions=(QualityDimensionVerdict(dimension="minimality", verdict="pass", reasons=("ok",)),),
            )
        failed = ContextQualityVerifier().verify(
            spec,
            bundle,
            dossier,
            {
                "dimensions": (
                    {"dimension": "minimality", "verdict": "pass", "reasons": ("all evidence has a purpose",)},
                    {"dimension": "sufficiency", "verdict": "unknown", "reasons": ("critical behavior remains uncertain",)},
                    {"dimension": "coherence", "verdict": "pass", "reasons": ("conflicts remain explicit",)},
                )
            },
        )
        assert failed.blocker_code == "context_quality_failed"
        assert failed.assessment is not None and not failed.assessment.passes

    asyncio.run(run())


def test_multisource_claim_synthesis_preserves_conflict_hypothesis_and_raw_auditability() -> None:
    spec, evidence, items = failure_analysis_fixture()
    bundle = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence, items=items)
    question_id = ContextSynthesisValidator.question_id(spec.questions[0])
    direct = tuple(
        ContextSynthesisClaim.create(
            statement={
                "R3": "R3 requires the account to lock after the third failed attempt.",
                "R8": "The implementation increments the counter after persistence.",
                "R5": "The test observed no lock after the third failed attempt.",
                "F2": "The runtime result reports that the account remained unlocked.",
            }[item.requirement_id],
            authority="confirmed",
            citations=(item.ref,),
            requirement_ids=(item.requirement_id,),
        )
        for item in bundle.items
    )
    synthesis = ContextSynthesisClaim.create(
        statement="The requirement and observed behavior conflict under the current implementation ordering.",
        authority="inference",
        premise_claim_ids=tuple(item.claim_id for item in direct),
        question_ids=(question_id,),
    )
    hypothesis = ContextSynthesisClaim.create(
        statement="Persistence ordering may delay enforcement of the lock threshold.",
        authority="hypothesis",
        premise_claim_ids=(synthesis.claim_id,),
    )
    claims = (*direct, synthesis, hypothesis)
    draft = ContextSynthesisDraft(
        sections=(
            SynthesisSection(
                title="Requirement, implementation, test, and failure",
                claim_ids=tuple(item.claim_id for item in claims),
            ),
        ),
        claims=claims,
    )
    assessments = tuple(
        ClaimSupportAssessment(
            claim_id=claim.claim_id,
            verdict="supported",
            citation_keys=tuple(evidence_ref_key(ref) for ref in claim.citations),
            reason="The cited frozen evidence directly supports this atomic claim.",
        )
        for claim in direct
    )
    dossier = ContextSynthesisValidator().validate(
        spec,
        bundle,
        draft,
        assessments,
        synthesizer_version="test-synthesizer-v1",
    )
    assert {identity for claim in dossier.claims for identity in claim.requirement_ids} == {
        "R3",
        "R8",
        "R5",
        "F2",
    }
    assert dossier.claims[-2].authority == "inference"
    assert dossier.claims[-1].authority == "hypothesis"
    assert {
        evidence_ref_key(ref)
        for claim in dossier.claims
        for ref in claim.citations
    } == {evidence_ref_key(item.ref) for item in bundle.items}
    assert bundle.evidence.sources[1].messages[0].tool_calls


def test_synthesis_rejects_omitted_question_invalid_premise_and_unsupported_confirmation() -> None:
    spec, evidence, items = failure_analysis_fixture()
    bundle = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence, items=items)
    direct = tuple(
        ContextSynthesisClaim.create(
            statement=f"Evidence resolves {item.requirement_id}.",
            authority="confirmed",
            citations=(item.ref,),
            requirement_ids=(item.requirement_id,),
        )
        for item in bundle.items
    )
    draft = ContextSynthesisDraft(
        sections=(SynthesisSection(title="Evidence", claim_ids=tuple(item.claim_id for item in direct)),),
        claims=direct,
    )
    supported = tuple(
        ClaimSupportAssessment(
            claim_id=claim.claim_id,
            verdict="supported",
            citation_keys=tuple(evidence_ref_key(ref) for ref in claim.citations),
            reason="Supported by the cited evidence.",
        )
        for claim in direct
    )
    validator = ContextSynthesisValidator()
    with pytest.raises(ValueError, match="questions"):
        validator.validate(spec, bundle, draft, supported, synthesizer_version="test-v1")

    invalid_premise = ContextSynthesisClaim.create(
        statement="The unknown claim proves the cause.",
        authority="inference",
        premise_claim_ids=("0" * 64,),
        question_ids=(validator.question_id(spec.questions[0]),),
    )
    invalid_draft = ContextSynthesisDraft(
        sections=(
            SynthesisSection(
                title="Invalid",
                claim_ids=(*tuple(item.claim_id for item in direct), invalid_premise.claim_id),
            ),
        ),
        claims=(*direct, invalid_premise),
    )
    with pytest.raises(ValueError, match="premise"):
        validator.validate(spec, bundle, invalid_draft, supported, synthesizer_version="test-v1")

    unsupported = supported[0].model_copy(update={"verdict": "unsupported"})
    question_covered = direct[0].model_copy(
        update={"question_ids": (validator.question_id(spec.questions[0]),)}
    )
    with pytest.raises(ValueError):
        ContextSynthesisDraft(
            sections=(SynthesisSection(title="Invalid identity", claim_ids=(question_covered.claim_id,)),),
            claims=(question_covered,),
        )
    with pytest.raises(ValueError, match="direct-support"):
        validator.validate(
            spec,
            bundle,
            ContextSynthesisDraft(
                sections=(SynthesisSection(title="Evidence", claim_ids=tuple(item.claim_id for item in direct)),),
                claims=direct,
                unresolved_questions=spec.questions,
            ),
            (unsupported, *supported[1:]),
            synthesizer_version="test-v1",
        )


def _spec(objective: str, requirement_id: str, question: str) -> WorkContextSpec:
    return WorkContextSpec.create(
        planner_version="planner-v1",
        objective=objective,
        separation_reason="Independent analysis history is required.",
        questions=(question,),
        completion_criteria=("The root cause is supported by frozen evidence.",),
        workspace_requirement="read_only",
        evidence_requirements=(
            EvidenceRequirement(
                requirement_id=requirement_id,
                role="requirement" if requirement_id == "req" else "failure",
                question=question,
                coverage_criterion="Exact evidence is available.",
            ),
        ),
    )
