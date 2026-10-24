"""预设授权、追加边界及旧输入幂等文档的纯合同回归。"""

import pytest
from pydantic import TypeAdapter, ValidationError

from backend.app.desktop.patrol.agent_loop.resources.budget_presets import PRESETS, initial_budgets, increased_budgets
from backend.app.desktop.patrol.agent_loop.inputs.patrol_inputs import PatrolInputRequest, request_document
from backend.app.desktop.patrol.agent_loop.schemas import LoopBudgetContract, LoopGrantMutationRequest


def test_workspace_default_is_bounded_without_changing_legacy_loop_defaults():
    budget = initial_budgets()
    assert budget == initial_budgets("normal")
    assert budget["max_concurrent_runs"] == 2
    for key in ("max_rounds", "max_duration_seconds", "max_model_calls", "max_input_tokens", "max_output_tokens"):
        assert 0 < budget[key] < initial_budgets("long")[key]
    assert LoopBudgetContract().max_duration_seconds is None
    assert LoopBudgetContract().max_concurrent_runs == 32


def test_increase_uses_consumption_and_preserves_unlimited_and_other_fields():
    current = initial_budgets()
    current.update(max_duration_seconds=1800, max_input_tokens=None, max_rounds=990,
                   max_concurrent_runs=8, max_contexts=12)
    revised = increased_budgets(current, {"duration_seconds": 9000, "model_calls": 150}, "normal").as_grant_budgets(current)
    assert revised["max_duration_seconds"] == 10800
    assert revised["max_model_calls"] == 250
    assert revised["max_rounds"] == 1000
    assert revised["max_input_tokens"] is None
    assert revised["max_contexts"] == 12
    assert revised["max_concurrent_runs"] == 8
    assert revised["expansion_resources_source"] == current["expansion_resources_source"]


def test_omitted_preset_keeps_old_request_document_and_explicit_preset_is_frozen():
    request = PatrolInputRequest(submission_id="stable", content="任务")
    assert "budget_preset" not in request_document(request)
    assert request_document(request.model_copy(update={"budget_preset": "long"}))["budget_preset"] == "long"
    with pytest.raises(ValidationError):
        PatrolInputRequest(submission_id="stable", content="任务", budget_preset="unlimited")


def test_increase_command_requires_current_authority_and_known_preset():
    adapter = TypeAdapter(LoopGrantMutationRequest)
    assert adapter.validate_python({"command": "increase_budget", "preset": "normal", "expected_authority_revision": 2}).preset == "normal"
    with pytest.raises(ValidationError):
        adapter.validate_python({"command": "increase_budget", "preset": "normal"})


def test_public_catalog_and_offline_ui_fixture_match():
    import json
    import subprocess
    from pathlib import Path
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.desktop.patrol.agent_loop.routes import agent_loop_router

    app = FastAPI()
    app.include_router(agent_loop_router)
    response = TestClient(app).get("/desktop/api/agent-loops/budget-presets")
    assert response.status_code == 200
    assert response.json() == {"default": "normal", "presets": PRESETS}
    fixture = Path(__file__).parents[2] / "desktop" / "tests/fixtures/workspace-patrol-budget-fixture.cjs"
    completed = subprocess.run(["node", "-e", "process.stdout.write(JSON.stringify(require(process.argv[1])))", str(fixture)], capture_output=True, encoding="utf-8", check=True)
    assert json.loads(completed.stdout) == response.json()


@pytest.mark.parametrize("preset", PRESETS)
def test_presets_obey_existing_hard_guard(preset):
    from backend.app.desktop.patrol.agent_loop.resources.budgets import LoopBudgetGuard

    budget = initial_budgets(preset)
    guard = LoopBudgetGuard()
    for usage, limit in guard.HARD_FIELDS.items():
        if budget.get(limit) is not None:
            assert guard.evaluate({usage: budget[limit]}, budget, "start").status == "exhausted"
