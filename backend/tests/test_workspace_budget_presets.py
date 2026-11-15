"""本文件对外提供固定无上限预算、历史读取与公开写入拒绝的合同回归。

输入为预算工厂、旧合同和公开请求；输出为配额、快照隔离及拒绝写入的断言。
具体工作流为构造新授权快照、读取旧值，再通过联合、HTTP 和服务边界尝试修改。
示例：python -m pytest backend/tests/test_workspace_budget_presets.py -q。
"""

import asyncio

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from backend.app.desktop.patrol.agent_loop.governance.authority_control import LoopAuthorityService
from backend.app.desktop.patrol.agent_loop.inputs.patrol_inputs import PatrolInputRequest, request_document
from backend.app.desktop.patrol.agent_loop.resources.unlimited_budget import unlimited_loop_budgets
from backend.app.desktop.patrol.agent_loop.routes import agent_loop_router
from backend.app.desktop.patrol.agent_loop.schemas import (
    AdjustLoopBudgetsRequest,
    IncreaseLoopBudgetRequest,
    LoopBudgetContract,
    LoopGrantMutationRequest,
)


def test_new_budget_has_no_product_quotas_and_keeps_stagnation_protection():
    budget = unlimited_loop_budgets()
    for name, limit in budget.items():
        if name.startswith("max_") and name != "max_no_progress":
            assert limit is None, name
    assert budget["max_no_progress"] == 20
    assert all(value is None for name, value in budget["expansion_resources"].items() if name != "version")
    assert budget["expansion_resources_source"] == "default"
    budget["expansion_resources"]["max_exact_reads"] = 5
    assert unlimited_loop_budgets()["expansion_resources"]["max_exact_reads"] is None


def test_history_budget_can_still_be_read_without_becoming_a_public_mutation():
    legacy = LoopBudgetContract.model_validate({"max_rounds": 40, "max_concurrent_runs": 2})
    assert legacy.max_rounds == 40
    assert legacy.max_concurrent_runs == 2
    assert LoopBudgetContract.model_validate(unlimited_loop_budgets()).max_rounds is None
    with pytest.raises(ValidationError):
        LoopBudgetContract(max_rounds=0)


@pytest.mark.parametrize("preset", [None, "normal", "long", "unlimited"])
def test_input_rejects_removed_budget_preset(preset):
    with pytest.raises(ValidationError):
        PatrolInputRequest.model_validate({"submission_id": "stable", "content": "任务", "budget_preset": preset})
    document = request_document(PatrolInputRequest(submission_id="stable", content="任务"))
    assert "budget_preset" not in document


@pytest.mark.parametrize("payload", [
    {"command": "adjust_budgets", "budgets": {"max_rounds": 10}},
    {"command": "increase_budget", "preset": "normal", "expected_authority_revision": 1},
    {"command": "narrow", "capabilities": ["continue_context"], "context_scope": ["c"], "permission_scope": ["read"], "budgets": {"max_rounds": 10}},
])
def test_public_grant_union_and_http_boundary_reject_budget_changes(payload):
    with pytest.raises(ValidationError):
        TypeAdapter(LoopGrantMutationRequest).validate_python(payload)
    app = FastAPI()
    app.include_router(agent_loop_router)
    with TestClient(app) as client:
        assert client.post("/desktop/api/agent-loops/loop/grant", json=payload).status_code == 422
    assert "/desktop/api/agent-loops/budget-presets" not in app.openapi()["paths"]


@pytest.mark.parametrize("mutation", [
    AdjustLoopBudgetsRequest(command="adjust_budgets", budgets={"max_rounds": 10}),
    IncreaseLoopBudgetRequest(command="increase_budget", preset="normal", expected_authority_revision=1),
])
def test_internal_service_also_rejects_retired_budget_commands_before_mutation(mutation):
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(LoopAuthorityService(None).mutate("loop", mutation))
    assert rejected.value.status_code == 422
