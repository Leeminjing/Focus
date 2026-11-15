"""本文件对外提供 Loop 固定无限预算的运行时回归测试。

输入为高累计用量、可空派生限制、历史有限合同及已有活动 Run；输出为预算准入、资源剩余额度和派发容量断言。
具体工作流为调用生产派生与压缩政策，并在隔离 PostgreSQL 中验证 Kernel、Coordinator 和 Dispatcher；实际运行池容量继续生效。
示例：`pytest backend/tests/test_unlimited_loop_runtime.py -q`。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.patrol.agent_loop.compression_authority.candidates import (
    CompressionCandidateService,
)
from backend.app.desktop.patrol.agent_loop.context_expansion.policy import (
    ExpansionAdmissionPolicy,
)
from backend.app.desktop.patrol.agent_loop.context_expansion.signals import (
    ExpansionSignalCollector,
)
from backend.app.desktop.patrol.agent_loop.execution.dispatch import LoopWaveDispatcher
from backend.app.desktop.patrol.agent_loop.models import (
    LoopBudgetUsage,
    LoopDelegationGrant,
)
from backend.app.desktop.patrol.agent_loop.resources.expansion_resource_policy import (
    resolve_expansion_resources,
)
from backend.app.desktop.patrol.agent_loop.resources.unlimited_budget import (
    unlimited_loop_budgets,
)
from backend.app.desktop.patrol.agent_loop.schemas import PatrolDecisionIntent
from backend.app.desktop.persistence.models import DesktopRun
from backend.tests.loop_run_boundary_support import commit_patrol
from backend.tests.test_loop_runtime_pools import _create_loop, _create_wave, _frontier
from backend.tests.test_semantic_context_admission import (
    _authorized_observation,
    _opportunity,
    _spec,
)


@pytest.mark.parametrize("limits", [{}, unlimited_loop_budgets()])
def test_expansion_admits_work_beyond_old_product_quotas(limits):
    observation = _authorized_observation().model_copy(update={
        "budget": {"limits": limits, "usage": {"contexts": 100, "lanes": 100, "input_tokens": 10_000_000}},
        "stable_results": tuple({"status": "running"} for _ in range(20)),
    })
    opportunity = _opportunity(observation, _spec())
    result = ExpansionAdmissionPolicy().evaluate(observation, (opportunity,))
    assert result.level == "recommended"
    assert result.opportunities == (opportunity,)
    signals = ExpansionSignalCollector().collect(observation).signals
    assert all(signal.kind != "token_pressure" for signal in signals)
    budget_signal = next(signal for signal in signals if signal.kind == "budget_state")
    assert dict(budget_signal.facts)["max_contexts"] == "None"


@pytest.mark.parametrize("field,code", [
    ("max_contexts", "context_budget_exhausted"),
    ("max_lanes", "lane_budget_exhausted"),
    ("max_new_lanes_per_round", "round_lane_budget_exhausted"),
    ("max_concurrent_runs", "concurrency_budget_exhausted"),
])
def test_historical_zero_expansion_limits_remain_enforced(field, code):
    observation = _authorized_observation().model_copy(update={
        "budget": {"limits": {field: 0}, "usage": {}},
    })
    result = ExpansionAdmissionPolicy().evaluate(observation, (_opportunity(observation, _spec()),))
    assert result.level == "not_applicable"
    assert result.blockers[0].code == code


@pytest.mark.parametrize("limits", [{}, unlimited_loop_budgets()])
def test_expansion_and_compression_keep_unlimited_global_remainders(limits):
    usage = SimpleNamespace(model_calls=50_000, input_tokens=10_000_000, output_tokens=5_000_000, retries=100)
    resources = resolve_expansion_resources(limits, 1, vars(usage))
    assert resources.global_model_calls_remaining is None
    assert resources.global_input_tokens_remaining is None
    assert resources.global_output_tokens_remaining is None
    CompressionCandidateService._require_candidate_budget(usage, limits, 50_000, 10_000)


@pytest.mark.usefixtures("isolated_postgres_database")
def test_kernel_and_coordinator_allow_high_cumulative_usage_with_unlimited_grant(tmp_path):
    from backend.app.desktop.patrol.agent_loop.coordinator import (
        CoordinatorClaim,
        LoopCoordinator,
    )

    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, context_id, revision_id = await _create_loop(sessions, tmp_path)
        try:
            async with sessions.begin() as session:
                grant = await session.get(LoopDelegationGrant, snapshot["grant"]["grant_id"])
                grant.budgets = unlimited_loop_budgets()
                usage = await session.get(LoopBudgetUsage, snapshot["loop_id"])
                usage.rounds, usage.model_calls = 500, 50_000
                usage.input_tokens, usage.output_tokens, usage.retries = 10_000_000, 5_000_000, 100
            intent = PatrolDecisionIntent(
                decision_id=uuid.uuid4().hex, idempotency_key=uuid.uuid4().hex,
                loop_id=snapshot["loop_id"], loop_revision=snapshot["revision"],
                round_id=snapshot["current_round_id"], holder_id=snapshot["holder_id"],
                grant_id=snapshot["grant"]["grant_id"], grant_revision=1, goal_revision=1,
                observed_frontier_hash=await _frontier(sessions, snapshot["current_round_id"]),
                observed_workspace_revision=1, rationale="Continue after substantial completed work.",
                actions=({"action": "continue_context", "context_id": context_id,
                          "context_revision_id": revision_id, "message": "Verify the remaining work."},),
            )
            result = await commit_patrol(sessions, intent)
            assert result.status == "committed", result.reason
            capacities = []

            class Dispatcher:
                async def dispatch(self, loop_id, round_id, capacity):
                    capacities.append(capacity)
                    return ()

            await LoopCoordinator(sessions).dispatch_ready(
                CoordinatorClaim("lease", snapshot["loop_id"], snapshot["current_round_id"], "1"),
                Dispatcher(), 2,
            )
            assert capacities == [2]
        finally:
            await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_unlimited_dispatch_uses_free_machine_capacity_with_existing_run(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, contexts, _ = await _create_wave(sessions, tmp_path)
        try:
            async with sessions.begin() as session:
                grant = await session.get(LoopDelegationGrant, snapshot["grant"]["grant_id"])
                grant.budgets = unlimited_loop_budgets()
                session.add(DesktopRun(
                    run_id=uuid.uuid4().hex, task_id=contexts[0][0], agent_id="existing-worker",
                    kind="teammate", status="running", origin="delegated_patrol",
                    execution_thread_id=uuid.uuid4().hex, loop_id=snapshot["loop_id"],
                    round_id=snapshot["current_round_id"],
                ))
            claimed = await LoopWaveDispatcher(sessions, object())._claim(
                snapshot["loop_id"], snapshot["current_round_id"], 1,
            )
            assert len(claimed) == 1
        finally:
            await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())
