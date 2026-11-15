"""验证 Workspace Patrol Context 装备缩权与共享意见生命周期。

输入为隔离 PostgreSQL、Context 装备和工作线权限；输出为角色缩权、拒绝越权及生命周期边界断言。
具体工作流为播种 Workspace Patrol、修改工作线读权限，再验证共享装备解析和意见状态机。
示例：pytest backend/tests/test_loop_context_equipment.py。
"""

import asyncio
from datetime import UTC, datetime
import os
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.patrol.agent_loop.execution.directive_equipment import resolve_context_equipment
from backend.app.desktop.patrol.agent_loop.governance.intervention_lifecycle import InterventionLifecycleRepository, InterventionTransitionRejected
from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopDelegationGrant, LoopUserIntent
from backend.app.desktop.contexts.curation.models import CurationLane
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop

pytestmark = pytest.mark.usefixtures("runtime_postgres_database")


def test_server_equipment_and_opinion_lifecycle_are_role_and_type_bound(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="user-equipment", started_at=datetime.now(UTC))
        try:
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                loop.equipment = {"model_name": "model-original", "skills": ["vault"],
                    "permissions": ["read", "write", "host_command"], "access_mode": "danger-full-access"}
                grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop.loop_id))
                grant.permission_scope = loop.equipment["permissions"]
                assert await resolve_context_equipment(session, loop, fixture["context_id"]) == {
                    **loop.equipment, "skill_snapshots": []}
                narrowed = await resolve_context_equipment(session, loop, fixture["context_id"],
                    overrides={"permissions": ["read"], "access_mode": "read-only"})
                assert narrowed["permissions"] == ["read"] and narrowed["access_mode"] == "read-only"
                lane = await session.scalar(select(CurationLane).where(CurationLane.program_id == loop.program_id))
                lane.lane_policy = {**lane.lane_policy, "workspace_mode": "read_only"}
                read_only = await resolve_context_equipment(session, loop, fixture["context_id"])
                assert read_only["permissions"] == ["read"] and read_only["access_mode"] == "read-only"
                with pytest.raises(ValueError, match="超出"):
                    await resolve_context_equipment(session, loop, fixture["context_id"], overrides={"permissions": ["write"]})
                opinion = LoopUserIntent(intent_id=uuid.uuid4().hex, loop_id=loop.loop_id, scope="portfolio",
                    content="审查布局", correlation_id=uuid.uuid4().hex, goal_revision=1, authority_revision=1)
                session.add(opinion)
                lifecycle = InterventionLifecycleRepository()
                await lifecycle.register(session, opinion)
                await lifecycle.transition(session, opinion.intent_id, "accepted")
                await lifecycle.transition(session, opinion.intent_id, "observed")
                with pytest.raises(InterventionTransitionRejected):
                    await lifecycle.transition(session, opinion.intent_id, "delivered")
                await lifecycle.transition(session, opinion.intent_id, "addressed")
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())
