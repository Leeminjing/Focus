"""本文件对外提供固定无上限预算与历史受理重放的真实 PostgreSQL 回归。

输入为隔离工作区、用户输入及历史预算档位文档；输出为授权、用量和稳定受理身份断言。
具体工作流为首次受理、收窄权限、重放当前或旧回执，核对用量及历史文档保持不变。
示例：python -m pytest backend/tests/test_workspace_budget_lifecycle.py -q。
"""

import asyncio
import uuid
from copy import deepcopy

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.tests.test_workspace_patrol import _setup
from backend.app.desktop.patrol.agent_loop.governance.authority_control import LoopAuthorityService
from backend.app.desktop.patrol.agent_loop.inputs.patrol_inputs import PatrolInputRequest
from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopDelegationGrant, LoopUserIntent
from backend.app.desktop.patrol.agent_loop.resources.unlimited_budget import unlimited_loop_budgets
from backend.app.desktop.patrol.agent_loop.schemas import NarrowLoopGrantRequest
from focus.history import content_hash

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_first_input_and_replay_use_unlimited_budget_without_resetting_consumption(tmp_path):
    async def run():
        engine, sessions, workspace_id, service = await _setup(tmp_path)
        try:
            request = PatrolInputRequest(submission_id=uuid.uuid4().hex, content="任务")
            receipt = await service.submit(workspace_id, request)
            loop_id = receipt["loop_id"]
            async with sessions.begin() as session:
                grant = await session.scalar(select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == loop_id, LoopDelegationGrant.status == "active"))
                assert grant.budgets == unlimited_loop_budgets()
                context_scope = tuple(grant.context_scope)
                capabilities = tuple(grant.capabilities)
                usage = await session.get(LoopBudgetUsage, loop_id)
                usage.model_calls, usage.input_tokens, usage.output_tokens = 500, 3_000_000, 400_000
            await LoopAuthorityService(sessions).mutate(loop_id, NarrowLoopGrantRequest(
                command="narrow", capabilities=capabilities, context_scope=context_scope, permission_scope=("read",)))
            assert await service.submit(workspace_id, request) == receipt
            async with sessions() as session:
                grant = await session.scalar(select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == loop_id, LoopDelegationGrant.status == "active"))
                usage = await session.get(LoopBudgetUsage, loop_id)
                assert grant.budgets == unlimited_loop_budgets()
                assert grant.permission_scope == ["read"]
                assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (500, 3_000_000, 400_000)
                assert (await session.get(AgentLoop, loop_id)).authority_revision == 2
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("preset", ["normal", "long"])
def test_replay_of_durable_pre_upgrade_input_ignores_only_its_retired_preset(tmp_path, preset):
    async def run():
        engine, sessions, workspace_id, service = await _setup(tmp_path)
        try:
            request = PatrolInputRequest(submission_id=uuid.uuid4().hex, input_type="outcome", content="完成任务")
            receipt = await service.submit(workspace_id, request)
            # Freeze the record exactly as the older server persisted it before losing its reply.
            async with sessions.begin() as session:
                row = await session.get(LoopUserIntent, receipt["intent_id"])
                document = {**row.request_payload["request"], "budget_preset": preset}
                historical = {**row.request_payload, "request": document,
                              "request_hash": content_hash([workspace_id, document])}
                row.request_payload = deepcopy(historical)
            assert await service.submit(workspace_id, request) == receipt
            async with sessions() as session:
                row = await session.get(LoopUserIntent, receipt["intent_id"])
                assert row.request_payload == historical
                loop = await session.get(AgentLoop, receipt["loop_id"])
                assert loop.authority_revision == 1
            for changed in (
                request.model_copy(update={"content": "另一任务"}),
                request.model_copy(update={"input_type": "boundary"}),
                request.model_copy(update={"access_mode": "read-only"}),
                request.model_copy(update={"request_id": "other", "request_revision": 1}),
            ):
                with pytest.raises(HTTPException) as rejected:
                    await service.submit(workspace_id, changed)
                assert rejected.value.status_code == 409
            # A real second workspace must not replay the globally stable submission identity.
            from backend.app.desktop.persistence.models import DesktopWorkspace
            other_workspace_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=other_workspace_id, path=str(tmp_path / "other"), display_name="other"))
            (tmp_path / "other").mkdir()
            with pytest.raises(HTTPException) as rejected:
                await service.submit(other_workspace_id, request)
            assert rejected.value.status_code == 409
        finally:
            await engine.dispose()
    asyncio.run(run())
