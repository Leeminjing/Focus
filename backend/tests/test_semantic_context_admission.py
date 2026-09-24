r"""本文件对外提供 cognitive planning coordinator 与 deterministic admission policy 的纯测试。

输入为冻结 observation、结构化 worker work specs、多来源 manifests、预算与 Workspace authority；输出为多 opportunity、no-op、
duplicate 或安全 blocker assessment。具体工作流为调用 coordinator/policy，不连接数据库，不依赖 marker 词表。
示例：`pytest backend/tests/test_semantic_context_admission.py -q`。
"""

from __future__ import annotations

import asyncio

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    EvidenceRequirement,
    ExpansionOpportunity,
    WorkContextSpec,
)
from backend.app.desktop.agent_loop.context_expansion.coordinator import (
    ContextExpansionCoordinator,
)
from backend.app.desktop.agent_loop.context_expansion.policy import (
    ExpansionAdmissionPolicy,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_manifest import (
    SemanticManifestProjector,
)
from backend.tests.test_semantic_context_planning import _draft, _observation


def _authorized_observation(*, worker_results=()):
    base = _observation(worker_results=worker_results)
    source = base.portfolio_frontier[0]["context_id"]
    return base.model_copy(
        update={
            "grant": {
                "capabilities": ("spawn_context", "create_lane", "adopt_workspace_result"),
                "permission_scope": ("read",),
                "context_scope": (source,),
            },
            "budget": {
                "limits": {
                    "max_input_tokens": 1000,
                    "max_contexts": 16,
                    "max_lanes": 8,
                    "max_new_lanes_per_round": 3,
                    "max_concurrent_runs": 4,
                },
                "usage": {"input_tokens": 950, "contexts": 1, "lanes": 1, "rounds": 2},
            },
        }
    )


def _curator_result(base, work_specs: list[dict]) -> dict:
    manifests = SemanticManifestProjector().project(base)
    return {
        "work_specs": work_specs,
        "retrieved_manifests": tuple(
            item.model_dump(mode="json") for item in manifests
        ),
    }


def _spec(*, workspace="read_only", objective="Investigate the failure.") -> WorkContextSpec:
    return WorkContextSpec.create(
        planner_version="planner-v1",
        objective=objective,
        separation_reason="The evidence needs an independent falsifiable analysis.",
        questions=("Which assumption conflicts with the observed result?",),
        completion_criteria=("One reproducible cause is supported by exact evidence.",),
        workspace_requirement=workspace,
        evidence_requirements=(
            EvidenceRequirement(
                requirement_id="failure",
                role="failure",
                question="What failed?",
                coverage_criterion="The exact failed run is available.",
            ),
        ),
    )


def _opportunity(observation, spec) -> ExpansionOpportunity:
    manifests = SemanticManifestProjector().project(observation)
    return ExpansionOpportunity.create(
        loop_id=observation.loop_id,
        round_id=observation.round_id,
        observation_hash="a" * 64,
        work_spec=spec,
        manifest_sources=tuple(manifest.source for manifest in manifests),
        manifest_ids=tuple(manifest.manifest_id for manifest in manifests),
    )


def test_policy_accepts_semantic_work_and_rejects_duplicate_identity() -> None:
    observation = _authorized_observation()
    opportunity = _opportunity(observation, _spec())
    policy = ExpansionAdmissionPolicy()

    accepted = policy.evaluate(observation, (opportunity,))
    duplicate = policy.evaluate(
        observation,
        (opportunity,),
        existing_independence_keys=frozenset({opportunity.independence_key}),
    )

    assert accepted.level == "recommended"
    assert accepted.opportunities == (opportunity,)
    assert duplicate.level == "not_applicable"
    assert duplicate.blockers[0].code == "duplicate_expansion"


def test_policy_rejects_isolated_write_without_write_permission() -> None:
    observation = _authorized_observation()
    opportunity = _opportunity(observation, _spec(workspace="isolated_write"))

    assessment = ExpansionAdmissionPolicy().evaluate(observation, (opportunity,))

    assert assessment.level == "not_applicable"
    assert assessment.blockers[0].code == "workspace_conflict"


def test_coordinator_returns_multiple_model_planned_opportunities() -> None:
    base = _authorized_observation()
    manifests = SemanticManifestProjector().project(base)
    known = (manifests[0].units[0].unit_id,)
    second = _draft(known) | {"objective": "Independently verify the fix.", "required": False}
    observation = _authorized_observation(
        worker_results=(
            {
                "kind": "lane_curator",
                "status": "success",
                "result": _curator_result(base, [_draft(known), second]),
            },
        )
    )

    assessment = asyncio.run(ContextExpansionCoordinator().assess(observation))

    assert assessment.level == "required"
    assert len(assessment.opportunities) == 2
    assert len({item.work_spec.work_spec_id for item in assessment.opportunities}) == 2


def test_token_pressure_and_user_paraphrase_can_still_produce_no_op() -> None:
    base = _authorized_observation()
    observation = _authorized_observation(
        worker_results=(
            {
                "kind": "lane_curator",
                "status": "success",
                "result": _curator_result(base, []),
            },
        )
    )
    observation = observation.model_copy(
        update={
            "user_intents": (
                {
                    "intent_id": "paraphrase",
                    "scope": "portfolio",
                    "content": "Could another line of reasoning inspect this independently?",
                },
            )
        }
    )

    assessment = asyncio.run(ContextExpansionCoordinator().assess(observation))

    assert assessment.level == "not_applicable"
    assert assessment.opportunities == ()


def test_user_requested_parallel_work_is_admitted_only_from_semantic_plan() -> None:
    base = _authorized_observation()
    known = (SemanticManifestProjector().project(base)[0].units[0].unit_id,)
    observation = _authorized_observation(
        worker_results=(
            {
                "kind": "lane_curator",
                "status": "success",
                "result": _curator_result(
                    base,
                    [
                        _draft(known)
                        | {
                            "objective": "Audit the frozen implementation evidence independently.",
                            "separation_reason": "The user requested an independent line of inquiry with its own completion boundary.",
                        }
                    ],
                ),
            },
        )
    ).model_copy(
        update={
            "user_intents": (
                {
                    "intent_id": "user-independent-audit",
                    "scope": "portfolio",
                    "content": "Could another line of reasoning inspect this independently?",
                },
            )
        }
    )

    assessment = asyncio.run(ContextExpansionCoordinator().assess(observation))

    assert assessment.level == "required"
    assert len(assessment.opportunities) == 1
    assert assessment.opportunities[0].work_spec.objective == (
        "Audit the frozen implementation evidence independently."
    )


def test_repeated_failure_can_be_planned_without_canonical_marker_words() -> None:
    base = _authorized_observation()
    known = (SemanticManifestProjector().project(base)[0].units[-1].unit_id,)
    draft = _draft(known) | {
        "objective": "Establish a falsifiable causal account.",
        "separation_reason": "The runtime contradiction needs its own evidence boundary.",
    }
    observation = _authorized_observation(
        worker_results=(
            {
                "kind": "lane_curator",
                "status": "success",
                "result": _curator_result(base, [draft]),
            },
        )
    )

    assessment = asyncio.run(ContextExpansionCoordinator().assess(observation))

    assert assessment.level == "required"
    assert assessment.opportunities[0].work_spec.objective == "Establish a falsifiable causal account."
