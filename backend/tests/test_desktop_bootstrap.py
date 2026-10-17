"""首屏不等待工具连接；真实任务列表以固定查询数返回摘要，不读取恢复状态。"""

import asyncio
from datetime import UTC, datetime, timedelta
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.routes import desktop_router
from backend.app.desktop.service import DesktopService


def test_bootstrap_does_not_wait_for_tool_discovery(monkeypatch):
    async def scenario():
        service = DesktopService.__new__(DesktopService)
        service.app_config = SimpleNamespace(models=[])
        service.list_tasks = AsyncMock(return_value=[])
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_tools():
            started.set()
            await release.wait()
            return [SimpleNamespace(name="remote_tool")]

        monkeypatch.setattr("focus.tools.tools.get_available_tools", slow_tools)
        app = FastAPI()
        app.include_router(desktop_router)
        app.state.desktop_service = service
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = await asyncio.wait_for(client.get("/desktop/api/bootstrap"), 1)
            assert first.status_code == 200
            assert first.json()["equipment"]["tools_status"] == "deferred"
            assert first.json()["equipment"]["tools"] == []
            assert not started.is_set()
            equipment = asyncio.create_task(client.get("/desktop/api/equipment"))
            try:
                await asyncio.wait_for(started.wait(), 1)
                assert not equipment.done()
                second = await asyncio.wait_for(client.get("/desktop/api/bootstrap"), 1)
                assert second.status_code == 200
            finally:
                release.set()
                response = await equipment
            assert response.json()["tools_status"] == "loaded"
            assert response.json()["tools"][0]["name"] == "remote_tool"

    asyncio.run(scenario())


def test_task_summaries_use_two_queries_without_recovery(isolated_postgres_database):
    async def scenario():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workspace_id = uuid.uuid4().hex
        task_ids = [uuid.uuid4().hex for _ in range(40)]
        now = datetime.now(UTC)
        service = DesktopService.__new__(DesktopService)
        service.session_factory = sessions
        service._task_payload = AsyncMock(side_effect=AssertionError("列表不得读取详情/checkpoint"))
        queries = []

        def record(_connection, _cursor, statement, *_args):
            if statement.lstrip().upper().startswith("SELECT"):
                queries.append(statement)

        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=f"/tmp/{workspace_id}", display_name="Startup"))
            for count in (1, 39):
                offset = 0 if count == 1 else 1
                async with sessions.begin() as session:
                    session.add_all([
                        DesktopThread(task_id=task_id, workspace_id=workspace_id, thread_id=task_id,
                                      title=f"Task {index}", ui_state={"input": "large private draft"},
                                      archived_at=now if index == 2 else None,
                                      deleted_at=now if index == 3 else None)
                        for index, task_id in enumerate(task_ids[offset:offset + count], offset)
                    ])
                if count == 1:
                    async with sessions.begin() as session:
                        session.add_all([
                            DesktopRun(run_id=uuid.uuid4().hex, task_id=task_ids[0],
                                       agent_id=agent, kind="main", status=status, created_at=created)
                            for agent, status, created in (
                                (f"main:{task_ids[0]}", "pending", now - timedelta(seconds=1)),
                                (f"main:{task_ids[0]}", "running", now),
                                (f"main:{task_ids[0]}", "success", now + timedelta(seconds=1)),
                                ("patrol:other", "running", now + timedelta(seconds=2)),
                            )
                        ])
                queries.clear()
                event.listen(engine.sync_engine, "before_cursor_execute", record)
                try:
                    summaries = await service.list_tasks()
                finally:
                    event.remove(engine.sync_engine, "before_cursor_execute", record)
                assert len(queries) == 2
                assert all("ui_state" not in query and "input_messages" not in query for query in queries)
                ours = {row["task_id"]: row for row in summaries if row["workspace_id"] == workspace_id}
                assert len(ours) == (1 if count == 1 else 40)
                assert ours[task_ids[0]]["active_run"]["status"] == "running"
                assert "equipment" not in ours[task_ids[0]]["active_run"]
                assert all("ui_state" not in row and "commitment_recovery" not in row for row in ours.values())
            assert ours[task_ids[2]]["lifecycle"] == "archived"
            assert ours[task_ids[3]]["lifecycle"] == "deleted"
            service._task_payload.assert_not_awaited()
        finally:
            async with sessions.begin() as session:
                await session.execute(delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace_id))
            await engine.dispose()

    asyncio.run(scenario())
