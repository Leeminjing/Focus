"""本文件对外提供 Loop 控制事实提交与执行器取消的原子边界验证。

输入为隔离 PostgreSQL、正式受理的 Run、暂停/停止/撤权/新 Mission/用户消息入口；输出为回滚无外部取消及提交后才通知的断言。
具体工作流为在末尾事件发布处制造事务失败，再重试真实服务调用；取消回调使用独立连接读取已提交状态，核对旧 Round 与 Directive 收敛。
示例：pytest backend/tests/test_loop_control_commit.py；不使用假账本或改变历史执行身份。
"""

import asyncio
from datetime import UTC, datetime
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from backend.app.desktop.agent_loop.authority_control import LoopAuthorityService
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDirective, LoopRound
from backend.app.desktop.agent_loop.schemas import RevokeLoopGrantRequest
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.run_orchestration.dispatch import RunDispatchRepository
from backend.app.desktop.run_orchestration.lifecycle import RunLifecycleFinalizer
from test_agent_loop_round_liveness import _seed_loop, _stop
from test_loop_execution_ownership import _seed_launching_directive, _admit_run

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


@pytest.mark.parametrize("command", ["pause", "stop", "revoke", "mission", "message"])
def test_control_rollback_does_not_cancel_and_committed_control_is_visible_to_executor(tmp_path, monkeypatch, command):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        reader = create_engine(make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg"))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label=f"control-{command}", started_at=datetime.now(UTC))
        service = fixture["service"]
        notifications = []
        run_id = None
        try:
            directive_id = await _seed_launching_directive(sessions, fixture, label="commit")
            run_id = await _admit_run(sessions, fixture, directive_id)

            def cancel(identity, *, action):
                with Session(reader) as session:
                    assert session.get(DesktopRun, identity).status == "interrupted"
                    assert session.get(LoopDirective, directive_id).lifecycle_state == "cancelled"
                    assert session.get(LoopRound, fixture["round_id"]).status == "superseded"
                notifications.append((identity, action))

            manager = SimpleNamespace(cancel=cancel)
            service._run_manager = manager
            authority = LoopAuthorityService(sessions, manager)
            target = authority if command == "revoke" else service
            original = target._append_event

            async def reject(*args, **kwargs):
                raise RuntimeError("transaction failed before commit")

            async def invoke():
                if command == "revoke":
                    return await authority.mutate(fixture["loop_id"], RevokeLoopGrantRequest(command="revoke"))
                if command == "mission":
                    return await service.override(fixture["loop_id"], goal="Revised goal", task_contract="fixture only",
                        acceptance_criteria=({"criterion_id": "done", "text": "tests pass"},))
                if command == "message":
                    return await service.user_message(fixture["context_id"], "授权内继续")
                return await service.control(fixture["loop_id"], command)

            monkeypatch.setattr(target, "_append_event", reject)
            with pytest.raises(RuntimeError, match="before commit"):
                await invoke()
            assert notifications == []
            async with sessions() as session:
                assert (await session.get(AgentLoop, fixture["loop_id"])).status == "running"
                assert (await session.get(DesktopRun, run_id)).status == "pending"
                assert (await session.get(LoopDirective, directive_id)).lifecycle_state != "cancelled"
            monkeypatch.setattr(target, "_append_event", original)
            await invoke()
            assert notifications == [(run_id, "interrupt")]
        finally:
            service._run_manager = None
            await _stop(service, fixture["loop_id"])
            if run_id is not None:
                await RunLifecycleFinalizer(sessions, SimpleNamespace()).abort_prepared(run_id, "test cleanup")
                async with sessions.begin() as session:
                    await RunDispatchRepository().settle_by_run(session, run_id)
            reader.dispose()
            await engine.dispose()

    asyncio.run(run())
