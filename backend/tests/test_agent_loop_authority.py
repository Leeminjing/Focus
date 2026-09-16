r"""本文件验证用户作为根权力对 Agent Loop delegation 的收窄、预算调整与撤销。

输入为真实 PostgreSQL Loop、活动 grant、用量账本与活动 Main Run；输出为单调 authority revision、
旧执行中断、权限不可放大、预算即时阻断以及撤销后不可恢复的断言。具体工作流为绑定初始用户 Run，
依次提交 narrow、adjust_budgets 和 revoke。示例：`pytest test_agent_loop_authority.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop.agent_loop import AgentLoopService, LoopAuthorityService, LoopCreateRequest
from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopRound
from backend.app.desktop.agent_loop.schemas import AdjustLoopBudgetsRequest, NarrowLoopGrantRequest, RevokeLoopGrantRequest
from backend.app.desktop.context_evolution import ContextRevisionContract, ContextRevisionOriginKind, ContextRevisionPayloadMode, ContextRevisionProjectionStatus, ContextRevisionRef, ContextRevisionRepository
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


class _RunManager:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def cancel(self, run_id: str, action: str) -> bool:
        self.cancelled.append(run_id)
        return True


def test_root_user_can_narrow_adjust_and_revoke_delegation(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        suffix = uuid.uuid4().hex[:8]
        workspace_id = f"ws-authority-{suffix}"
        context_id = f"context-authority-{suffix}"
        revision_id = uuid.uuid4().hex
        loop_id = uuid.uuid4().hex
        run_manager = _RunManager()
        try:
            async with sessions.begin() as session:
                workspace_path = tmp_path / workspace_id
                workspace_path.mkdir()
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(workspace_path), display_name="authority"))
                await session.flush()
                session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{suffix}", title="authority"))
                await session.flush()
                ref = ContextRevisionRef(context_id=context_id, revision_id=revision_id, generation=1, execution_thread_id=f"thread-{suffix}", checkpoint_ns="", checkpoint_id="checkpoint-1", payload_mode=ContextRevisionPayloadMode.CHECKPOINT)
                repository = ContextRevisionRepository()
                await repository.insert(session, ContextRevisionContract(ref=ref, content_hash="a" * 64, projection_status=ContextRevisionProjectionStatus.VALID, origin_kind=ContextRevisionOriginKind.ROOT, created_at=datetime.now(UTC)))
                await repository.switch_current(session, ref, None)
                session.add(DesktopRun(run_id=f"initial-{suffix}", task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="success", origin="direct_user", execution_thread_id=f"thread-{suffix}", context_revision_id=revision_id, model_call_count=3, prompt_input_tokens=10, prompt_output_tokens=5, settled_at=datetime.now(UTC)))

            service = AgentLoopService(sessions, run_manager)
            started = await service.start(LoopCreateRequest(loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id, initial_run_id=f"initial-{suffix}", holder_id=f"patrol-{suffix}", goal="Ship safely", task_contract="Stay in scope", acceptance_criteria=({"criterion_id": "done", "text": "done"},), capabilities=("continue_context", "request_completion"), context_scope=(context_id,), permission_scope=("read", "write")))
            async with sessions() as session:
                initial = await session.get(DesktopRun, f"initial-{suffix}")
                first_round = await session.get(LoopRound, initial.round_id)
                initial_usage = await session.get(LoopBudgetUsage, loop_id)
                assert initial.loop_id == loop_id
                assert first_round.number == 1
                assert first_round.status == "settled"
                assert started["current_round_id"] != first_round.round_id
                assert (initial_usage.model_calls, initial_usage.input_tokens, initial_usage.output_tokens) == (3, 10, 5)
            async with sessions.begin() as session:
                session.add(DesktopRun(run_id=f"active-{suffix}", task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="pending", origin="delegated_patrol", execution_thread_id=f"thread-active-{suffix}", context_revision_id=revision_id, loop_id=loop_id, round_id=started["current_round_id"]))

            authority = LoopAuthorityService(sessions, run_manager)
            with pytest.raises(HTTPException) as enlargement:
                await authority.mutate(loop_id, NarrowLoopGrantRequest(command="narrow", capabilities=("continue_context", "request_completion", "stop_loop"), context_scope=(context_id,), permission_scope=("read", "write")))
            assert enlargement.value.status_code == 422

            narrowed = await authority.mutate(loop_id, NarrowLoopGrantRequest(command="narrow", capabilities=("continue_context",), context_scope=(context_id,), permission_scope=("read",)))
            assert narrowed["authority_revision"] == 2
            assert run_manager.cancelled == [f"active-{suffix}"]
            snapshot = await service.get(loop_id)
            assert snapshot["grant"]["capabilities"] == ["continue_context"]
            assert snapshot["grant"]["permission_scope"] == ["read"]

            failed_round_id = snapshot["current_round_id"]
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, loop_id, with_for_update=True)
                failed_round = await session.get(LoopRound, failed_round_id, with_for_update=True)
                loop.status = "waiting_user"
                loop.health = "degraded"
                failed_round.status = "error"
            resumed = await service.control(loop_id, "resume")
            assert resumed["current_round_id"] != failed_round_id
            assert resumed["status"] == "running"
            assert resumed["usage"]["retries"] == 1

            async with sessions.begin() as session:
                usage = await session.get(LoopBudgetUsage, loop_id, with_for_update=True)
                usage.output_tokens = 50
            exhausted = await authority.mutate(loop_id, AdjustLoopBudgetsRequest(command="adjust_budgets", budgets={"max_output_tokens": 40}))
            assert exhausted["authority_revision"] == 3
            snapshot = await service.get(loop_id)
            assert snapshot["status"] == "waiting_user"
            assert "output_tokens_budget" in snapshot["waiting_reason"]

            await authority.mutate(loop_id, AdjustLoopBudgetsRequest(command="adjust_budgets", budgets={"max_output_tokens": 100}))
            snapshot = await service.get(loop_id)
            assert snapshot["status"] == "running"
            assert snapshot["authority_revision"] == 4

            await authority.mutate(loop_id, RevokeLoopGrantRequest(command="revoke"))
            snapshot = await service.get(loop_id)
            assert snapshot["status"] == "waiting_user"
            assert snapshot["grant"] is None
            with pytest.raises(HTTPException) as resume:
                await service.control(loop_id, "resume")
            assert resume.value.status_code == 409
        finally:
            await engine.dispose()

    asyncio.run(run())
