"""通过真实工作区输入和根授权事务验证预算追加、等待及并发点击。"""

import asyncio
from datetime import UTC, datetime, timedelta
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.tests.test_workspace_patrol import _setup
from backend.app.desktop.agent_loop.authority_control import LoopAuthorityService
from backend.app.desktop.agent_loop.budget_presets import initial_budgets
from backend.app.desktop.agent_loop.context_expansion.models import LoopIndexBudgetReservation
from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopDelegationGrant
from backend.app.desktop.agent_loop.patrol_inputs import PatrolInputRequest
from backend.app.desktop.agent_loop.schemas import IncreaseLoopBudgetRequest
from backend.app.desktop.agent_loop.wait_models import LoopWaitRequest
from backend.app.desktop.agent_loop.wait_requests import LoopWaitRequestFactory, LoopWaitRequestService

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


@pytest.mark.parametrize("preset", [None, "long"])
def test_workspace_first_input_uses_preset_and_retry_never_resets_budget(tmp_path, preset):
    async def run():
        engine, sessions, workspace_id, service = await _setup(tmp_path)
        try:
            request = PatrolInputRequest(submission_id=uuid.uuid4().hex, content="任务", budget_preset=preset)
            receipt = await service.submit(workspace_id, request)
            authority = LoopAuthorityService(sessions)
            await authority.mutate(receipt["loop_id"], IncreaseLoopBudgetRequest(
                command="increase_budget", preset="normal", expected_authority_revision=1))
            assert await service.submit(workspace_id, request) == receipt
            async with sessions() as session:
                grant = await session.scalar(select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == receipt["loop_id"], LoopDelegationGrant.status == "active"))
                assert grant.budgets["max_model_calls"] == initial_budgets(preset)["max_model_calls"] + 100
                assert grant.budgets["max_concurrent_runs"] == initial_budgets(preset)["max_concurrent_runs"]
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_increase_keeps_consumption_pause_and_rejects_duplicate_version(tmp_path):
    async def run():
        engine, sessions, workspace_id, service = await _setup(tmp_path)
        try:
            receipt = await service.submit(workspace_id, PatrolInputRequest(submission_id=uuid.uuid4().hex, content="任务"))
            loop_id = receipt["loop_id"]
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, loop_id)
                loop.status = "paused"
                loop.created_at = datetime.now(UTC) - timedelta(hours=3)
                usage = await session.get(LoopBudgetUsage, loop_id)
                usage.model_calls = 100
                session.add(LoopIndexBudgetReservation(reservation_id=uuid.uuid4().hex,
                    loop_id=loop_id, grant_revision=1, model_calls=50, input_tokens=0, output_tokens=0))
            request = IncreaseLoopBudgetRequest(command="increase_budget", preset="normal", expected_authority_revision=1)
            authority = LoopAuthorityService(sessions)
            result = await asyncio.gather(authority.mutate(loop_id, request), authority.mutate(loop_id, request), return_exceptions=True)
            assert sum(isinstance(item, dict) for item in result) == 1
            failure = next(item for item in result if isinstance(item, HTTPException))
            assert failure.status_code == 409
            async with sessions() as session:
                loop = await session.get(AgentLoop, loop_id)
                grant = await session.scalar(select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == loop_id, LoopDelegationGrant.status == "active"))
                assert loop.status == "paused"
                assert grant.budgets["max_model_calls"] == 250
                assert grant.budgets["max_duration_seconds"] >= 12600
                assert (await session.get(LoopBudgetUsage, loop_id)).model_calls == 100
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("budget_wait", [True, False])
def test_increase_only_resolves_budget_wait(tmp_path, budget_wait):
    async def run():
        engine, sessions, workspace_id, service = await _setup(tmp_path)
        try:
            receipt = await service.submit(workspace_id, PatrolInputRequest(submission_id=uuid.uuid4().hex, content="任务"))
            loop_id = receipt["loop_id"]
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, loop_id)
                draft = LoopWaitRequestFactory.budget_action("用尽", initial_budgets()) if budget_wait else LoopWaitRequestFactory.retry_or_stop("失败需要决策")
                wait = await LoopWaitRequestService().open(session, loop, draft, created_by="test", correlation_id=uuid.uuid4().hex)
                wait_id, revision = wait.request_id, loop.authority_revision
            operation = LoopAuthorityService(sessions).mutate(loop_id, IncreaseLoopBudgetRequest(
                command="increase_budget", preset="normal", expected_authority_revision=revision))
            if budget_wait:
                await operation
            else:
                with pytest.raises(HTTPException) as rejected:
                    await operation
                assert rejected.value.status_code == 409
            async with sessions() as session:
                loop = await session.get(AgentLoop, loop_id)
                wait = await session.get(LoopWaitRequest, wait_id)
                assert loop.status == ("running" if budget_wait else "waiting_user")
                assert wait.status == ("superseded" if budget_wait else "open")
        finally:
            await engine.dispose()
    asyncio.run(run())
