"""本文件对外提供预算升级保留旧人工审批来源的真实 PostgreSQL 回归。

输入为合法唯一旧 Directive 来源、人工恢复事实和已解除的检查点；输出为升级前来源有效、
升级后仍须完整人工恢复与检查点核对的断言。具体工作流为复用正式审批播种，仅替换检查点读取。
示例：pytest backend/tests/test_workspace_budget_upgrade_access_recovery.py。
"""

import asyncio
import os
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.patrol.agent_loop.lifecycle.unlimited_budget_upgrade import (
    upgrade_workspace_loop_budgets,
)
from backend.app.desktop.patrol.agent_loop.models import (
    AgentLoop,
    LoopDelegationGrant,
    LoopDirective,
    LoopPendingDecision,
)
from backend.app.desktop.patrol.agent_loop.recovery.user_access_gate_recovery import (
    UserAccessGateRecovery,
)
from backend.app.desktop.patrol.agent_loop.schemas import LoopWaitResponseRequest
from backend.app.desktop.patrol.agent_loop.waits.wait_models import LoopWaitRequest
from backend.app.desktop.persistence.models import DesktopRun
from backend.app.desktop.runs.orchestration.lifecycle import RunLifecycleFinalizer
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.test_loop_user_access_gate_recovery import _handled
from backend.tests.test_workspace_unlimited_budget_upgrade import _OLD_BUDGETS


def test_upgrade_preserves_legacy_source_without_approving_human_gate(runtime_postgres_database, tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            for variant in ("valid", "delayed_settlement", "no_human_resume", "still_pending", "tampered_gate", "tampered_round", "tampered_run", "explicit", "unproven_source"):
                directory = tmp_path / variant
                directory.mkdir()
                fixture = await _seed_loop(sessions, directory, label="gate", started_at=datetime.now(UTC))
                service = fixture["service"]
                try:
                    gate, original, resumed, wait, old_body, asked = await _handled(sessions, fixture, legacy=variant != "explicit")
                    async with sessions.begin() as session:
                        grant = await session.scalar(select(LoopDelegationGrant).where(
                            LoopDelegationGrant.loop_id == fixture["loop_id"], LoopDelegationGrant.status == "active"))
                        grant.budgets = dict(_OLD_BUDGETS)
                        loop = await session.get(AgentLoop, fixture["loop_id"])
                        request = await session.get(LoopWaitRequest, wait["request_id"])
                        stored_gate = await session.get(LoopPendingDecision, gate.pending_decision_id)
                        (await session.get(DesktopRun, original.run_id)).created_at = stored_gate.created_at - timedelta(seconds=1)
                        await session.flush()
                        source = await service._user_gate_recovery._source(session, loop, request, stored_gate)
                        assert source.run_id == original.run_id
                        if variant == "unproven_source":
                            source.equipment = {"access_mode": "read-only"}
                        if variant == "delayed_settlement":
                            source.settled_at = None
                    assert await upgrade_workspace_loop_budgets(sessions) == (fixture["loop_id"],)
                    assert await upgrade_workspace_loop_budgets(sessions) == ()
                    async with sessions.begin() as session:
                        request = await session.get(LoopWaitRequest, wait["request_id"])
                        marker = request.scope.get("legacy_access_source")
                        if variant in {"explicit", "unproven_source"}:
                            assert marker is None
                        else:
                            assert marker == {"kind": "fixed_unlimited_budget_upgrade", "pending_decision_id": gate.pending_decision_id,
                                              "round_id": fixture["round_id"], "run_id": original.run_id, "task_id": original.task_id}
                            assert request.revision == wait["revision"] + 1
                        if variant == "no_human_resume":
                            (await session.get(DesktopRun, resumed.run_id)).settled_at = None
                        if variant.startswith("tampered_"):
                            field = {"tampered_gate": "pending_decision_id", "tampered_round": "round_id", "tampered_run": "run_id"}[variant]
                            request.scope = {**request.scope, "legacy_access_source": {**marker, field: uuid.uuid4().hex}}
                    if variant == "still_pending":
                        class Checkpointer:
                            def __init__(self, calls):
                                self.calls = calls

                            async def aget_tuple(self, config):
                                self.calls.append(deepcopy(config))
                                return SimpleNamespace(config=deepcopy(config), checkpoint={"id": "pending"},
                                    pending_writes=[("task", "__interrupt__", {"type": "access_review", "tool": "browser_run_code_unsafe"})])
                        service._user_gate_recovery = UserAccessGateRecovery(Checkpointer(asked))
                    current_wait = await service.active_wait_request(fixture["loop_id"])
                    body = LoopWaitResponseRequest(request_revision=current_wait["revision"], idempotency_key=uuid.uuid4().hex,
                                                   answer={"action": "retry"})
                    if variant not in {"explicit", "unproven_source"}:
                        with pytest.raises(HTTPException) as stale:
                            await service.resolve_wait_request(fixture["loop_id"], wait["request_id"], old_body, actor_id="user")
                        assert stale.value.status_code == 409
                    if variant == "delayed_settlement":
                        with pytest.raises(HTTPException) as unsettled:
                            await service.resolve_wait_request(fixture["loop_id"], wait["request_id"], body, actor_id="user")
                        assert unsettled.value.status_code == 409
                        assert unsettled.value.detail == {"code": "user_gate_not_resolved", "message": "人工访问审批原执行未稳定中断"}
                        async with sessions.begin() as session:
                            request = await session.get(LoopWaitRequest, wait["request_id"])
                            assert request.status == "open"
                            source = await session.get(DesktopRun, original.run_id, with_for_update=True)
                            await RunLifecycleFinalizer(sessions, None)._settle_interrupted(session, source, None)
                            assert source.settled_at is not None
                    succeeds = variant in {"valid", "explicit", "delayed_settlement"}
                    if succeeds:
                        result = await service.resolve_wait_request(fixture["loop_id"], wait["request_id"], body, actor_id="user")
                        assert result["loop_status"] == "running"
                        assert len(asked) == 1
                    else:
                        with pytest.raises(HTTPException) as refused:
                            await service.resolve_wait_request(fixture["loop_id"], wait["request_id"], body, actor_id="user")
                        assert refused.value.status_code == 409
                        assert refused.value.detail["code"] == "user_gate_not_resolved"
                    async with sessions() as session:
                        if variant != "explicit":
                            directive = await session.scalar(select(LoopDirective).where(LoopDirective.loop_id == fixture["loop_id"]))
                            assert (directive.status, directive.lifecycle_state, directive.queued_reason) == (
                                "cancelled", "cancelled", "fixed_unlimited_budget_upgrade")
                        assert (await session.get(AgentLoop, fixture["loop_id"])).status == ("running" if succeeds else "waiting_user")
                        assert (await session.get(LoopWaitRequest, wait["request_id"])).status == ("resolved" if succeeds else "open")
                        stored_gate = await session.get(LoopPendingDecision, gate.pending_decision_id)
                        assert stored_gate.status == ("resolved" if succeeds else "pending")
                        assert stored_gate.payload == gate.payload and not stored_gate.delegable
                finally:
                    await _stop(service, fixture["loop_id"])
        finally:
            await engine.dispose()
    asyncio.run(run())
