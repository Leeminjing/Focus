r"""本文件对外提供 Context expansion 合同、detector、policy、coordinator 与状态机的纯测试。

输入为冻结 Observation fixture、稳定 Revision、Mission checks、失败/Token/授权预算和 Curator proposal；输出为
确定 identity、候选、required/recommended/not_applicable assessment、blocker 与合法状态转换断言。具体工作流为
不连接数据库地穿过 ContextExpansionCoordinator 的公开 interface。示例：`pytest backend/tests/test_context_expansion_contracts.py`。
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from backend.app.desktop.agent_loop import context_expansion
from backend.app.desktop.agent_loop.context_expansion import ContextExpansionCoordinator, ExpansionOpportunity
from backend.app.desktop.agent_loop.context_expansion.contracts import CuratorExpansionProposal, SpawnContextIntent
from backend.app.desktop.agent_loop.context_expansion.lifecycle import ExpansionLifecycleStateMachine, ExpansionTransitionRejected
from backend.app.desktop.agent_loop.context_expansion.policy import ExpansionAdmissionPolicy
from backend.app.desktop.agent_loop.round_orchestration import MissionReference, PatrolContractViolation, PatrolDecisionProposal, StructuredPatrolDecisionModel
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.context_evolution import ContextRevisionPayloadMode, ContextRevisionRef


def _revision(context_id: str = "context-primary") -> ContextRevisionRef:
    return ContextRevisionRef(
        context_id=context_id,
        revision_id=f"revision-{context_id}",
        generation=1,
        execution_thread_id=f"thread-{context_id}",
        checkpoint_ns="",
        checkpoint_id=f"checkpoint-{context_id}",
        payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
    )


def _observation(**updates) -> LoopObservationEnvelope:
    source = _revision()
    payload = {
        "loop_id": "loop-expansion",
        "loop_revision": 1,
        "round_id": "round-expansion",
        "goal_revision": 1,
        "authority_revision": 1,
        "observed_frontier_hash": "a" * 64,
        "mission": {
            "outcome": "Implement and independently verify the change",
            "boundaries": {"in_scope": [], "required_invariants": [], "prohibited_actions": []},
            "completion_checks": [
                {"check_id": "implementation", "claim": "Implementation is complete", "required": True, "expected_evidence_kinds": ["artifact"]},
                {"check_id": "tests", "claim": "Focused tests pass", "required": True, "expected_evidence_kinds": ["test"]},
            ],
        },
        "grant": {
            "capabilities": ["continue_context", "create_lane"],
            "context_scope": [source.context_id],
            "permission_scope": ["read"],
        },
        "portfolio_frontier": (
            {
                "lane_id": "lane-primary",
                "context_id": source.context_id,
                "revision_id": source.revision_id,
                "revision": source.model_dump(mode="json"),
                "role": "primary",
            },
        ),
        "workspace": {"revision": 1},
        "budget": {
            "limits": {"max_contexts": 16, "max_lanes": 8, "max_new_lanes_per_round": 3, "max_concurrent_runs": 4, "max_input_tokens": 1000},
            "usage": {"contexts": 1, "lanes": 1, "rounds": 3, "input_tokens": 100, "no_progress_count": 0},
        },
    }
    payload.update(updates)
    return LoopObservationEnvelope.model_validate(payload)


def test_opportunity_identity_is_stable_and_semantic_contract_is_strict() -> None:
    source = _revision()
    values = dict(
        loop_id="loop-expansion",
        round_id="round-expansion",
        source=source,
        purpose="Independent verification",
        work_order="Run focused tests.",
        completion_check="Focused tests pass.",
        workspace_mode="read_only",
        independence_key="completion-check:tests",
        triggers=("independent_verification",),
        required=True,
    )

    first = ExpansionOpportunity.create(**values)
    second = ExpansionOpportunity.create(**values)
    restored = ExpansionOpportunity.model_validate_json(first.model_dump_json())

    assert first.opportunity_id == second.opportunity_id
    assert first.semantic_fingerprint == second.semantic_fingerprint
    assert restored == first
    assert SpawnContextIntent(
        opportunity_id=first.opportunity_id,
        source_context_id=source.context_id,
        purpose=first.purpose,
        work_order=first.work_order,
        completion_check=first.completion_check,
        workspace_mode=first.workspace_mode,
    ).action == "spawn_context"
    with pytest.raises(ValidationError):
        SpawnContextIntent.model_validate({**first.model_dump(), "action": "spawn_context", "unknown": True})
    with pytest.raises(ValidationError):
        ExpansionOpportunity.create(
            **{
                **values,
                "source": source.model_copy(update={"checkpoint_id": None}),
            }
        )


def test_context_expansion_package_exposes_only_stable_interface() -> None:
    assert set(context_expansion.__all__) == {
        "CompiledExpansion",
        "ContextExpansionCoordinator",
        "DeclineExpansionIntent",
        "ExpansionAssessment",
        "ExpansionBlocker",
        "ExpansionOpportunity",
        "ExpansionOutcome",
        "SpawnContextIntent",
    }


def test_detector_and_policy_require_independent_verification() -> None:
    assessment = asyncio.run(ContextExpansionCoordinator().assess(_observation()))

    assert assessment.level == "required"
    assert assessment.requires_decision is True
    assert assessment.decision_deadline_round == 5
    assert {item.independence_key for item in assessment.opportunities} == {
        "completion-check:implementation",
        "completion-check:tests",
    }


def test_policy_reports_budget_duplicate_and_workspace_blockers() -> None:
    observation = _observation()
    opportunities = asyncio.run(ContextExpansionCoordinator().assess(observation)).opportunities
    budget_blocked = observation.model_copy(
        update={
            "budget": {
                **observation.budget,
                "usage": {**observation.budget["usage"], "contexts": 16},
            }
        }
    )

    assessment = ExpansionAdmissionPolicy().evaluate(
        budget_blocked,
        opportunities,
        existing_independence_keys=frozenset({"completion-check:tests"}),
    )

    assert assessment.level == "not_applicable"
    assert {item.code for item in assessment.blockers} == {"context_budget_exhausted"}
    duplicate = ExpansionAdmissionPolicy().evaluate(
        observation,
        (opportunities[-1],),
        existing_independence_keys=frozenset({"completion-check:tests"}),
    )
    assert duplicate.blockers[0].code == "duplicate_expansion"
    isolated = opportunities[-1].model_copy(update={"workspace_mode": "isolated_write"})
    workspace = ExpansionAdmissionPolicy().evaluate(observation, (isolated,))
    assert workspace.blockers[0].code == "workspace_conflict"


@pytest.mark.parametrize(
    ("case", "expected"),
    (
        ("authority", "authority_missing"),
        ("unreadable", "source_unreadable"),
        ("stale", "stale_source"),
        ("scope", "source_out_of_scope"),
        ("context_budget", "context_budget_exhausted"),
        ("lane_budget", "lane_budget_exhausted"),
        ("round_budget", "round_lane_budget_exhausted"),
        ("concurrency", "concurrency_budget_exhausted"),
        ("duplicate", "duplicate_expansion"),
        ("workspace_permission", "workspace_conflict"),
        ("workspace_capability", "workspace_isolation_unavailable"),
        ("workspace_runtime", "workspace_isolation_unavailable"),
    ),
)
def test_policy_emits_each_stable_admission_blocker(case: str, expected: str) -> None:
    observation = _observation()
    opportunity = asyncio.run(ContextExpansionCoordinator().assess(observation)).opportunities[-1]
    existing = frozenset()
    grant = dict(observation.grant)
    budget = {"limits": dict(observation.budget["limits"]), "usage": dict(observation.budget["usage"])}
    workspace = dict(observation.workspace)
    stable_results = observation.stable_results
    if case == "authority":
        grant["capabilities"] = ["continue_context"]
    elif case == "unreadable":
        opportunity = ExpansionOpportunity.create(
            loop_id=opportunity.loop_id,
            round_id=opportunity.round_id,
            source=_revision("context-missing"),
            purpose=opportunity.purpose,
            work_order=opportunity.work_order,
            completion_check=opportunity.completion_check,
            workspace_mode=opportunity.workspace_mode,
            independence_key=opportunity.independence_key,
            triggers=opportunity.triggers,
        )
    elif case == "stale":
        opportunity = ExpansionOpportunity.create(
            loop_id=opportunity.loop_id,
            round_id=opportunity.round_id,
            source=opportunity.source.model_copy(update={"revision_id": "revision-stale", "checkpoint_id": "checkpoint-stale"}),
            purpose=opportunity.purpose,
            work_order=opportunity.work_order,
            completion_check=opportunity.completion_check,
            workspace_mode=opportunity.workspace_mode,
            independence_key=opportunity.independence_key,
            triggers=opportunity.triggers,
        )
    elif case == "scope":
        grant["context_scope"] = ["context-other"]
    elif case == "context_budget":
        budget["usage"]["contexts"] = budget["limits"]["max_contexts"]
    elif case == "lane_budget":
        budget["usage"]["lanes"] = budget["limits"]["max_lanes"]
    elif case == "round_budget":
        budget["limits"]["max_new_lanes_per_round"] = 0
    elif case == "concurrency":
        stable_results = tuple({"run_id": f"run-{index}", "status": "running"} for index in range(4))
    elif case == "duplicate":
        existing = frozenset({opportunity.independence_key})
    elif case.startswith("workspace_"):
        opportunity = opportunity.model_copy(update={"workspace_mode": "isolated_write"})
        if case == "workspace_permission":
            grant["permission_scope"] = ["read"]
        else:
            grant["permission_scope"] = ["read", "write"]
            if case == "workspace_capability":
                grant["capabilities"] = ["continue_context", "create_lane"]
            else:
                grant["capabilities"] = ["continue_context", "create_lane", "adopt_workspace_result"]
                workspace["isolation_available"] = False
    candidate_observation = observation.model_copy(
        update={"grant": grant, "budget": budget, "workspace": workspace, "stable_results": stable_results}
    )

    assessment = ExpansionAdmissionPolicy().evaluate(
        candidate_observation,
        (opportunity,),
        existing_independence_keys=existing,
    )

    assert assessment.level == "not_applicable"
    assert assessment.blockers[0].code == expected


def test_policy_emits_recommended_and_workspace_detector_signal() -> None:
    observation = _observation(
        mission={"outcome": "Finish", "completion_checks": []},
        workspace={"revision": 1, "parallel_write_required": True},
        user_intents=(
            {
                "intent_id": "intent-write",
                "scope": "portfolio",
                "content": "请并行实现另一个修复",
            },
        ),
    )
    observation = observation.model_copy(
        update={
            "grant": {
                **observation.grant,
                "capabilities": ["continue_context", "create_lane", "adopt_workspace_result"],
                "permission_scope": ["read", "write"],
            }
        }
    )
    assessment = asyncio.run(ContextExpansionCoordinator().assess(observation))
    recommended = assessment.opportunities[0].model_copy(update={"required": False})
    reevaluated = ExpansionAdmissionPolicy().evaluate(observation, (recommended,))

    assert assessment.opportunities[0].workspace_mode == "isolated_write"
    assert "workspace_state" in assessment.opportunities[0].triggers
    assert reevaluated.level == "recommended"


def test_detector_handles_repeated_failure_token_pressure_and_no_split() -> None:
    base = _observation(mission={"outcome": "Finish", "completion_checks": []})
    none = asyncio.run(ContextExpansionCoordinator().assess(base))
    assert none.level == "not_applicable"
    assert none.blockers[0].code == "not_independent"
    pressured = base.model_copy(
        update={
            "stable_results": ({"run_id": "run-failed", "status": "failed"},),
            "budget": {
                **base.budget,
                "usage": {**base.budget["usage"], "input_tokens": 950, "no_progress_count": 2},
            },
        }
    )
    assessment = asyncio.run(ContextExpansionCoordinator().assess(pressured))
    assert assessment.level == "required"
    assert {trigger for item in assessment.opportunities for trigger in item.triggers} == {"repeated_failure", "token_pressure"}


def test_curator_proposal_is_bound_to_frozen_source_without_authority() -> None:
    class Curator:
        async def propose(self, observation, opportunities):
            return (
                CuratorExpansionProposal(
                    source_context_id="context-primary",
                    purpose="Architecture review",
                    work_order="Review the proposed structure independently.",
                    completion_check="Report concrete architecture risks.",
                    independence_key="architecture-review",
                ),
            )

    assessment = asyncio.run(ContextExpansionCoordinator(curator=Curator()).assess(_observation()))
    proposal = next(item for item in assessment.opportunities if item.independence_key == "architecture-review")

    assert proposal.source.revision_id == "revision-context-primary"
    assert proposal.triggers == ("curator_proposal",)


def test_expansion_lifecycle_is_forward_only_and_terminal() -> None:
    machine = ExpansionLifecycleStateMachine()

    assert machine.validate("detected", "curated") == "curated"
    assert machine.validate("curated", "proposed") == "proposed"
    assert machine.validate("proposed", "compiled") == "compiled"
    assert machine.validate("compiled", "authorized") == "authorized"
    assert machine.validate("authorized", "committed") == "committed"
    assert machine.validate("committed", "dispatched") == "dispatched"
    assert machine.validate("blocked", "blocked") == "blocked"
    with pytest.raises(ExpansionTransitionRejected):
        machine.validate("blocked", "proposed")


def test_required_assessment_cannot_be_bypassed_by_continue_context() -> None:
    observation = _observation()
    assessment = asyncio.run(ContextExpansionCoordinator().assess(observation))
    observation = observation.model_copy(update={"expansion_assessment": assessment.model_dump(mode="json")})
    proposal = PatrolDecisionProposal(
        rationale="Continue without acknowledging expansion.",
        mission_references=(MissionReference(role="outcome", reference_id="outcome"),),
        actions=(
            {
                "action": "continue_context",
                "context_id": "context-primary",
                "context_revision_id": "revision-context-primary",
                "message": "Continue.",
            },
        ),
    )

    with pytest.raises(PatrolContractViolation, match="required Context expansion"):
        StructuredPatrolDecisionModel._validate_expansion_decision(proposal, observation)


def test_patrol_spawn_must_reference_frozen_opportunity_verbatim() -> None:
    observation = _observation()
    assessment = asyncio.run(ContextExpansionCoordinator().assess(observation))
    opportunity = assessment.opportunities[0]
    observation = observation.model_copy(update={"expansion_assessment": assessment.model_dump(mode="json")})
    valid = PatrolDecisionProposal(
        rationale="Delegate independent verification.",
        mission_references=(MissionReference(role="outcome", reference_id="outcome"),),
        actions=(
            {
                "action": "spawn_context",
                "opportunity_id": opportunity.opportunity_id,
                "source_context_id": opportunity.source.context_id,
                "purpose": opportunity.purpose,
                "work_order": opportunity.work_order,
                "completion_check": opportunity.completion_check,
                "workspace_mode": opportunity.workspace_mode,
            },
        ),
    )

    StructuredPatrolDecisionModel._validate_expansion_decision(valid, observation)
    forged = valid.model_copy(
        update={
            "actions": (
                valid.actions[0].model_copy(update={"work_order": "Invent a different task."}),
            )
        }
    )
    with pytest.raises(PatrolContractViolation, match="原样引用"):
        StructuredPatrolDecisionModel._validate_expansion_decision(forged, observation)
