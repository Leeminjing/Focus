"""本文件对外提供预算升级后沿用当前 Mission 的真实 PostgreSQL 恢复回归。

输入为旧有限授权、已落定轮次与类型化目标等待；输出为版本化恢复及严格身份拒绝断言。
具体工作流为执行生产升级，核对旧轮次不变，再显式沿用当前 Mission 创建新授权下的后继。
示例：python -m pytest backend/tests/test_mission_wait_budget_upgrade.py -q。
"""

import asyncio
import os
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.patrol.agent_loop.journal.journal_models import (
    LoopJournalEvent,
)
from backend.app.desktop.patrol.agent_loop.lifecycle.unlimited_budget_upgrade import (
    upgrade_workspace_loop_budgets,
)
from backend.app.desktop.patrol.agent_loop.missions.mission_models import (
    LoopMissionRevision,
)
from backend.app.desktop.patrol.agent_loop.models import (
    AgentLoop,
    LoopDelegationGrant,
    LoopPendingDecision,
    LoopRound,
)
from backend.app.desktop.patrol.agent_loop.schemas import (
    ResumeWithCurrentMissionRequest,
)
from backend.app.desktop.patrol.agent_loop.waits.wait_models import LoopWaitRequest
from backend.app.desktop.patrol.agent_loop.waits.wait_requests import (
    LoopWaitRequestFactory,
    LoopWaitRequestService,
)
from backend.app.desktop.persistence.models import DesktopThread
from backend.tests.workspace_loop_support import _seed_greeting_loop

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


async def _seed_wait(sessions, tmp_path, status="settled"):
    fixture = await _seed_greeting_loop(sessions, tmp_path)
    async with sessions.begin() as session:
        loop = await session.get(AgentLoop, fixture["loop_id"])
        source = await session.get(LoopRound, loop.current_round_id)
        source.status = status
        grant = await session.scalar(select(LoopDelegationGrant).where(
            LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.status == "active"))
        grant.budgets = {**grant.budgets, "max_rounds": 40, "max_model_calls": 100}
        wait = await LoopWaitRequestService().open(session, loop, LoopWaitRequestFactory.clarification(
            "请重新说明目标", {"cause": "missing_goal", "mission_revision": loop.goal_revision,
                            "evidence_identity": {"kind": "mission", "reference_id": "outcome", "revision": loop.goal_revision}}),
            created_by="portfolio-patrol", correlation_id=uuid.uuid4().hex, round_id=source.round_id)
        fixture.update(wait_id=wait.request_id, source_round_id=source.round_id, source_grant_id=grant.grant_id)
    return fixture


def _confirmation(fixture, revision):
    return ResumeWithCurrentMissionRequest(confirmation="resume_with_current_mission", request_revision=revision,
                                          idempotency_key=f"upgrade-mission:{fixture['wait_id']}:{revision}")


