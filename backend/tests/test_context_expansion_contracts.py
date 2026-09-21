r"""本文件对外提供 Context expansion 合同、detector、policy、coordinator 与状态机的纯测试。

输入为冻结 Observation fixture、稳定 Revision、Mission checks、失败/Token/授权预算和 Curator proposal；输出为
确定 identity、候选、required/recommended/not_applicable assessment、blocker、决策合同（identity 选择、required 出口、
封闭引用取值）与合法状态转换断言。具体工作流为
不连接数据库地穿过 ContextExpansionCoordinator 与 PatrolDecisionContract 的公开 interface。示例：`pytest backend/tests/test_context_expansion_contracts.py`。
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
from backend.app.desktop.agent_loop.patrol_contract import PatrolDecisionContract
from backend.app.desktop.agent_loop.round_orchestration import PatrolContractViolation, PatrolDecisionProposal
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
    assert SpawnContextIntent(opportunity_id=first.opportunity_id).action == "spawn_context"
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
        mission_references=({"role": "outcome", "reference_id": "outcome"},),
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
        PatrolDecisionContract().validate(
            actions=proposal.actions,
            mission_references=proposal.mission_references,
            observation=observation,
        )


def test_required_assessment_accepts_wait_for_user_as_exit() -> None:
    observation = _observation()
    assessment = asyncio.run(ContextExpansionCoordinator().assess(observation))
    observation = observation.model_copy(update={"expansion_assessment": assessment.model_dump(mode="json")})
    proposal = PatrolDecisionProposal(
        rationale="Derivation is required but cannot be prepared in this round.",
        mission_references=({"role": "outcome", "reference_id": "outcome"},),
        actions=({"action": "wait_for_user", "reason": "需要用户决定是否派生"},),
    )

    PatrolDecisionContract().validate(
        actions=proposal.actions,
        mission_references=proposal.mission_references,
        observation=observation,
    )


def test_patrol_spawn_selects_frozen_opportunity_by_identity() -> None:
    observation = _observation()
    assessment = asyncio.run(ContextExpansionCoordinator().assess(observation))
    opportunity = assessment.opportunities[0]
    observation = observation.model_copy(update={"expansion_assessment": assessment.model_dump(mode="json")})
    valid = PatrolDecisionProposal(
        rationale="Delegate independent verification.",
        mission_references=({"role": "outcome", "reference_id": "outcome"},),
        actions=({"action": "spawn_context", "opportunity_id": opportunity.opportunity_id},),
    )

    PatrolDecisionContract().validate(
        actions=valid.actions,
        mission_references=valid.mission_references,
        observation=observation,
    )

    unknown = PatrolDecisionProposal(
        rationale="Reference an opportunity outside the frozen assessment.",
        mission_references=({"role": "outcome", "reference_id": "outcome"},),
        actions=({"action": "spawn_context", "opportunity_id": "b" * 64},),
    )
    with pytest.raises(PatrolContractViolation, match="assessment 之外"):
        PatrolDecisionContract().validate(
            actions=unknown.actions,
            mission_references=unknown.mission_references,
            observation=observation,
        )


def test_spawn_context_rejects_semantic_fields() -> None:
    with pytest.raises(ValidationError):
        PatrolDecisionProposal(
            rationale="Try to restate the frozen semantics.",
            mission_references=({"role": "outcome", "reference_id": "outcome"},),
            actions=(
                {
                    "action": "spawn_context",
                    "opportunity_id": "a" * 64,
                    "purpose": "Rewrite the purpose.",
                    "work_order": "Rewrite the work order.",
                },
            ),
        )


def test_contract_holds_for_whitespace_normalized_numbered_work_order() -> None:
    opportunity = ExpansionOpportunity.create(
        loop_id="loop-expansion",
        round_id="round-expansion",
        source=_revision(),
        purpose="独立验证编号清单",
        work_order="1. 先跑聚焦测试。\n2.   再读失败日志。\n3. 汇总可复现证据。",
        completion_check="聚焦测试全绿",
        workspace_mode="read_only",
        independence_key="verification:numbered-list",
        triggers=("curator_proposal",),
        required=True,
    )
    assert "\n" not in opportunity.work_order
    observation = _observation().model_copy(
        update={
            "expansion_assessment": {
                "loop_id": "loop-expansion",
                "round_id": "round-expansion",
                "frontier_hash": "a" * 64,
                "policy_version": "context-expansion-v1",
                "level": "required",
                "opportunities": (opportunity.model_dump(mode="json"),),
                "blockers": (),
            }
        }
    )
    proposal = PatrolDecisionProposal(
        rationale="按 identity 选择已冻结的派生机会。",
        mission_references=({"role": "outcome", "reference_id": "outcome"},),
        actions=({"action": "spawn_context", "opportunity_id": opportunity.opportunity_id},),
    )

    PatrolDecisionContract().validate(
        actions=proposal.actions,
        mission_references=proposal.mission_references,
        observation=observation,
    )


def test_completion_reference_follows_current_mission_revision() -> None:
    observation = _observation()
    contract = PatrolDecisionContract()
    current = PatrolDecisionProposal(
        rationale="引用当前 revision 的完成检查。",
        mission_references=(
            {"role": "outcome", "reference_id": "outcome"},
            {"role": "completion_check", "reference_id": "tests"},
        ),
        actions=({"action": "wait_for_user", "reason": "需要用户输入"},),
    )
    contract.validate(actions=current.actions, mission_references=current.mission_references, observation=observation)

    revised = observation.model_copy(
        update={
            "mission": {
                **observation.mission,
                "completion_checks": (
                    {"check_id": "tests-v2", "claim": "Focused tests pass", "required": True, "expected_evidence_kinds": ("test",)},
                ),
            }
        }
    )
    with pytest.raises(PatrolContractViolation, match="合法集合为 tests-v2"):
        contract.validate(actions=current.actions, mission_references=current.mission_references, observation=revised)


def test_unknown_boundary_group_names_the_declared_groups() -> None:
    with pytest.raises(ValidationError, match="已声明分组之一"):
        PatrolDecisionProposal(
            rationale="引用未声明的 boundary 分组。",
            mission_references=({"role": "boundary", "reference_id": "boundary"},),
            actions=({"action": "wait_for_user", "reason": "需要用户输入"},),
        )


def test_decline_expansion_requires_policy_blocker() -> None:
    observation = _observation()
    assessment = asyncio.run(ContextExpansionCoordinator().assess(observation))
    opportunity = assessment.opportunities[0]
    observation = observation.model_copy(update={"expansion_assessment": assessment.model_dump(mode="json")})
    declined = PatrolDecisionProposal(
        rationale="The opportunity is blocked by policy.",
        mission_references=({"role": "outcome", "reference_id": "outcome"},),
        actions=(
            {
                "action": "decline_expansion",
                "opportunity_id": opportunity.opportunity_id,
                "blocker_code": "not_independent",
                "reason": "Policy did not register this blocker.",
            },
        ),
    )

    with pytest.raises(PatrolContractViolation, match="blocker"):
        PatrolDecisionContract().validate(
            actions=declined.actions,
            mission_references=declined.mission_references,
            observation=observation,
        )
