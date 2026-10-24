"""本文件对外提供 Shell 退出核验与 PostgreSQL 恢复结算的合同测试。

输入为真实账本、重启恢复链路、匹配或缺失回执与可控启动身份；输出为核验、拒绝和幂等结算断言。
具体工作流为登记 Shell、执行重启协调、请求用户恢复，再检查 Run、派发、租约和 outbox 的事务一致性。
示例：python -m pytest backend/tests/test_shell_recovery.py -q。
"""

import asyncio
import json
import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.runs.execution_attempts.shell_recovery import ShellCleanupRecovery
from backend.app.desktop.runs.execution_attempts.tool import ToolExecutionLedger
from backend.app.desktop.persistence.models import DesktopRun, ToolExecutionAttempt
from backend.app.desktop.runs.orchestration.lifecycle import RunLifecycleFinalizer
from backend.app.desktop.runs.orchestration.dispatch import RunDispatchRecovery
from backend.app.desktop.runs.orchestration.models import RunDispatch, RunOutboxEvent
from backend.app.desktop.workspaces.coordination.leases import WorkspaceLeaseManager
from backend.app.desktop.workspaces.coordination.models import WorkspaceLease
from backend.app.desktop.workspaces.coordination.schemas import WorkspaceLeaseRequest
from backend.tests.test_shell_cleanup_settlement import _Checkpointer, _execution
from backend.tests.test_execution_attempt_audit import _seed
from focus.runtime.runs.manager import RunManager
from focus.runtime.tool_attempts import ToolExecutionUncertain
from focus.sandbox.recovery import ShellRecoveryEvidence

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")
BOOT = {"machine": "test-machine", "boot_id": "first-kernel-boot"}
NEXT_BOOT = {**BOOT, "boot_id": "next-kernel-boot"}


async def _blocked(sessions, tmp_path):
    run_id, slot_id, lease, record, _ = await _execution(sessions, tmp_path)
    evidence = ShellRecoveryEvidence(tmp_path / "receipts", boot_provider=lambda: BOOT)
    ledger = ToolExecutionLedger(sessions, shell_evidence=evidence)
    await ledger.claim(run_id + "-shell", run_id, {"id": "call", "name": "powershell", "args": {}}, requires_process_cleanup=True)
    await RunDispatchRecovery(sessions).reconcile()
    await RunLifecycleFinalizer(sessions, _Checkpointer()).reconcile_active("restart")
    receipt = await ledger.shell_receipt(run_id + "-shell")
    return run_id, slot_id, lease, record, evidence, ledger, receipt


def _write(evidence, receipt, **fields):
    evidence.path(receipt["execution"]).write_text(json.dumps({"phase": "exited", "jobDrained": True,
        "execution": receipt["execution"], **fields}), encoding="utf-8")


async def _assert_pending(sessions, run_id, lease):
    async with sessions() as session:
        run = await session.get(DesktopRun, run_id)
        assert run.settled_at is None and run.workspace_result["shell_cleanup"]
        assert (await session.get(WorkspaceLease, lease.lease_id)).status == "active"
        assert not list(await session.scalars(select(RunOutboxEvent).where(RunOutboxEvent.run_id == run_id)))


