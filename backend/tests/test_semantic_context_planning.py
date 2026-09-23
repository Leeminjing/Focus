r"""本文件对外提供 signal、semantic manifest 与 supervised cognitive planner adapter 的测试。

输入为带精确 message preview 的冻结 Loop observation、合法/非法 worker 结构化结果与 marker 文本；输出为
纯事实 signals、带引用 manifests、稳定 work specs 或阶段 failure。具体工作流为调用三个独立公开阶段，断言任何
阈值、关键词或未知引用都不能直接生成派生工作。示例：`pytest backend/tests/test_semantic_context_planning.py -q`。
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from backend.app.desktop.agent_loop.context_expansion.manifest_adapter import (
    CompositeSemanticManifestProjector,
    WorkerResultManifestProjector,
)
from backend.app.desktop.agent_loop.context_expansion.planner import (
    WorkerResultCognitivePlanner,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_manifest import (
    SemanticManifestProjector,
)
from backend.app.desktop.agent_loop.context_expansion.signals import (
    ExpansionSignalCollector,
)
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.tests._semantic_context_fixtures import revision_ref


def _observation(*, worker_results: tuple[dict, ...] = ()) -> LoopObservationEnvelope:
    source = revision_ref("planning")
    return LoopObservationEnvelope(
        loop_id="loop-planning",
        loop_revision=1,
        round_id="round-planning",
        goal_revision=3,
        authority_revision=2,
        observed_frontier_hash="f" * 64,
        mission={
            "revision": 3,
            "outcome": "Fix the failure and verify the requirement.",
            "completion_checks": (
                {"check_id": "R3", "claim": "The requirement holds."},
                {"check_id": "R5", "claim": "The test passes."},
            ),
        },
        grant={"context_scope": (source.context_id,)},
        portfolio_frontier=(
            {
                "lane_id": "lane-planning",
                "context_id": source.context_id,
                "revision_id": source.revision_id,
                "role": "implementation",
                "revision": source.model_dump(mode="json"),
                "content_hash": "c" * 64,
                "message_evidence_preview": (
                    {
                        "message_id": "implementation-R8",
                        "role": "ai",
                        "content": "The counter is incremented after persistence.",
                    },
                    {
                        "message_id": "failure-F2",
                        "role": "tool",
                        "content": "Expected locked; observed unlocked.",
                        "status": "error",
                        "tool_call_id": "call-F2",
                    },
                ),
            },
        ),
        stable_results=(
            {
                "run_id": "run-F2",
                "context_id": source.context_id,
                "status": "error",
                "error": "Expected locked; observed unlocked.",
            },
        ),
        workspace={"revision": 7, "fingerprint": "workspace-7"},
        budget={
            "limits": {"max_input_tokens": 1000},
            "usage": {"input_tokens": 950, "no_progress_count": 2},
        },
        user_intents=(
            {
                "intent_id": "intent-1",
                "scope": "portfolio",
                "content": "Please fix this in parallel.",
            },
        ),
        worker_results=worker_results,
    )


def _draft(candidate_unit_ids: tuple[str, ...] = ()) -> dict:
    return {
        "objective": "Find the falsifiable root cause.",
        "separation_reason": "Failure analysis needs an independent evidence view.",
        "questions": ["Which implementation assumption conflicts with the failure?"],
        "completion_criteria": ["Tie one root-cause hypothesis to reproducible evidence."],
        "workspace_requirement": "read_only",
        "required": True,
        "evidence_requirements": [
            {
                "requirement_id": "failure",
                "role": "failure",
                "question": "What failed?",
                "coverage_criterion": "The exact stable failure is available.",
                "candidate_unit_ids": list(candidate_unit_ids),
            }
        ],
    }


def test_signals_report_facts_without_prescribing_context_semantics() -> None:
    signals = ExpansionSignalCollector().collect(_observation())

    assert {item.kind for item in signals.signals} == {
        "mission_structure",
        "failure_recurrence",
        "token_pressure",
        "user_authority",
        "budget_state",
        "workspace_state",
        "portfolio_shape",
    }
    serialized = signals.model_dump_json()
    assert "work_order" not in serialized
    assert "isolated_write" not in serialized
    assert "failure-analysis" not in serialized


def test_thresholds_and_marker_words_cannot_create_work_without_planner_output() -> None:
    observation = _observation()
    signals = ExpansionSignalCollector().collect(observation)
    result = asyncio.run(
        WorkerResultCognitivePlanner().plan(
            observation,
            signals,
            SemanticManifestProjector().project(observation),
        )
    )

    assert any(item.kind == "token_pressure" for item in signals.signals)
    assert any(item.kind == "user_authority" for item in signals.signals)
    assert result.work_specs == ()
    assert result.failure is not None


def test_manifest_preserves_exact_revision_and_failure_kind() -> None:
    manifests = SemanticManifestProjector().project(_observation())

    assert len(manifests) == 1
    assert manifests[0].source.revision_id == "revision-planning-1"
    assert {item.kind for item in manifests[0].units} == {"claim", "failure"}
    assert all(item.evidence_refs for item in manifests[0].units)


def test_manifest_preserves_explicit_semantic_kinds_without_text_inference() -> None:
    observation = _observation()
    frontier = dict(observation.portfolio_frontier[0])
    kinds = (
        "decision",
        "claim",
        "hypothesis",
        "unresolved_question",
        "implementation_effect",
        "verification_result",
        "failure",
    )
    frontier["message_evidence_preview"] = tuple(
        {
            "message_id": f"semantic-{kind}",
            "role": "ai",
            "content": f"Statement classified as {kind}.",
            "semantic_kind": kind,
        }
        for kind in kinds
    )
    changed = observation.model_copy(update={"portfolio_frontier": (frontier,)})

    manifest = SemanticManifestProjector().project(changed)[0]

    assert {unit.kind for unit in manifest.units} == set(kinds)


def test_manifest_rejects_unknown_kind_and_unsupported_message() -> None:
    observation = _observation()
    frontier = dict(observation.portfolio_frontier[0])
    frontier["message_evidence_preview"] = (
        {"message_id": "invented", "role": "ai", "content": "Unsupported.", "semantic_kind": "fact"},
    )
    changed = observation.model_copy(update={"portfolio_frontier": (frontier,)})

    with pytest.raises(ValueError, match="未知 semantic kind"):
        SemanticManifestProjector().project(changed)

    frontier["message_evidence_preview"] = ({"role": "ai", "content": "No evidence identity."},)
    changed = observation.model_copy(update={"portfolio_frontier": (frontier,)})
    with pytest.raises(ValueError, match="无 identity"):
        SemanticManifestProjector().project(changed)


def test_manifest_rejects_invalid_hash_and_out_of_scope_source() -> None:
    observation = _observation()
    frontier = dict(observation.portfolio_frontier[0])
    frontier["content_hash"] = "not-a-hash"
    invalid_hash = observation.model_copy(update={"portfolio_frontier": (frontier,)})

    with pytest.raises(ValidationError):
        SemanticManifestProjector().project(invalid_hash)

    out_of_scope = observation.model_copy(update={"grant": {"context_scope": ("different-context",)}})
    with pytest.raises(ValueError, match="context scope"):
        SemanticManifestProjector().project(out_of_scope)


def test_empty_manifest_is_a_valid_explicit_projection() -> None:
    observation = _observation()
    frontier = dict(observation.portfolio_frontier[0])
    frontier["message_evidence_preview"] = ()
    changed = observation.model_copy(update={"portfolio_frontier": (frontier,)})

    manifest = SemanticManifestProjector().project(changed)[0]

    assert manifest.units == ()


def test_manifest_model_adapter_accepts_valid_and_empty_structured_output() -> None:
    base = _observation()
    valid = base.model_copy(
        update={
            "worker_results": (
                {
                    "kind": "semantic_manifest_projector",
                    "status": "success",
                    "result": {
                        "units": [
                            {
                                "revision_id": "revision-planning-1",
                                "kind": "implementation_effect",
                                "authority": "confirmed",
                                "statement": "The counter is incremented after persistence.",
                                "message_ids": ["implementation-R8"],
                            }
                        ]
                    },
                },
            )
        }
    )
    accepted = WorkerResultManifestProjector().project(valid)
    empty = base.model_copy(
        update={
            "worker_results": (
                {"kind": "semantic_manifest_projector", "status": "success", "result": {"units": []}},
            )
        }
    )
    empty_result = WorkerResultManifestProjector().project(empty)

    assert accepted.failure is None
    assert accepted.manifests[0].units[0].kind == "implementation_effect"
    assert empty_result.failure is None
    assert empty_result.manifests[0].units == ()


def test_composite_manifest_adds_validated_curator_semantics_to_production_projection() -> None:
    observation = _observation(
        worker_results=(
            {
                "kind": "lane_curator",
                "status": "success",
                "result": {
                    "manifest_units": [
                        {
                            "revision_id": "revision-planning-1",
                            "kind": "implementation_effect",
                            "authority": "confirmed",
                            "statement": "The counter is incremented after persistence.",
                            "message_ids": ["implementation-R8"],
                        }
                    ],
                    "work_specs": [],
                },
            },
        )
    )

    manifest = CompositeSemanticManifestProjector().project(observation)[0]

    assert "implementation_effect" in {unit.kind for unit in manifest.units}
    assert all(unit.evidence_refs for unit in manifest.units)


def test_manifest_model_adapter_rejects_malformed_and_unsupported_output() -> None:
    base = _observation()
    malformed = base.model_copy(
        update={
            "worker_results": (
                {
                    "kind": "semantic_manifest_projector",
                    "status": "success",
                    "result": {"units": [{"revision_id": "revision-planning-1"}]},
                },
            )
        }
    )
    unsupported = base.model_copy(
        update={
            "worker_results": (
                {
                    "kind": "semantic_manifest_projector",
                    "status": "success",
                    "result": {
                        "units": [
                            {
                                "revision_id": "revision-planning-1",
                                "kind": "claim",
                                "authority": "confirmed",
                                "statement": "A statement absent from the cited source.",
                                "message_ids": ["implementation-R8"],
                            }
                        ]
                    },
                },
            )
        }
    )

    malformed_result = WorkerResultManifestProjector().project(malformed)
    unsupported_result = WorkerResultManifestProjector().project(unsupported)

    assert malformed_result.failure is not None
    assert malformed_result.failure.code == "manifest_contract_invalid"
    assert unsupported_result.failure is not None
    assert unsupported_result.failure.code == "manifest_contract_invalid"


def test_manifest_cache_is_revision_hash_scoped() -> None:
    projector = SemanticManifestProjector()
    observation = _observation()
    first = projector.project(observation)[0]
    changed_frontier = dict(observation.portfolio_frontier[0])
    changed_frontier["content_hash"] = "d" * 64
    changed_frontier["message_evidence_preview"] = (
        {"message_id": "new", "role": "human", "content": "New revision content."},
    )
    changed = observation.model_copy(update={"portfolio_frontier": (changed_frontier,)})
    second = projector.project(changed)[0]

    assert first.manifest_id != second.manifest_id
    assert first.units[0].statement != second.units[0].statement


def test_planner_freezes_valid_worker_draft() -> None:
    base = _observation()
    manifests = SemanticManifestProjector().project(base)
    known = (manifests[0].units[0].unit_id,)
    observation = _observation(
        worker_results=(
            {"kind": "lane_curator", "status": "success", "result": {"work_specs": [_draft(known)]}},
        )
    )
    result = asyncio.run(
        WorkerResultCognitivePlanner().plan(
            observation,
            ExpansionSignalCollector().collect(observation),
            manifests,
        )
    )

    assert result.failure is None
    assert len(result.work_specs) == 1
    assert result.required_work_spec_ids == (result.work_specs[0].work_spec_id,)


def test_planner_rejects_invented_semantic_unit() -> None:
    observation = _observation(
        worker_results=(
            {"kind": "lane_curator", "status": "success", "result": {"work_specs": [_draft(("invented",))]}},
        )
    )
    manifests = SemanticManifestProjector().project(observation)
    result = asyncio.run(
        WorkerResultCognitivePlanner().plan(
            observation,
            ExpansionSignalCollector().collect(observation),
            manifests,
        )
    )

    assert result.failure is not None
    assert result.failure.code == "planner_evidence_identity_unknown"


def test_planner_worker_failure_is_explicit_without_template_fallback() -> None:
    observation = _observation(
        worker_results=(
            {"kind": "lane_curator", "status": "error", "result": {"error": "model unavailable"}},
        )
    )
    result = asyncio.run(
        WorkerResultCognitivePlanner().plan(
            observation,
            ExpansionSignalCollector().collect(observation),
            SemanticManifestProjector().project(observation),
        )
    )

    assert result.failure is not None
    assert result.failure.code == "planner_worker_failed"
    assert result.work_specs == ()


def test_planner_timeout_and_malformed_schema_are_explicit() -> None:
    timed_out = _observation(
        worker_results=(
            {"kind": "lane_curator", "status": "failed", "result": {"error": "timeout"}},
        )
    )
    timeout_result = asyncio.run(
        WorkerResultCognitivePlanner().plan(
            timed_out,
            ExpansionSignalCollector().collect(timed_out),
            SemanticManifestProjector().project(timed_out),
        )
    )
    malformed = _observation(
        worker_results=(
            {"kind": "lane_curator", "status": "success", "result": {"work_specs": [{"objective": "missing fields"}]}},
        )
    )
    malformed_result = asyncio.run(
        WorkerResultCognitivePlanner().plan(
            malformed,
            ExpansionSignalCollector().collect(malformed),
            SemanticManifestProjector().project(malformed),
        )
    )

    assert timeout_result.failure is not None
    assert timeout_result.failure.code == "planner_worker_failed"
    assert malformed_result.failure is not None
    assert malformed_result.failure.code == "planner_contract_invalid"


def test_planner_empty_structured_output_is_a_valid_no_op() -> None:
    observation = _observation(
        worker_results=(
            {"kind": "lane_curator", "status": "success", "result": {"work_specs": []}},
        )
    )
    result = asyncio.run(
        WorkerResultCognitivePlanner().plan(
            observation,
            ExpansionSignalCollector().collect(observation),
            SemanticManifestProjector().project(observation),
        )
    )

    assert result.failure is None
    assert result.work_specs == ()


def test_planner_missing_result_is_explicit() -> None:
    observation = _observation()
    result = asyncio.run(
        WorkerResultCognitivePlanner().plan(
            observation,
            ExpansionSignalCollector().collect(observation),
            SemanticManifestProjector().project(observation),
        )
    )

    assert result.failure is not None
    assert result.failure.code == "planner_result_missing"
