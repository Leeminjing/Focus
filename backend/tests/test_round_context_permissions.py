"""本文件验证 Patrol 权限清单与重叠 Round 的执行授权隔离。

输入为完整 Context 清单、可信角色上限及真实 PostgreSQL 决策/Directive；输出为缺项、越权拒绝与逐轮失效断言。
工作流先验证决策合同，再令 R5 与 R6 对同一 Context 分别授权可写和只读，检查新 Mission 不覆盖旧授权，取消、撤权、到期及结算后禁止新调用。
示例：python -m pytest backend/tests/test_round_context_permissions.py -q；权限读取不启动模型或执行进程。
"""

import asyncio
from datetime import UTC, datetime, timedelta
import os
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_agent_loop_round_liveness import _seed_loop, _stop
from test_loop_execution_ownership import _seed_launching_directive

from backend.app.desktop.agent_loop import directive_equipment
from backend.app.desktop.agent_loop.models import AgentLoop, LoopContextMembership, LoopDecision, LoopDelegationGrant, LoopDirective, LoopRound
from backend.app.desktop.agent_loop.schemas import ContextPermission


def test_permission_inventory_requires_every_context_and_stays_below_role_ceiling(monkeypatch):
    async def exercise():
        async def equipment(session, loop, context_id):
            return {"access_mode": "workspace-write" if context_id == "writer" else "read-only",
                    "permissions": ["read", "write"] if context_id == "writer" else ["read"]}

        monkeypatch.setattr(directive_equipment, "resolve_context_equipment", equipment)
        loop = SimpleNamespace(equipment={"access_mode": "workspace-write", "permissions": ["read", "write"]})
        observation = SimpleNamespace(portfolio_frontier=[{"context_id": "writer"}, {"context_id": "reader"}])

        def proposal(entries, actions=()):
            return SimpleNamespace(context_permissions=tuple(ContextPermission(context_id=key, access_mode=mode)
                for key, mode in entries), actions=actions)

        legal = proposal((("writer", "workspace-write"), ("reader", "read-only")))
        resolved = await directive_equipment.validate_context_permissions(None, loop, observation, legal)
        assert "host_command" not in resolved["writer"]["permissions"]
        for entries in ((("writer", "read-only"),),
                        (("writer", "read-only"), ("reader", "read-only"), ("reader", None)),
                        (("writer", "danger-full-access"), ("reader", "read-only")),
                        (("writer", "read-only"), ("reader", "workspace-write"))):
            with pytest.raises(ValueError):
                await directive_equipment.validate_context_permissions(None, loop, observation, proposal(entries))
        execute = SimpleNamespace(action="continue_context", context_id="writer")
        with pytest.raises(ValueError, match="未获授"):
            await directive_equipment.validate_context_permissions(None, loop, observation,
                proposal((("writer", None), ("reader", "read-only")), (execute,)))
        derived = SimpleNamespace(action="create_lane", plan=SimpleNamespace(lane_policy={"workspace_mode": "isolated_write"}))
        grant = proposal((("writer", None), ("reader", None), ("action:0", "workspace-write")), (derived,))
        assert (await directive_equipment.validate_context_permissions(None, loop, observation, grant))["action:0"]["access_mode"] == "workspace-write"

    asyncio.run(exercise())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_committed_round_permissions_survive_new_round_and_expire_independently(tmp_path):
    async def exercise():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="round-mode", started_at=datetime.now(UTC))
        try:
            first_id = await _seed_launching_directive(sessions, fixture, label="write")
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                loop.equipment = {**loop.equipment, "access_mode": "workspace-write"}
                first = await session.get(LoopDirective, first_id)
                decision = await session.get(LoopDecision, first.decision_id)
                decision.intent = {"context_permissions": [{"context_id": fixture["context_id"], "access_mode": "workspace-write"}]}
                first_equipment = await directive_equipment.resolve_directive_equipment(session, loop, first)
                older = await session.get(LoopRound, fixture["round_id"])
                new_id = uuid.uuid4().hex
                session.add(LoopRound(round_id=new_id, loop_id=loop.loop_id, number=older.number + 1,
                    authority_revision=1, goal_revision=2, frontier_hash=older.frontier_hash, workspace_revision=1))
                loop.current_round_id, loop.goal_revision = new_id, 2
            second_id = await _seed_launching_directive(sessions, {**fixture, "round_id": new_id}, label="read")
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                second = await session.get(LoopDirective, second_id)
                second.goal_revision = 2
                second_equipment = await directive_equipment.resolve_directive_equipment(session, loop, second)
                def run(round_id, directive_id, equipment):
                    return SimpleNamespace(loop_id=loop.loop_id, round_id=round_id, directive_id=directive_id,
                        task_id=fixture["context_id"], equipment=equipment, status="pending", settled_at=None)
                r5 = run(fixture["round_id"], first_id, first_equipment)
                r6 = run(new_id, second_id, second_equipment)
                assert (await directive_equipment.round_permission_state(session, r5))["access_mode"] == "workspace-write"
                assert (await directive_equipment.round_permission_state(session, r6))["access_mode"] == "read-only"
                assert (await directive_equipment.round_permission_state(session, r5))["valid"]
                assert (await directive_equipment.round_permission_state(session, r6))["valid"]
                (await session.get(LoopDirective, first_id)).lifecycle_state = "cancelled"
                assert not (await directive_equipment.round_permission_state(session, r5))["valid"]
                assert (await directive_equipment.round_permission_state(session, r6))["valid"]
                membership = await session.scalar(select(LoopContextMembership).where(
                    LoopContextMembership.loop_id == loop.loop_id,
                    LoopContextMembership.context_id == fixture["context_id"]))
                membership.status = "archived"
                assert not (await directive_equipment.round_permission_state(session, r6))["valid"]
                membership.status = "active"
                grant = await session.get(LoopDelegationGrant, second.grant_id)
                grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                assert not (await directive_equipment.round_permission_state(session, r6))["valid"]
                grant.expires_at, grant.status = None, "revoked"
                assert not (await directive_equipment.round_permission_state(session, r6))["valid"]
                grant.status = "active"
                assert (await directive_equipment.round_permission_state(session, r6))["valid"]
                (await session.get(LoopRound, new_id)).status = "settled"
                assert not (await directive_equipment.round_permission_state(session, r6))["valid"]
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(exercise())