@pytest.mark.parametrize("proof", ["receipt", "reboot"])
def test_verified_recovery_settles_once_and_releases_directory(tmp_path, proof):
    async def exercise():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            run_id, slot_id, lease, _, evidence, ledger, receipt = await _blocked(sessions, tmp_path)
            if proof == "receipt":
                _write(evidence, receipt)
            finalizer = RunLifecycleFinalizer(sessions, _Checkpointer())
            recovery = ShellCleanupRecovery(sessions, finalizer, RunManager(), evidence=evidence,
                boot_provider=lambda: NEXT_BOOT if proof == "reboot" else BOOT)
            listed = await recovery.blocked()
            item = next(item for item in listed if item["run_id"] == run_id)
            assert item["directory"] == str(tmp_path) and item["attempts"][0]["call_id"] == "call"
            first = await recovery.recover(run_id)
            second = await recovery.recover(run_id)
            assert first.status == "interrupted" and not first.idempotent
            assert second.idempotent and first.event_id == second.event_id
            async with sessions() as session:
                run = await session.get(DesktopRun, run_id)
                assert run.settled_at is not None and not run.workspace_result["shell_cleanup"]
                assert run.workspace_result["shell_cleanup_recovery"]["attempts"][run_id + "-shell"]["kind"] == ("runner_receipt" if proof == "receipt" else "windows_reboot")
                assert run.workspace_result["reconciliation"]["retry_safe"] is False
                assert (await session.get(WorkspaceLease, lease.lease_id)).status == "released"
                assert (await session.scalar(select(RunDispatch).where(RunDispatch.run_id == run_id))).settled_at is not None
                assert len(list(await session.scalars(select(RunOutboxEvent).where(RunOutboxEvent.run_id == run_id)))) == 1
                assert (await session.get(ToolExecutionAttempt, run_id + "-shell")).status == "claimed"
            with pytest.raises(ToolExecutionUncertain):
                await ledger.claim(run_id + "-shell", run_id, {"id": "call", "name": "powershell", "args": {}})
            next_run, _ = await _seed(sessions, tmp_path / "next-workspace")
            await WorkspaceLeaseManager(sessions).acquire(WorkspaceLeaseRequest(slot_id=slot_id, run_id=next_run, mode="write"))
            await finalizer.reconcile_active("end test execution")
            assert not any(item["run_id"] == run_id for item in await recovery.blocked())
        finally:
            await engine.dispose()
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["missing", "corrupt", "wrong_identity", "not_drained", "different_machine", "same_boot"])
def test_unverified_recovery_preserves_barrier(tmp_path, failure):
    async def exercise():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            run_id, _, lease, _, evidence, _, receipt = await _blocked(sessions, tmp_path)
            if failure == "corrupt":
                evidence.path(receipt["execution"]).write_text("{broken", encoding="utf-8")
            elif failure == "wrong_identity":
                _write(evidence, receipt, execution={**receipt["execution"], "attempt_id": "other"})
            elif failure == "not_drained":
                _write(evidence, receipt, jobDrained=False)
            boot = {**NEXT_BOOT, "machine": "other"} if failure == "different_machine" else BOOT
            recovery = ShellCleanupRecovery(sessions, RunLifecycleFinalizer(sessions, _Checkpointer()), RunManager(),
                evidence=evidence, boot_provider=lambda: boot)
            with pytest.raises(ValueError, match="退出证据"):
                await recovery.recover(run_id)
            await _assert_pending(sessions, run_id, lease)
        finally:
            await engine.dispose()
    asyncio.run(exercise())


def test_all_attempts_required_and_transaction_failure_is_retryable(tmp_path, monkeypatch):
    async def exercise():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            run_id, _, lease, _, evidence, ledger, receipt = await _blocked(sessions, tmp_path)
            # Two outstanding attempts, as produced by parallel tool calls before interruption.
            async with sessions.begin() as session:
                run = await session.get(DesktopRun, run_id)
                extra = evidence.prepare(run_id, "second-attempt", "second-call")
                run.workspace_result = {**run.workspace_result, "shell_cleanup": {
                    **run.workspace_result["shell_cleanup"], "second-attempt": {
                        "call_id": "second-call", "tool": "powershell", "state": "unknown", "execution": extra}}}
            _write(evidence, receipt)
            finalizer = RunLifecycleFinalizer(sessions, _Checkpointer())
            recovery = ShellCleanupRecovery(sessions, finalizer, RunManager(), evidence=evidence, boot_provider=lambda: BOOT)
            with pytest.raises(ValueError):
                await recovery.recover(run_id)
            await _assert_pending(sessions, run_id, lease)
            _write(evidence, {"execution": extra})
            original = finalizer._outbox.enqueue_settled
            async def fail(*args, **kwargs):
                raise RuntimeError("commit failed")
            monkeypatch.setattr(finalizer._outbox, "enqueue_settled", fail)
            with pytest.raises(RuntimeError, match="commit failed"):
                await recovery.recover(run_id)
            await _assert_pending(sessions, run_id, lease)
            monkeypatch.setattr(finalizer._outbox, "enqueue_settled", original)
            assert (await recovery.recover(run_id)).status == "interrupted"
        finally:
            await engine.dispose()
    asyncio.run(exercise())


