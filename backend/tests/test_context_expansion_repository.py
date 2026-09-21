r"""本文件对外提供 Context expansion migration、repository、幂等登记与生命周期恢复测试。

输入为隔离 PostgreSQL、真实 Loop/Context Revision、稳定 ExpansionOpportunity 和状态转换；输出为单一当前记录、
不可变 revision 历史、终态封闭和重建后一致断言。具体工作流为迁移测试库、启动 Loop、跨 Repository 实例写读同一
expansion。示例：`pytest backend/tests/test_context_expansion_repository.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop.agent_loop import AgentLoopService, LoopCreateRequest
from backend.app.desktop.agent_loop.context_expansion.contracts import ExpansionOpportunity
from backend.app.desktop.agent_loop.context_expansion.models import LoopContextExpansion, LoopContextExpansionTransition
from backend.app.desktop.agent_loop.context_expansion.repository import ContextExpansionRepository, ExpansionRepositoryRejected
from backend.app.desktop.context_evolution import ContextRevisionContract, ContextRevisionOriginKind, ContextRevisionPayloadMode, ContextRevisionProjectionStatus, ContextRevisionRef, ContextRevisionRepository
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_expansion_repository_is_idempotent_and_recoverable(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, source = await _create_loop(sessions, tmp_path)
        opportunity = ExpansionOpportunity.create(
            loop_id=snapshot["loop_id"],
            round_id=snapshot["current_round_id"],
            source=source,
            purpose="Independent verification",
            work_order="Run focused tests without modifying the primary workspace.",
            completion_check="Focused tests pass with reproducible output.",
            workspace_mode="read_only",
            independence_key="completion-check:tests",
            triggers=("independent_verification",),
            required=True,
        )
        repository = ContextExpansionRepository()
        try:
            async with sessions.begin() as session:
                first = await repository.create(session, opportunity, policy_version="context-expansion-v1", level="required")
                second = await repository.create(session, opportunity, policy_version="context-expansion-v1", level="required")
                assert first.expansion_id == second.expansion_id
            async with sessions.begin() as session:
                await ContextExpansionRepository().transition(session, opportunity.opportunity_id, "curated", "Bootstrap Curator 已形成候选")
                await ContextExpansionRepository().transition(session, opportunity.opportunity_id, "proposed", "Patrol 已提出派生")
                await ContextExpansionRepository().transition(session, opportunity.opportunity_id, "blocked", "Context 预算已耗尽", blocker_code="context_budget_exhausted")
            async with sessions() as session:
                row = await session.get(LoopContextExpansion, opportunity.opportunity_id)
                history = tuple((await session.scalars(select(LoopContextExpansionTransition).where(LoopContextExpansionTransition.expansion_id == opportunity.opportunity_id).order_by(LoopContextExpansionTransition.revision))).all())
                keys = await ContextExpansionRepository().active_independence_keys(session, snapshot["loop_id"])
            assert row.state == "blocked"
            assert row.revision == 4
            assert row.blocker_code == "context_budget_exhausted"
            assert [item.to_state for item in history] == ["detected", "curated", "proposed", "blocked"]
            assert keys == frozenset()
            async with sessions.begin() as session:
                same = await ContextExpansionRepository().transition(session, opportunity.opportunity_id, "blocked", "ignored duplicate")
                assert same.revision == 4
                with pytest.raises(ExpansionRepositoryRejected):
                    await ContextExpansionRepository().transition(session, opportunity.opportunity_id, "proposed", "illegal restart")
        finally:
            loop = await service.get(snapshot["loop_id"])
            if loop["status"] in {"running", "paused", "waiting_user"}:
                await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())


async def _create_loop(sessions, tmp_path):
    suffix = uuid.uuid4().hex[:8]
    workspace_id = f"ws-expand-{suffix}"
    context_id = f"ctx-expand-{suffix}"
    revision_id = uuid.uuid4().hex
    loop_id = uuid.uuid4().hex
    workspace_path = tmp_path / workspace_id
    workspace_path.mkdir()
    source = ContextRevisionRef(
        context_id=context_id,
        revision_id=revision_id,
        generation=1,
        execution_thread_id=f"thread-{suffix}",
        checkpoint_ns="",
        checkpoint_id="checkpoint-1",
        payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
    )
    async with sessions.begin() as session:
        session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(workspace_path), display_name="expansion"))
        await session.flush()
        session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{suffix}", title="expansion"))
        await session.flush()
        revisions = ContextRevisionRepository()
        await revisions.insert(session, ContextRevisionContract(ref=source, content_hash="e" * 64, projection_status=ContextRevisionProjectionStatus.VALID, origin_kind=ContextRevisionOriginKind.ROOT, created_at=datetime.now(UTC)))
        await revisions.switch_current(session, source, None)
        session.add(DesktopRun(run_id=f"initial-{suffix}", task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="success", origin="direct_user", execution_thread_id=f"thread-{suffix}", context_revision_id=revision_id, settled_at=datetime.now(UTC)))
    service = AgentLoopService(sessions)
    snapshot = await service.start(
        LoopCreateRequest(
            loop_id=loop_id,
            workspace_id=workspace_id,
            initial_context_id=context_id,
            initial_run_id=f"initial-{suffix}",
            holder_id="patrol-expansion",
            goal="Implement and verify the change",
            task_contract="Keep the primary workspace safe",
            acceptance_criteria=({"criterion_id": "tests", "text": "focused tests pass"},),
            capabilities=("continue_context", "create_lane", "request_completion"),
            context_scope=(context_id,),
            permission_scope=("read",),
        )
    )
    return service, snapshot, source