@pytest.mark.parametrize("status", ["settled", "error"])
@pytest.mark.parametrize("backfill", [False, True])
def test_budget_upgrade_keeps_old_round_and_explicit_mission_recovery_works_after_journal_retention(tmp_path, status, backfill):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture = await _seed_wait(sessions, tmp_path, status)
            loop_id, wait_id = fixture["loop_id"], fixture["wait_id"]
            assert loop_id in await upgrade_workspace_loop_budgets(sessions)
            if backfill:
                # Earlier startup upgrades left this typed wait open without a recovery marker.
                async with sessions.begin() as session:
                    wait = await session.get(LoopWaitRequest, wait_id)
                    wait.scope = {name: value for name, value in wait.scope.items() if name != "authority_upgrade"}
                    wait.revision = 1
                    target_id = await session.scalar(select(LoopDelegationGrant.grant_id).where(
                        LoopDelegationGrant.loop_id == loop_id, LoopDelegationGrant.status == "active"))
                assert loop_id in await upgrade_workspace_loop_budgets(sessions)
                async with sessions() as session:
                    assert (await session.get(LoopDelegationGrant, target_id)).status == "active"
            async with sessions() as session:
                loop = await session.get(AgentLoop, loop_id)
                source = await session.get(LoopRound, fixture["source_round_id"])
                wait = await session.get(LoopWaitRequest, wait_id)
                assert loop.status == "waiting_user" and loop.authority_revision == 2
                assert (source.status, source.authority_revision) == (status, 1)
                assert wait.kind == "clarification" and wait.response_mode == "text" and wait.revision == 2
                assert wait.scope["cause"] == "missing_goal" and wait.scope["evidence_identity"]["kind"] == "mission"
            assert loop_id not in await upgrade_workspace_loop_budgets(sessions)
            with pytest.raises(HTTPException) as stale:
                await fixture["service"].resume_with_current_mission(loop_id, wait_id, _confirmation(fixture, 1), actor_id="user")
            assert stale.value.status_code == 409
            # Display journal retention must not erase the authority proof held by grant history.
            async with sessions.begin() as session:
                await session.execute(delete(LoopJournalEvent).where(LoopJournalEvent.loop_id == loop_id))
            result = await fixture["service"].resume_with_current_mission(loop_id, wait_id, _confirmation(fixture, 2), actor_id="user")
            replay = await fixture["service"].resume_with_current_mission(loop_id, wait_id, _confirmation(fixture, 2), actor_id="user")
            assert result["created"] is True and replay["created"] is False
            assert result["current_round_id"] == replay["current_round_id"] != fixture["source_round_id"]
            async with sessions() as session:
                source = await session.get(LoopRound, fixture["source_round_id"])
                successor = await session.get(LoopRound, result["current_round_id"])
                assert (source.status, source.authority_revision) == (status, 1)
                assert successor.authority_revision == 2 and successor.goal_revision == source.goal_revision
                assert (await session.get(LoopWaitRequest, wait_id)).status == "superseded"
                assert (await session.get(AgentLoop, loop_id)).status == "running"
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("invalid", [
    "marker_missing", "source_grant", "source_round", "target_grant", "target_authority", "mission_revision",
    "source_not_revoked", "changed_permissions", "missing_mission", "human_gate", "missing_frontier",
])
def test_budget_upgrade_mission_recovery_rejects_invalid_lineage_or_current_blockers(tmp_path, invalid):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture = await _seed_wait(sessions, tmp_path)
            loop_id, wait_id = fixture["loop_id"], fixture["wait_id"]
            await upgrade_workspace_loop_budgets(sessions)
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, loop_id)
                wait = await session.get(LoopWaitRequest, wait_id)
                source = await session.get(LoopDelegationGrant, fixture["source_grant_id"])
                target = await session.scalar(select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == loop_id, LoopDelegationGrant.status == "active"))
                scope = dict(wait.scope)
                marker = dict(scope["authority_upgrade"])
                marker_fields = {"source_grant": "source_grant_id", "source_round": "source_round_id",
                                 "target_grant": "grant_id", "target_authority": "authority_revision",
                                 "mission_revision": "mission_revision"}
                if invalid == "marker_missing":
                    scope.pop("authority_upgrade")
                elif invalid in marker_fields:
                    marker[marker_fields[invalid]] = 99 if invalid in {"target_authority", "mission_revision"} else uuid.uuid4().hex
                    scope["authority_upgrade"] = marker
                elif invalid == "source_not_revoked":
                    source.status = "expired"
                elif invalid == "changed_permissions":
                    target.permission_scope = [*target.permission_scope, "host_command"]
                elif invalid == "missing_mission":
                    mission = await session.scalar(select(LoopMissionRevision).where(
                        LoopMissionRevision.loop_id == loop_id, LoopMissionRevision.revision == loop.goal_revision))
                    mission.outcome = None
                elif invalid == "human_gate":
                    session.add(LoopPendingDecision(pending_decision_id=uuid.uuid4().hex, loop_id=loop_id,
                                                    kind="permission", delegable=False, payload={}))
                elif invalid == "missing_frontier":
                    (await session.get(DesktopThread, loop.initial_context_id)).current_revision_id = None
                wait.scope = scope
            with pytest.raises(HTTPException) as rejected:
                await fixture["service"].resume_with_current_mission(loop_id, wait_id, _confirmation(fixture, 2), actor_id="user")
            assert rejected.value.status_code == 409
            async with sessions() as session:
                loop = await session.get(AgentLoop, loop_id)
                assert loop.status == "waiting_user" and loop.current_round_id == fixture["source_round_id"]
                assert (await session.get(LoopWaitRequest, wait_id)).status == "open"
                assert (await session.get(LoopRound, fixture["source_round_id"])).authority_revision == 1
        finally:
            await engine.dispose()
    asyncio.run(run())