def test_legacy_run_can_enroll_reboot_evidence(tmp_path):
    async def exercise():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            run_id, _, lease, _, evidence, _, _ = await _blocked(sessions, tmp_path)
            async with sessions.begin() as session:
                run = await session.get(DesktopRun, run_id)
                run.workspace_result = {"shell_cleanup": {"legacy": {"call_id": "lost", "tool": "powershell", "state": "pending"}}}
            recovery = ShellCleanupRecovery(sessions, RunLifecycleFinalizer(sessions, _Checkpointer()), RunManager(),
                evidence=evidence, boot_provider=lambda: BOOT)
            with pytest.raises(ValueError, match="重启 Windows"):
                await recovery.recover(run_id)
            await _assert_pending(sessions, run_id, lease)
            recovery._boot = lambda: NEXT_BOOT
            assert (await recovery.recover(run_id)).status == "interrupted"
        finally:
            await engine.dispose()
    asyncio.run(exercise())


def test_recovery_api_requires_session_and_server_evidence(tmp_path):
    async def exercise():
        import httpx
        from fastapi import FastAPI
        from types import SimpleNamespace
        from backend.app.desktop.api.routes import desktop_router
        from backend.app.gateway.middleware.auth import AuthMiddleware

        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            run_id, _, lease, _, evidence, _, receipt = await _blocked(sessions, tmp_path)
            service = ShellCleanupRecovery(sessions, RunLifecycleFinalizer(sessions, _Checkpointer()), RunManager(),
                evidence=evidence, boot_provider=lambda: BOOT)
            app = FastAPI()
            app.include_router(desktop_router)
            app.add_middleware(AuthMiddleware)
            app.state.session_key = "test-session"
            app.state.desktop_service = SimpleNamespace(shell_recovery=service)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app, client=("127.0.0.1", 12345)), base_url="http://test") as client:
                assert (await client.get("/desktop/api/shell-recovery")).status_code == 401
                headers = {"X-Focus-Session": "test-session"}
                listed = await client.get("/desktop/api/shell-recovery", headers=headers)
                assert any(item["run_id"] == run_id for item in listed.json())
                endpoint = f"/desktop/api/runs/{run_id}/shell-recovery"
                refused = await client.post(endpoint, headers=headers, json={"process_cleanup": "confirmed", "jobDrained": True})
                assert refused.status_code == 409
                await _assert_pending(sessions, run_id, lease)
                _write(evidence, receipt)
                assert (await client.post(endpoint, headers=headers)).status_code == 200
                assert (await client.post(endpoint, headers=headers)).json()["idempotent"]
                assert (await client.post("/desktop/api/runs/missing/shell-recovery", headers=headers)).status_code == 404
        finally:
            await engine.dispose()
    asyncio.run(exercise())


def test_live_executor_blocks_recovery_then_concurrent_retries_settle_once(tmp_path):
    async def exercise():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            run_id, _, lease, record, evidence, _, receipt = await _blocked(sessions, tmp_path)
            from focus.runtime.runs.schemas import RunStatus
            manager = RunManager()
            manager.create(record.thread_id, run_id=run_id)
            recovery = ShellCleanupRecovery(sessions, RunLifecycleFinalizer(sessions, _Checkpointer()), manager,
                evidence=evidence, boot_provider=lambda: BOOT)
            _write(evidence, receipt)
            with pytest.raises(ValueError, match="仍在运行"):
                await recovery.recover(run_id)
            await _assert_pending(sessions, run_id, lease)
            manager.update(run_id, status=RunStatus.interrupted)
            results = await asyncio.gather(recovery.recover(run_id), recovery.recover(run_id))
            assert sum(not result.idempotent for result in results) == 1
            assert results[0].event_id == results[1].event_id
        finally:
            await engine.dispose()
    asyncio.run(exercise())


def test_recovery_lists_persisted_directory_without_slot(tmp_path):
    async def exercise():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            run_id, _, _, _, evidence, _, _ = await _blocked(sessions, tmp_path)
            async with sessions.begin() as session:
                run = await session.get(DesktopRun, run_id)
                run.workspace_anchor = {"workspace_path": str(tmp_path)}
            recovery = ShellCleanupRecovery(sessions, RunLifecycleFinalizer(sessions, _Checkpointer()), RunManager(),
                evidence=evidence, boot_provider=lambda: BOOT)
            item = next(item for item in await recovery.blocked() if item["run_id"] == run_id)
            assert item["directory"] == str(tmp_path)
        finally:
            await engine.dispose()
    asyncio.run(exercise())
