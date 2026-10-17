"""首屏不等待工具连接；列表批量返回持久摘要，仅历史中断任务需要兼容读取检查点。"""

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
from backend.app.desktop.agent_loop.models import AgentLoop, LoopPendingDecision
from backend.app.desktop.agent_loop.wait_models import LoopWaitRequest


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


def test_task_summaries_use_fixed_queries_without_recovery(isolated_postgres_database):
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
                        DesktopThread(task_id=task_id, workspace_id=workspace_id,
                                      thread_id=f"focus-assembly:{workspace_id}" if index == 4 else task_id,
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
                assert len(queries) == 3
                assert all("ui_state" not in query and "input_messages" not in query for query in queries)
                ours = {row["task_id"]: row for row in summaries if row["workspace_id"] == workspace_id}
                assert len(ours) == (1 if count == 1 else 40)
                assert ours[task_ids[0]]["active_run"]["status"] == "running"
                assert ours[task_ids[0]]["latest_run"]["status"] == "success"
                assert "equipment" not in ours[task_ids[0]]["active_run"]
                assert all("ui_state" not in row and "commitment_recovery" not in row for row in ours.values())
            assert ours[task_ids[2]]["lifecycle"] == "archived"
            assert ours[task_ids[3]]["lifecycle"] == "deleted"
            assert ours[task_ids[4]]["harness_mode"] == "assembly"
            assert ours[task_ids[4]]["assembly_target"] == {
                "scope": "workspace", "workspace_id": workspace_id, "path": f"/tmp/{workspace_id}",
            }
            async with sessions.begin() as session:
                session.add_all([
                    DesktopRun(run_id=uuid.uuid4().hex, task_id=task_ids[index],
                               agent_id=f"main:{task_ids[index]}", kind="main", status=status,
                               workspace_result={"task_attention": attention})
                    for index, status, attention in (
                        (5, "success", []), (6, "error", []),
                        (7, "interrupted", [{"id": "authorization", "kind": "access_approval"}]),
                        (8, "interrupted", []),
                    )
                ])
            # A new service has neither task details nor any in-memory recovery cache.
            restarted = DesktopService.__new__(DesktopService)
            restarted.session_factory = sessions
            ours = {row["task_id"]: row for row in await restarted.list_tasks()}
            assert ours[task_ids[5]]["latest_run"]["status"] == "success"
            assert ours[task_ids[6]]["latest_run"]["status"] == "error"
            assert ours[task_ids[7]]["attention"] == [{"id": "authorization", "kind": "access_approval"}]
            assert ours[task_ids[8]]["attention"] == []
            assert ours[task_ids[9]]["latest_run"] is None
            # Legacy interrupted runs are resolved using the real execution identity.
            legacy_run_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(DesktopRun(run_id=legacy_run_id, task_id=task_ids[10],
                                       agent_id=f"main:{task_ids[10]}", kind="main", status="interrupted",
                                       execution_thread_id="legacy-execution", checkpoint_ns="branch"))
            async def checkpoint(config):
                if config["configurable"].get("checkpoint_ns") == "branch":
                    return SimpleNamespace(pending_writes=[("t", "__interrupt__", {"type": "access_review"})])
                return None
            restarted.checkpointer = SimpleNamespace(aget_tuple=AsyncMock(side_effect=checkpoint))
            ours = {row["task_id"]: row for row in await restarted.list_tasks()}
            assert ours[task_ids[10]]["attention"][0]["kind"] == "access_approval"
            assert restarted.checkpointer.aget_tuple.await_count == 2
            await restarted.list_tasks()
            assert restarted.checkpointer.aget_tuple.await_count == 2
            loop_id = uuid.uuid4().hex
            decision_id = uuid.uuid4().hex
            request_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(AgentLoop(loop_id=loop_id, workspace_id=workspace_id,
                                      initial_context_id=task_ids[11], holder_id="user", status="waiting_user"))
                await session.flush()
                session.add(LoopPendingDecision(pending_decision_id=decision_id, loop_id=loop_id,
                                                kind="access_approval", payload={}))
                session.add(LoopWaitRequest(request_id=request_id, loop_id=loop_id, kind="clarification",
                                            prompt="请补充信息", response_mode="text", correlation_id="wait", created_by="user"))
            ours = {row["task_id"]: row for row in await restarted.list_tasks()}
            assert ours[task_ids[11]]["loop"] == {"loop_id": loop_id, "status": "waiting_user"}
            assert {item["id"] for item in ours[task_ids[11]]["attention"]} == {decision_id, request_id}
            async with sessions.begin() as session:
                decision = await session.get(LoopPendingDecision, decision_id)
                decision.status = "resolved"
                request = await session.get(LoopWaitRequest, request_id)
                request.status = "resolved"
            ours = {row["task_id"]: row for row in await restarted.list_tasks()}
            assert ours[task_ids[11]]["attention"] == []
            # Abandoning a commitment must invalidate both persisted and legacy summaries.
            async with sessions.begin() as session:
                session.add(DesktopRun(run_id=uuid.uuid4().hex, task_id=task_ids[12],
                                       agent_id=f"main:{task_ids[12]}", kind="main", status="interrupted",
                                       workspace_result={"task_attention": [{"id": "review", "kind": "commitment"}]}))
            restarted.checkpointer.adelete_thread = AsyncMock()
            await restarted.abandon_commitment(task_ids[12])
            ours = {row["task_id"]: row for row in await restarted.list_tasks()}
            assert ours[task_ids[12]]["attention"] == []
            service._task_payload.assert_not_awaited()
        finally:
            async with sessions.begin() as session:
                await session.execute(delete(AgentLoop).where(AgentLoop.workspace_id == workspace_id))
                await session.execute(delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace_id))
            await engine.dispose()

    asyncio.run(scenario())
