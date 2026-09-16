r"""本文件验证 workspace 意图、fingerprint、单 Writer fencing、adoption 与中断恢复合同。

输入为服务端 equipment/effect、临时文件树和隔离 PostgreSQL slot/Run；输出为不可降级写意图、
稳定 fingerprint、并发 writer 拒绝、reader 共存、陈旧 token 拒绝和原子采用断言。具体工作流为
从持久 workspace 建权威/隔离 slot 并穿过公开服务。示例：`pytest test_workspace_coordination.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.workspace_coordination import (
    RunExecutionAnchor,
    WorkspaceAccessMode,
    WorkspaceAdopter,
    WorkspaceAdoption,
    WorkspaceAdoptionRequest,
    WorkspaceFingerprinter,
    WorkspaceIntentDeriver,
    WorkspaceLeaseConflict,
    WorkspaceLeaseManager,
    WorkspaceLeaseRequest,
    WorkspaceLease,
    WorkspaceRunReconciler,
    WorkspaceSlot,
)


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_server_intent_cannot_downgrade_write_and_fingerprint_is_stable(tmp_path: Path) -> None:
    intent = WorkspaceIntentDeriver.derive(
        {"permissions": ["read"]},
        [{"tool_name": "patch", "writes_workspace": True}],
    )
    assert intent.mode is WorkspaceAccessMode.WRITE
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.txt").write_text("alpha", encoding="utf-8")
    first = WorkspaceFingerprinter().capture(root)
    second = WorkspaceFingerprinter().capture(root)
    assert first == second
    (root / "a.txt").write_text("beta", encoding="utf-8")
    assert WorkspaceFingerprinter().capture(root).digest != first.digest


def test_single_writer_many_readers_fencing_adoption_and_recovery() -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        suffix = uuid.uuid4().hex[:8]
        workspace_id = f"ws-slot-{suffix}"
        context_id = f"context-slot-{suffix}"
        writer_ids = [uuid.uuid4().hex for _ in range(2)]
        reader_ids = [uuid.uuid4().hex for _ in range(2)]
        authoritative_id = uuid.uuid4().hex
        isolated_id = uuid.uuid4().hex
        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=f"/tmp/{workspace_id}", display_name="slots"))
                await session.flush()
                session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{suffix}", title="slots"))
                await session.flush()
                for run_id in writer_ids + reader_ids:
                    session.add(DesktopRun(run_id=run_id, task_id=context_id, agent_id=f"agent-{run_id}", kind="main", status="success", origin="loop", execution_thread_id=f"execution-{run_id}"))
                await session.flush()
                session.add_all([
                    WorkspaceSlot(slot_id=authoritative_id, workspace_id=workspace_id, kind="authoritative", root_path=f"/tmp/{workspace_id}", current_fingerprint="a" * 64),
                    WorkspaceSlot(slot_id=isolated_id, workspace_id=workspace_id, kind="isolated", root_path=f"/tmp/{workspace_id}-isolated", current_fingerprint="b" * 64, owner_loop_id="loop-1", owner_lane_id="lane-1"),
                ])
            manager = WorkspaceLeaseManager(sessions)
            first = await manager.acquire(WorkspaceLeaseRequest(slot_id=authoritative_id, run_id=writer_ids[0], mode="write"))
            with pytest.raises(WorkspaceLeaseConflict):
                await manager.acquire(WorkspaceLeaseRequest(slot_id=authoritative_id, run_id=writer_ids[1], mode="write"))
            await manager.release(first.lease_id, first.fencing_token)
            readers = await asyncio.gather(*[
                manager.acquire(WorkspaceLeaseRequest(slot_id=authoritative_id, run_id=run_id, mode="read"))
                for run_id in reader_ids
            ])
            assert len(readers) == 2
            with pytest.raises(WorkspaceLeaseConflict):
                await manager.assert_valid(first.lease_id, first.fencing_token)
            for reader in readers:
                await manager.release(reader.lease_id, reader.fencing_token)
            expiring = await manager.acquire(
                WorkspaceLeaseRequest(
                    slot_id=authoritative_id,
                    run_id=writer_ids[1],
                    mode="write",
                )
            )
            async with sessions.begin() as session:
                lease = await session.get(WorkspaceLease, expiring.lease_id, with_for_update=True)
                lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            with pytest.raises(WorkspaceLeaseConflict):
                await manager.renew(expiring.lease_id, expiring.fencing_token)
            async with sessions() as session:
                expired = await session.scalar(
                    select(WorkspaceLease).where(WorkspaceLease.lease_id == expiring.lease_id)
                )
                assert expired.status == "expired"
            adopter = WorkspaceAdopter(sessions)

            async def apply_result(source, target):
                assert source.slot_id == isolated_id
                return "c" * 64

            adopted = await adopter.adopt(
                WorkspaceAdoptionRequest(adoption_id=uuid.uuid4().hex, source_slot_id=isolated_id, target_slot_id=authoritative_id, source_revision=1, expected_target_revision=1),
                apply_result,
            )
            assert adopted.status == "adopted"
            anchor = RunExecutionAnchor(run_id=writer_ids[0], slot_id=isolated_id, observed_workspace_revision=1, observed_fingerprint="b" * 64, effect_evidence=[{"tool": "write"}])
            result = WorkspaceRunReconciler.classify(anchor, "write", "c" * 64)
            assert result["retryable"] is False
            assert result["observation"]["changed"] is True
        finally:
            async with sessions.begin() as session:
                await session.execute(
                    delete(WorkspaceAdoption).where(
                        WorkspaceAdoption.source_slot_id == isolated_id
                    )
                )
                await session.execute(delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace_id))
            await engine.dispose()

    asyncio.run(run())
