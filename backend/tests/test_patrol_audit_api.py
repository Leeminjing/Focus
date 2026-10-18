"""生产审计/Observation HTTP 入口的多版本、等待目标、权限和分页验收。

使用隔离 PostgreSQL 与真实 Patrol 持久化，不调用 Provider。
示例：python -m pytest backend/tests/test_patrol_audit_api.py -q。
"""

import asyncio
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.models import LoopDelegationGrant, LoopWorkerRequest
from backend.app.desktop.agent_loop.patrol_session_models import LoopCuratorAssignment, LoopPatrolPhaseTransition
from backend.app.desktop.agent_loop.patrol_session_repository import PatrolSessionRepository
from backend.app.desktop.agent_loop.patrol_session_state import PatrolActivity, PatrolEvidenceReference, PatrolWaitTarget
from backend.app.desktop.agent_loop.query_routes import loop_query_router
from test_patrol_session import _create_loop, _observation_envelope, _persist_observation


@pytest.mark.usefixtures("isolated_postgres_database")
def test_http_patrol_history_selects_versions_and_redacts_all_records(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot = await _create_loop(sessions, tmp_path)
        other_service, other = await _create_loop(sessions, tmp_path)
        loop_id, round_id = snapshot["loop_id"], snapshot["current_round_id"]
        repository = PatrolSessionRepository()
        app = FastAPI()
        app.include_router(loop_query_router)
        app.state.desktop_service = SimpleNamespace(session_factory=sessions)
        ids, session_ids, worker_ids = [], [], []
        try:
            for generation in (1, 2):
                observation = await _persist_observation(sessions, _observation_envelope(loop_id, round_id), generation=generation)
                ids.append(observation.observation_id)
                async with sessions.begin() as session:
                    patrol = await repository.begin(session, loop_id, round_id, generation)
                    session_ids.append(patrol.session_id)
                    worker_id = uuid4().hex
                    worker_ids.append(worker_id)
                    session.add(LoopWorkerRequest(worker_request_id=worker_id, loop_id=loop_id, round_id=round_id,
                        observation_id=ids[-1], observation_generation=generation,
                        kind="lane_curator", scope={}, status="error" if generation == 1 else "running", result={}))
                    await session.flush()
                    session.add(LoopCuratorAssignment(assignment_id=uuid4().hex, assignment_key=f"v{generation}",
                        session_id=patrol.session_id, loop_id=loop_id, round_id=round_id, worker_request_id=worker_id,
                        state="failed" if generation == 1 else "analyzing",
                        scope={"context_id": f"v{generation}", "evidence": "hidden-scope", "private_reasoning": "hidden-thought"},
                        evidence_refs=[{"kind": "test", "entity_id": "private-evidence"}],
                        result_summary=f"version {generation}", failure="Curator 超时" if generation == 1 else None))
                    activity = PatrolActivity(summary=f"version {generation}", wait_reason="等待 Curator",
                        wait_targets=(PatrolWaitTarget(entity_type="worker_request", entity_id=worker_id),),
                        evidence_refs=(PatrolEvidenceReference(kind="test", entity_id="private-evidence"),))
                    if generation == 1:
                        await repository.transition(session, patrol.session_id, "failed", activity,
                            terminal_outcome={"status": "failed", "secret": "hidden-terminal"})
                    else:
                        for phase in ("freezing_observation", "observing", "dispatching_curators", "collecting_curators"):
                            await repository.transition(session, patrol.session_id, phase, activity)
            base = f"/desktop/api/agent-loops/{loop_id}"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                async with sessions() as session:
                    before = await session.scalar(select(func.count()).select_from(LoopPatrolPhaseTransition))
                response = await client.get(f"{base}/audit")
                assert response.status_code == 200, response.text
                current = response.json()["patrol_history"]
                assert current["observation_id"] == ids[1]
                assert current["session"]["phase"] == "collecting_curators"
                assert current["phases"][-1]["wait_targets"][0]["entity_id"] == worker_ids[1]
                round_only = (await client.get(f"{base}/audit", params={"round_id": round_id})).json()["patrol_history"]
                assert round_only["observation_id"] == ids[1]
                for generation, identity in enumerate(ids, 1):
                    response = await client.get(f"{base}/audit", params={"round_id": round_id, "observation_id": identity})
                    assert response.status_code == 200, response.text
                    history = response.json()["patrol_history"]
                    assert history["session"]["session_id"] == session_ids[generation - 1]
                    assert {item["observation_id"] for item in history["phases"]} == {identity}
                    assert [item["result_summary"] for item in history["curators"]] == [f"version {generation}"]
                    assert "private-evidence" not in json.dumps(history)
                    assert "hidden-" not in json.dumps(history)
                exact = (await client.get(f"{base}/audit", params={"observation_id": ids[0]})).json()["patrol_history"]
                assert exact["session"]["status"] == "failed"
                assert exact["curators"][0]["failure"] == "Curator 超时"
                assert (await client.get(f"{base}/audit", params={"round_id": other["current_round_id"]})).status_code == 404
                assert (await client.get(f"{base}/audit", params={"round_id": other["current_round_id"], "observation_id": ids[0]})).status_code == 404
                assert (await client.get(f"{base}/audit", params={"observation_id": uuid4().hex})).status_code == 404
                assert (await client.get(f"/desktop/api/agent-loops/{other['loop_id']}/audit", params={"observation_id": ids[0]})).status_code == 404
                url = f"{base}/observations/{ids[1]}"
                params = {"section": "patrol_history", "limit": 2}
                first = (await client.get(url, params=params)).json()
                assert first["has_more"] and first["patrol_session"]["session_id"] == session_ids[1]
                rows = list(first["items"])
                page = first
                while page["has_more"]:
                    response = await client.get(url, params={**params, "cursor": page["next_cursor"]})
                    assert response.status_code == 200, response.text
                    page = response.json()
                    rows.extend(page["items"])
                assert len(rows) == first["total"]
                assert rows[-1]["history_kind"] == "curator"
                async with sessions() as session:
                    assert await session.scalar(select(func.count()).select_from(LoopPatrolPhaseTransition)) == before
                async with sessions.begin() as session:
                    grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop_id))
                    grant.permission_scope = ["read", "view_evidence"]
                assert (await client.get(url, params={**params, "cursor": first["next_cursor"]})).status_code == 409
                visible = (await client.get(url, params={**params, "limit": 120})).json()
                assert "private-evidence" in json.dumps(visible)
                assert "hidden-thought" not in json.dumps(visible)
                fresh = (await client.get(url, params=params)).json()
                async with sessions.begin() as session:
                    await repository.transition(session, session_ids[1], "collecting_curators", PatrolActivity(summary="仍在等待"))
                assert (await client.get(url, params={**params, "cursor": fresh["next_cursor"]})).status_code == 409
                no_session = await _persist_observation(sessions, _observation_envelope(loop_id, round_id), generation=3)
                empty = (await client.get(f"{base}/audit")).json()["patrol_history"]
                assert empty["observation_id"] == no_session.observation_id
                assert empty["session"] is None and empty["phases"] == [] and empty["curators"] == []
                empty_page = (await client.get(f"{base}/observations/{no_session.observation_id}", params={"section": "patrol_history"})).json()
                assert empty_page["patrol_session"] is None and empty_page["items"] == []
                old = (await client.get(f"{base}/audit", params={"observation_id": ids[0]})).json()["patrol_history"]
                assert old["session"]["status"] == "failed"
        finally:
            await service.control(loop_id, "stop")
            await other_service.control(other["loop_id"], "stop")
            await engine.dispose()
    asyncio.run(run())
