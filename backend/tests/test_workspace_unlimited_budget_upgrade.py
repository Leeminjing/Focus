"""真实 PostgreSQL 验证无上限预算升级的授权、等待与消费历史边界。

输入为旧配额下运行、暂停、人工等待、终态和撤权记录；输出为新授权生效、旧工作
收敛、显式重试与幂等升级的断言。模型在途预留跨升级保留，并通过原始回执实际结算。
示例：pytest backend/tests/test_workspace_unlimited_budget_upgrade.py。
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence.registry  # noqa: F401
from backend.app.desktop.patrol.agent_loop.context_expansion.index_budget_repository import (
    IndexBudgetReservationRepository,
)
from backend.app.desktop.patrol.agent_loop.context_expansion.models import (
    LoopIndexBudgetReservation,
)
from backend.app.desktop.patrol.agent_loop.journal.journal_models import (
    LoopJournalEvent,
)
from backend.app.desktop.patrol.agent_loop.lifecycle.unlimited_budget_upgrade import (
    upgrade_workspace_loop_budgets,
)
from backend.app.desktop.patrol.agent_loop.models import (
    AgentLoop,
    LoopBudgetUsage,
    LoopDelegationGrant,
    LoopRound,
    LoopWorkerRequest,
)
from backend.app.desktop.patrol.agent_loop.resources.unlimited_budget import (
    unlimited_loop_budgets,
)
from backend.app.desktop.patrol.agent_loop.resources.usage import LoopUsageDelta
from backend.app.desktop.patrol.agent_loop.schemas import LoopWaitResponseRequest
from backend.app.desktop.patrol.agent_loop.service import AgentLoopService
from backend.app.desktop.patrol.agent_loop.waits.wait_models import (
    LoopWaitRequest,
    LoopWaitResponse,
)
from backend.app.desktop.persistence.models import (
    DesktopRun,
    DesktopThread,
    DesktopWorkspace,
)

_OLD_BUDGETS = {"max_rounds": 40, "max_duration_seconds": 1800, "max_model_calls": 100,
                "max_input_tokens": 500000, "max_output_tokens": 50000, "max_retries": 6,
                "max_concurrent_runs": 2, "max_no_progress": 3}


async def _seed(sessions, tmp_path, *, status="running", wait_kind=None, grant_status="active",
                mode="workspace_patrol", expired=False):
    identity = {key: uuid.uuid4().hex for key in ("workspace", "context", "loop", "round", "grant", "run", "wait", "receipt")}
    workspace_path = tmp_path / identity["workspace"]
    workspace_path.mkdir()
    async with sessions.begin() as session:
        session.add(DesktopWorkspace(workspace_id=identity["workspace"], path=str(workspace_path), display_name="budget upgrade"))
        await session.flush()
        session.add(DesktopThread(task_id=identity["context"], workspace_id=identity["workspace"],
                                  thread_id=uuid.uuid4().hex, title="budget upgrade"))
        await session.flush()
        loop = AgentLoop(loop_id=identity["loop"], workspace_id=identity["workspace"],
                         initial_context_id=identity["context"], holder_id="patrol", interaction_mode=mode,
                         status=status, health="idle" if status != "running" else "waiting_runs",
                         equipment={"permissions": ["read"], "access_mode": "read-only"},
                         waiting_reason="needs user" if status == "waiting_user" else None)
        session.add(loop)
        await session.flush()
        session.add_all([
            LoopDelegationGrant(grant_id=identity["grant"], loop_id=identity["loop"], revision=1,
                                holder_id="patrol", capabilities=["continue_context"],
                                context_scope=[identity["context"]], permission_scope=["read"],
                                budgets=dict(_OLD_BUDGETS), status=grant_status,
                                expires_at=datetime.now(UTC) - timedelta(hours=1) if expired else None),
            LoopRound(round_id=identity["round"], loop_id=identity["loop"], number=1,
                      authority_revision=1, goal_revision=1, frontier_hash="a" * 64, workspace_revision=1,
                      status="error" if wait_kind else "observed"),
            LoopBudgetUsage(loop_id=identity["loop"], rounds=39, model_calls=97,
                            input_tokens=499000, output_tokens=49000, retries=5),
            LoopIndexBudgetReservation(reservation_id=identity["receipt"], loop_id=identity["loop"],
                                       grant_revision=1, model_calls=1, input_tokens=500, output_tokens=400,
                                       actual_usage={"receipt_state": "unknown"}),
        ])
        await session.flush()
        loop.current_round_id = identity["round"]
        if status == "running":
            session.add(DesktopRun(run_id=identity["run"], task_id=identity["context"], agent_id="main",
                                   kind="main", status="running", loop_id=identity["loop"], round_id=identity["round"]))
        if wait_kind:
            session.add(LoopWaitRequest(request_id=identity["wait"], loop_id=identity["loop"], round_id=identity["round"],
                                        kind=wait_kind, prompt="needs user", response_mode="action",
                                        response_contract={"actions": [{"action": "revise_budget", "label": "调整预算"},
                                                                       {"action": "stop", "label": "停止"}]},
                                        scope={"kind": "access_approval"} if wait_kind == "recovery_action" else {},
                                        correlation_id=identity["wait"], created_by="test"))
    return identity


def test_upgrade_preserves_control_history_and_settles_inflight_usage(runtime_postgres_database, tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            running = await _seed(sessions, tmp_path)
            paused = await _seed(sessions, tmp_path, status="paused")
            budget_wait = await _seed(sessions, tmp_path, status="waiting_user", wait_kind="budget_action")
            permission_wait = await _seed(sessions, tmp_path, status="waiting_user", wait_kind="recovery_action")
            stopped = await _seed(sessions, tmp_path, status="stopped")
            revoked = await _seed(sessions, tmp_path, status="waiting_user", grant_status="revoked")
            legacy = await _seed(sessions, tmp_path, mode="context_loop")
            expected = {row["loop"] for row in (running, paused, budget_wait, permission_wait)}
            assert set(await upgrade_workspace_loop_budgets(sessions)) == expected
            async with sessions() as session:
                for identity in (running, paused, budget_wait, permission_wait):
                    loop = await session.get(AgentLoop, identity["loop"])
                    old = await session.get(LoopDelegationGrant, identity["grant"])
                    active = await session.scalar(select(LoopDelegationGrant).where(
                        LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.status == "active"))
                    assert (old.status, old.budgets) == ("revoked", _OLD_BUDGETS)
                    assert (loop.authority_revision, active.revision) == (2, 2)
                    assert active.budgets == unlimited_loop_budgets()
                    assert (active.capabilities, active.context_scope, active.permission_scope, active.expires_at) == (
                        old.capabilities, old.context_scope, old.permission_scope, old.expires_at)
                    usage = await session.get(LoopBudgetUsage, loop.loop_id)
                    assert (usage.rounds, usage.model_calls, usage.input_tokens, usage.output_tokens, usage.retries) == (
                        39, 97, 499000, 49000, 5)
                    receipt = await session.get(LoopIndexBudgetReservation, identity["receipt"])
                    assert (receipt.model_calls, receipt.input_tokens, receipt.output_tokens, receipt.settled_at,
                            receipt.actual_usage) == (1, 500, 400, None, {"receipt_state": "unknown"})
                active_loop = await session.get(AgentLoop, running["loop"])
                active_round = await session.get(LoopRound, active_loop.current_round_id)
                assert (active_loop.status, active_round.authority_revision, active_round.number) == ("running", 2, 2)
                assert (await session.get(LoopRound, running["round"])).status == "superseded"
                old_run = await session.get(DesktopRun, running["run"])
                assert (old_run.status, old_run.error, old_run.settled_at) == ("interrupted", "fixed_unlimited_budget_upgrade", None)
                assert (await session.get(AgentLoop, paused["loop"])).status == "paused"
                for identity in (budget_wait, permission_wait):
                    loop = await session.get(AgentLoop, identity["loop"])
                    assert (loop.status, loop.current_round_id) == ("waiting_user", identity["round"])
                wait = await session.get(LoopWaitRequest, budget_wait["wait"])
                assert (wait.status, wait.revision) == ("open", 2)
                assert [action["action"] for action in wait.response_contract["actions"]] == ["retry", "stop"]
                permission = await session.get(LoopWaitRequest, permission_wait["wait"])
                assert (permission.prompt, permission.status, permission.revision, permission.scope) == (
                    "needs user", "open", 1, {"kind": "access_approval"})
                for identity, status in ((stopped, "stopped"), (revoked, "waiting_user"), (legacy, "running")):
                    loop = await session.get(AgentLoop, identity["loop"])
                    grant = await session.get(LoopDelegationGrant, identity["grant"])
                    assert (loop.status, loop.authority_revision, grant.budgets) == (status, 1, _OLD_BUDGETS)
                event_count = await session.scalar(select(func.count()).select_from(LoopJournalEvent))
            assert await upgrade_workspace_loop_budgets(sessions) == ()
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(LoopJournalEvent)) == event_count
                receipt = await session.get(LoopIndexBudgetReservation, running["receipt"])
                owner = IndexBudgetReservationRepository.from_receipt(sessions, receipt)
            await owner.settle(LoopUsageDelta(model_calls=1, input_tokens=420, output_tokens=180))
            await owner.settle(LoopUsageDelta(model_calls=1, input_tokens=420, output_tokens=180))
            async with sessions() as session:
                usage = await session.get(LoopBudgetUsage, running["loop"])
                assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (98, 499420, 49180)
                assert (await session.get(LoopIndexBudgetReservation, running["receipt"])).settled_at is not None
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_upgrade_replaces_obsolete_execution_retries_without_restarting_workers(runtime_postgres_database, tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            for variant in ("worker", "already_upgraded", "expired"):
                identity = await _seed(sessions, tmp_path, status="waiting_user", wait_kind="recovery_action",
                                       expired=variant == "expired")
                worker_id = uuid.uuid4().hex
                old_scope = {"worker_request_id": worker_id, "worker_kind": "completion_verifier"}
                async with sessions.begin() as session:
                    wait = await session.get(LoopWaitRequest, identity["wait"])
                    wait.scope = old_scope
                    wait.response_contract = {"actions": [{"action": "retry", "label": "重试"},
                                                           {"action": "stop", "label": "停止"}]}
                    round_row = await session.get(LoopRound, identity["round"])
                    round_row.status = "waiting_workers"
                    session.add(LoopWorkerRequest(worker_request_id=worker_id, loop_id=identity["loop"],
                                                 round_id=identity["round"], kind="completion_verifier", status="error",
                                                 scope={"execution_authority_revision": 1}, attempt=3, max_attempts=3,
                                                 result={"error": "old failure"}))
                    if variant == "already_upgraded":
                        loop = await session.get(AgentLoop, identity["loop"])
                        grant = await session.get(LoopDelegationGrant, identity["grant"])
                        loop.authority_revision = grant.revision = 2
                        grant.budgets = unlimited_loop_budgets()
                        round_row.status = "superseded"
                assert identity["loop"] in await upgrade_workspace_loop_budgets(sessions)
                async with sessions() as session:
                    loop = await session.get(AgentLoop, identity["loop"])
                    old = await session.get(LoopWaitRequest, identity["wait"])
                    replacement = await session.scalar(select(LoopWaitRequest).where(
                        LoopWaitRequest.loop_id == loop.loop_id, LoopWaitRequest.status == "open"))
                    assert loop.status == "waiting_user"
                    assert (old.status, old.scope) == ("superseded", old_scope)
                    assert replacement.scope == {"component": "authority_upgrade", "superseded_request_id": old.request_id}
                    assert replacement.kind == "recovery_action"
                    replacement_id, replacement_revision = replacement.request_id, replacement.revision
                assert await upgrade_workspace_loop_budgets(sessions) == ()
                service = AgentLoopService(sessions)
                with pytest.raises(HTTPException) as blocked:
                    await service.control(identity["loop"], "resume")
                assert blocked.value.status_code == 409
                with pytest.raises(HTTPException) as obsolete:
                    await service.resolve_wait_request(identity["loop"], identity["wait"], LoopWaitResponseRequest(
                        request_revision=1, idempotency_key=uuid.uuid4().hex, answer={"action": "retry"}), actor_id="user")
                assert obsolete.value.status_code == 409
                retry = LoopWaitResponseRequest(request_revision=replacement_revision,
                                               idempotency_key=uuid.uuid4().hex, answer={"action": "retry"})
                if variant == "expired":
                    with pytest.raises(HTTPException) as denied:
                        await service.resolve_wait_request(identity["loop"], replacement_id, retry, actor_id="user")
                    assert denied.value.status_code == 409
                else:
                    result = await service.resolve_wait_request(identity["loop"], replacement_id, retry, actor_id="user")
                    assert result["loop_status"] == "running"
                async with sessions() as session:
                    loop = await session.get(AgentLoop, identity["loop"])
                    worker = await session.get(LoopWorkerRequest, worker_id)
                    assert (worker.status, worker.attempt, worker.max_attempts, worker.result) == (
                        "error", 3, 3, {"error": "old failure"})
                    usage = await session.get(LoopBudgetUsage, loop.loop_id)
                    assert usage.retries == 5
                    if variant == "expired":
                        assert (loop.status, loop.current_round_id) == ("waiting_user", identity["round"])
                    else:
                        new_round = await session.get(LoopRound, loop.current_round_id)
                        assert new_round.round_id != identity["round"]
                        assert (new_round.status, new_round.authority_revision) == ("observed", loop.authority_revision)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_upgrade_keeps_human_wait_contracts_even_with_execution_hints(runtime_postgres_database, tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            for human_scope in ({"kind": "access_approval"}, {"information_only": True, "cause": "missing_input"},
                                {"cause": "missing_goal", "evidence_identity": {"kind": "mission"}},
                                {"quality_recovery": {"authority_revision": 1}}):
                identity = await _seed(sessions, tmp_path, status="waiting_user", wait_kind="recovery_action")
                scope = {**human_scope, **({"worker_request_id": "old-worker"} if "quality_recovery" not in human_scope else {})}
                async with sessions.begin() as session:
                    wait = await session.get(LoopWaitRequest, identity["wait"])
                    wait.scope = scope
                await upgrade_workspace_loop_budgets(sessions)
                async with sessions() as session:
                    wait = await session.get(LoopWaitRequest, identity["wait"])
                    loop = await session.get(AgentLoop, identity["loop"])
                    assert (wait.status, wait.revision, wait.scope, wait.prompt) == ("open", 1, scope, "needs user")
                    assert loop.status == "waiting_user"
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("expired", [False, True])
def test_old_budget_wait_requires_current_explicit_retry(runtime_postgres_database, tmp_path, expired):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            identity = await _seed(sessions, tmp_path, status="waiting_user", wait_kind="budget_action", expired=expired)
            await upgrade_workspace_loop_budgets(sessions)
            service = AgentLoopService(sessions)
            with pytest.raises(HTTPException) as stale:
                await service.resolve_wait_request(identity["loop"], identity["wait"], LoopWaitResponseRequest(
                    request_revision=1, idempotency_key=uuid.uuid4().hex, answer={"action": "retry"}), actor_id="user")
            assert stale.value.status_code == 409
            request = LoopWaitResponseRequest(request_revision=2, idempotency_key=uuid.uuid4().hex, answer={"action": "retry"})
            if expired:
                with pytest.raises(HTTPException) as denied:
                    await service.resolve_wait_request(identity["loop"], identity["wait"], request, actor_id="user")
                assert denied.value.status_code == 409
            else:
                result = await service.resolve_wait_request(identity["loop"], identity["wait"], request, actor_id="user")
                assert result["loop_status"] == "running"
            async with sessions() as session:
                loop = await session.get(AgentLoop, identity["loop"])
                wait = await session.get(LoopWaitRequest, identity["wait"])
                responses = await session.scalar(select(func.count()).select_from(LoopWaitResponse))
                assert (loop.status, wait.status, responses) == (
                    ("waiting_user", "open", 0) if expired else ("running", "resolved", 1))
                assert (await session.get(LoopDelegationGrant, identity["grant"])).budgets == _OLD_BUDGETS
        finally:
            await engine.dispose()
    asyncio.run(run())
