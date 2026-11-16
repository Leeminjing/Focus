r"""本文件对外提供旧 Context Loop 退役与 Workspace Patrol 默认的回归测试。

输入为隔离 PostgreSQL 中的旧活动 Loop、终态历史及 Workspace Patrol；输出为旧授权和执行
原子收敛、策展占用释放、历史与工作区模式不变的断言。具体工作流为播种真实领域记录，
执行启动前退役，再检查幂等重放；迁移在独立测试库验证列默认的往返。
示例：`pytest backend/tests/test_context_loop_retirement.py`。
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence.registry  # noqa: F401
from backend.app.desktop.contexts.curation.models import CurationLane, CurationProgram
from backend.app.desktop.patrol.agent_loop.governance.authority_control import (
    LoopAuthorityService,
)
from backend.app.desktop.patrol.agent_loop.governance.intervention_lifecycle import (
    InterventionLifecycleRepository,
)
from backend.app.desktop.patrol.agent_loop.journal.journal_models import (
    LoopJournalEvent,
)
from backend.app.desktop.patrol.agent_loop.lifecycle.retirement import (
    retire_context_loops,
)
from backend.app.desktop.patrol.agent_loop.models import (
    AgentLoop,
    LoopDelegationGrant,
    LoopRound,
    LoopUserIntent,
    LoopWorkerRequest,
)
from backend.app.desktop.patrol.agent_loop.schemas import RevokeLoopGrantRequest
from backend.app.desktop.patrol.agent_loop.workers.worker_attempts import (
    WorkerAttemptAuthority,
    WorkerAttemptRejected,
)
from backend.app.desktop.persistence.models import (
    DesktopRun,
    DesktopThread,
    DesktopWorkspace,
)


def test_startup_retires_legacy_work_without_resuming_or_changing_workspace_patrol(
    runtime_postgres_database, tmp_path,
):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        legacy_id, workspace_loop_id, historical_id = (uuid.uuid4().hex for _ in range(3))
        grant_id, round_id, run_id, lane_id, intent_id = (uuid.uuid4().hex for _ in range(5))
        worker_id = uuid.uuid4().hex
        try:
            async with sessions.begin() as session:
                workspace_id = uuid.uuid4().hex
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="retirement"))
                await session.flush()
                contexts = {}
                for loop_id in (legacy_id, workspace_loop_id, historical_id):
                    context = DesktopThread(task_id=uuid.uuid4().hex, workspace_id=workspace_id,
                        thread_id=uuid.uuid4().hex, title="retirement")
                    session.add(context)
                    contexts[loop_id] = context
                program = CurationProgram(program_id=uuid.uuid4().hex, workspace_id=workspace_id)
                session.add(program)
                await session.flush()
                legacy = AgentLoop(loop_id=legacy_id, interaction_mode="context_loop", workspace_id=workspace_id,
                    initial_context_id=contexts[legacy_id].task_id, program_id=program.program_id,
                    holder_id="retired-patrol", status="running", health="waiting_runs")
                workspace = AgentLoop(loop_id=workspace_loop_id, workspace_id=workspace_id,
                    initial_context_id=contexts[workspace_loop_id].task_id, holder_id="workspace-patrol",
                    status="running", health="observing")
                historical = AgentLoop(loop_id=historical_id, interaction_mode="context_loop", workspace_id=workspace_id,
                    initial_context_id=contexts[historical_id].task_id, holder_id="old-patrol", status="completed",
                    final_result={"summary": "historical result"}, completed_at=datetime.now(UTC))
                session.add_all([legacy, workspace, historical])
                await session.flush()
                session.add_all([
                    LoopDelegationGrant(grant_id=grant_id, loop_id=legacy_id, revision=1, holder_id="retired-patrol",
                        capabilities=["continue_context"], context_scope=[legacy.initial_context_id],
                        permission_scope=["read"], budgets={}),
                    CurationLane(lane_id=lane_id, program_id=program.program_id,
                        managed_context_id=legacy.initial_context_id, purpose="legacy", normalized_purpose="legacy"),
                    LoopRound(round_id=round_id, loop_id=legacy_id, number=1, status="observed",
                        authority_revision=1, goal_revision=1, frontier_hash="a" * 64, workspace_revision=1),
                    DesktopRun(run_id=run_id, task_id=legacy.initial_context_id, agent_id="legacy-agent", kind="main",
                        status="running", origin="delegated_patrol", execution_thread_id=contexts[legacy_id].thread_id,
                        loop_id=legacy_id, round_id=round_id),
                ])
                legacy.current_round_id = round_id
                intent = LoopUserIntent(intent_id=intent_id, loop_id=legacy_id, scope="context",
                    target_context_id=legacy.initial_context_id, content="old pending message",
                    intent_kind="direct_message", correlation_id=intent_id, goal_revision=1, authority_revision=1)
                session.add(intent)
                await InterventionLifecycleRepository().register(session, intent)
                await InterventionLifecycleRepository().transition(session, intent_id, "accepted")
                session.add(LoopWorkerRequest(worker_request_id=worker_id, loop_id=legacy_id, round_id=round_id,
                    kind="completion_verifier", scope={}))
            async with sessions() as session:
                legacy = await session.get(AgentLoop, legacy_id)
                worker = await session.get(LoopWorkerRequest, worker_id)
                with pytest.raises(WorkerAttemptRejected, match="已退役"):
                    await WorkerAttemptAuthority().require_authorized(session, legacy, worker)
            with pytest.raises(HTTPException) as rejected:
                await LoopAuthorityService(sessions).mutate(legacy_id, RevokeLoopGrantRequest(command="revoke"))
            assert rejected.value.status_code == 409
            assert "已退役" in rejected.value.detail
            assert await retire_context_loops(sessions) == (legacy_id,)
            async with sessions() as session:
                legacy = await session.get(AgentLoop, legacy_id)
                assert (legacy.status, legacy.revision, legacy.authority_revision) == ("stopped", 2, 2)
                assert legacy.completed_at is not None
                assert (await session.get(LoopDelegationGrant, grant_id)).status == "revoked"
                assert (await session.get(CurationLane, lane_id)).lifecycle == "retired"
                assert (await session.get(LoopRound, round_id)).status == "superseded"
                assert (await session.get(LoopWorkerRequest, worker_id)).status == "cancelled"
                stopped_run = await session.get(DesktopRun, run_id)
                assert (stopped_run.status, stopped_run.error) == ("interrupted", "context_loop_retired")
                assert stopped_run.settled_at is None  # Actual finalization still owns settlement and lease release.
                intent = await session.get(LoopUserIntent, intent_id)
                assert (intent.delivery_state, intent.status, intent.terminal_reason) == (
                    "cancelled", "superseded", "context_loop_retired",
                )
                workspace = await session.get(AgentLoop, workspace_loop_id)
                assert (workspace.interaction_mode, workspace.status, workspace.authority_revision) == ("workspace_patrol", "running", 1)
                historical = await session.get(AgentLoop, historical_id)
                assert historical.status == "completed"
                assert historical.final_result == {"summary": "historical result"}
                events = await session.scalar(select(func.count()).select_from(LoopJournalEvent))
            assert await retire_context_loops(sessions) == ()
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(LoopJournalEvent)) == events
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_workspace_patrol_database_default_migration_round_trip(isolated_postgres_database):
    config = Config(str(Path(__file__).parents[1] / "packages/harness/focus/persistence/migrations/alembic.ini"))
    engine = create_engine(make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg"))
    def default():
        with engine.connect() as connection:
            return connection.scalar(text("SELECT column_default FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'agent_loops' AND column_name = 'interaction_mode'"))
    try:
        assert "workspace_patrol" in default()
        command.downgrade(config, "39d4e5f6a7b8")
        assert "context_loop" in default()
        command.upgrade(config, "4a5b6c7d8e9f")
        assert "workspace_patrol" in default()
    finally:
        command.upgrade(config, "4a5b6c7d8e9f")
        engine.dispose()
