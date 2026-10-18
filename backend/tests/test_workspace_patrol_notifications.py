"""Workspace Patrol summaries govern notifications for every managed Context."""

import asyncio
from datetime import UTC, datetime, timedelta
import os
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.models import AgentLoop, LoopContextMembership, LoopPendingDecision
from backend.app.desktop.agent_loop.wait_models import LoopWaitRequest
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.service import DesktopService


def test_workspace_patrol_summaries_include_members_waits_and_successors(isolated_postgres_database):
    async def scenario():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workspace_id, root, child, standalone, loop_id = [uuid.uuid4().hex for _ in range(5)]
        clarification, budget, decision = [uuid.uuid4().hex for _ in range(3)]
        now = datetime.now(UTC)
        service = DesktopService.__new__(DesktopService)
        service.session_factory = sessions

        async def summaries():
            return {item["task_id"]: item for item in await service.list_tasks()}

        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=f"/tmp/{workspace_id}", display_name="Patrol"))
                await session.flush()
                session.add_all([DesktopThread(task_id=identity, thread_id=identity, workspace_id=workspace_id, title=identity)
                                 for identity in (root, child, standalone)])
                await session.flush()
                session.add(AgentLoop(loop_id=loop_id, workspace_id=workspace_id, initial_context_id=root,
                                      holder_id="patrol", interaction_mode="workspace_patrol", status="running",
                                      created_at=now, updated_at=now))
                await session.flush()
                session.add(LoopContextMembership(membership_id=uuid.uuid4().hex, loop_id=loop_id,
                                                  context_id=child, role="worker"))
                session.add(DesktopRun(run_id=uuid.uuid4().hex, task_id=child, agent_id=f"main:{child}",
                                       kind="main", status="success", created_at=now + timedelta(seconds=2)))
                session.add_all([
                    LoopWaitRequest(request_id=clarification, loop_id=loop_id, kind="clarification",
                                    prompt="private question", response_mode="text", correlation_id="question",
                                    created_by="patrol", scope={"information_only": True}),
                    LoopWaitRequest(request_id=budget, loop_id=loop_id, kind="budget_action",
                                    prompt="private budget", response_mode="action", correlation_id="budget",
                                    created_by="patrol"),
                    LoopPendingDecision(pending_decision_id=decision, loop_id=loop_id,
                                        kind="access_approval", payload={"command": "private"}),
                ])

            rows = await summaries()
            expected = {"loop_id": loop_id, "status": "running", "interaction_mode": "workspace_patrol",
                        "workspace_id": workspace_id, "initial_context_id": root}
            assert rows[root].get("loop") == expected
            assert rows[child].get("loop") == expected
            assert "loop" not in rows[standalone]
            assert {item["id"] for item in rows[root]["attention"]} == {clarification, budget, decision}
            assert all(set(item) == {"id", "kind", "loop_id"} for item in rows[root]["attention"])
            assert rows[child]["attention"] == []
            assert rows[root]["latest_run"] is None  # Intake does not create a Main Run.

            for status in ("waiting_user", "paused", "completed", "failed", "stopped"):
                async with sessions.begin() as session:
                    loop = await session.get(AgentLoop, loop_id)
                    loop.status, loop.updated_at = status, now + timedelta(seconds=1)
                rows = await summaries()
                assert rows[root]["loop"]["status"] == status
                # A child Run's timestamp must never detach it from the overall result.
                assert rows[child]["loop"]["status"] == status
                if status in {"completed", "failed", "stopped"}:
                    assert rows[root]["attention"] == []

            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, loop_id)
                loop.status, loop.health, loop.waiting_reason = "waiting_user", "idle", "awaiting_input"
                loop.final_result = {"verification": {"verification_id": "delivery-first", "conclusion": "satisfied",
                                                       "criteria": [{"explanation": "private result"}]}}
            rows = await summaries()
            for identity in (root, child):
                assert rows[identity]["loop"]["status"] == "waiting_user"
                assert rows[identity]["loop"]["delivery"] == {"verification_id": "delivery-first", "conclusion": "satisfied"}
                assert "private result" not in str(rows[identity]["loop"])

            successor = uuid.uuid4().hex
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, loop_id)
                loop.status = "stopped"
                await session.flush()
                session.add(AgentLoop(loop_id=successor, workspace_id=workspace_id, initial_context_id=root,
                                      holder_id="patrol", interaction_mode="workspace_patrol", status="running",
                                      created_at=now + timedelta(seconds=3)))
                await session.flush()
                session.add(LoopContextMembership(membership_id=uuid.uuid4().hex, loop_id=successor,
                                                  context_id=child, role="worker"))
            rows = await summaries()
            assert rows[root]["loop"]["loop_id"] == successor
            assert rows[child]["loop"]["loop_id"] == successor
            assert rows[root]["attention"] == []
            assert "delivery" not in rows[root]["loop"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())
