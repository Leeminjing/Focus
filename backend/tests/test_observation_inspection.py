"""本文件对外提供 Observation 检查的隔离 PostgreSQL/HTTP 验收。

输入为真实冻结 Observation、前序版本、当前权限和分页请求；输出为不可变身份、隐私、分页及零写副作用断言。
具体工作流为复用 Loop 播种与冻结，挂载生产 router，读取后改变当前权限与 head，再检查原冻结分页；合法来源合同中的嵌套指标不能公开，完整性独立于分页；不调用 Provider。
示例：python -m pytest backend/tests/test_observation_inspection.py -q。
"""

import asyncio
import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_agent_loop_round_liveness import _seed_loop, _stop
from test_round_task_progress import _Checkpointer, _source

from backend.app.desktop.agent_loop.models import LoopDelegationGrant, LoopObservation
from backend.app.desktop.agent_loop.fact_models import LoopFact
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.agent_loop.query_routes import loop_query_router
from backend.app.desktop.agent_loop.task_progress.models import LoopDecisionInputs, LoopProgressHead, LoopTaskProgress
from backend.app.desktop.domain_evidence.identity import canonical_hash


@pytest.mark.usefixtures("isolated_postgres_database")
def test_frozen_http_inspection_paging_privacy_and_read_only(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        seed = await _seed_loop(sessions, tmp_path, label="inspection", started_at=datetime.now(UTC))
        other = await _seed_loop(sessions, tmp_path, label="other", started_at=datetime.now(UTC))
        app = FastAPI()
        app.include_router(loop_query_router)
        app.state.desktop_service = SimpleNamespace(session_factory=sessions)
        try:
            envelope = await LoopObservationService(sessions, _Checkpointer()).capture(seed["loop_id"], seed["round_id"])
            async with sessions.begin() as session:
                observation = await session.scalar(select(LoopObservation).where(LoopObservation.round_id == seed["round_id"]))
                frozen = await session.get(LoopDecisionInputs, observation.observation_id)
                frozen_payload = dict(frozen.payload)
                previous = dict(frozen_payload["previous_progress"])
                previous["items"] = [{"item_id": str(i), "description": f"Frozen item {i}", "state": "unknown", "support": "unknown", "evidence_keys": ["private-evidence"]} for i in range(135)]
                previous["history_complete"] = False
                frozen_payload["previous_progress"] = previous
                frozen_payload["previous_progress_hash"] = canonical_hash(previous)
                private_marker = "synthetic-private-metric"
                metric_payloads = [
                    {"metrics": {"count_status": "exact", "passed": {"api_key": private_marker}, "failed": 0, "skipped": 0}},
                    {"metrics": {"count_status": "exact", "passed": [{"messages": private_marker}], "failed": 0, "skipped": 0}},
                    {"metrics": {"count_status": {"diagnostic": private_marker}, "passed": 0, "failed": 0, "skipped": 0}},
                    {"metrics": {"count_status": "exact", "passed": 12, "failed": 1, "skipped": 0, "total": 13}},
                    {"metrics": {"count_status": "not_applicable"}},
                    {"count_status": "exact", "passed": private_marker, "failed": 0, "skipped": 0},
                ]
                frozen_payload["task_delta"] = {
                    **frozen_payload["task_delta"], "complete": False, "blocker": "source_limit",
                    "sources": [_source(source_id=f"metric-{index}", payload=body).model_dump(mode="json") for index, body in enumerate(metric_payloads)],
                }
                frozen.payload = frozen_payload
                frozen.content_hash = canonical_hash(frozen_payload)
                identity = observation.observation_id
                loop_id = seed["loop_id"]
                baseline_count = await session.scalar(select(func.count()).select_from(LoopTaskProgress))
                fact_id = uuid4().hex
                session.add(LoopFact(
                    fact_id=fact_id, loop_id=loop_id, identity_key=canonical_hash({"fixture": fact_id}),
                    fact_type="test", normalized_subject="failed-result", state="verified",
                    presentation={"title": "有证据的失败结果", "outcome_status": "failed", "metrics": {"count_status": "unknown"}},
                    evidence=[{"run_id": "safe-source", "secret": "do-not-expose"}],
                    observer={"private_reasoning": "do-not-expose"}, occurred_at=datetime.now(UTC),
                ))
            url = f"/desktop/api/agent-loops/{loop_id}/observations/{identity}"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                summary = (await client.get(url)).json()
                assert summary["observation_hash"] == observation.envelope_hash
                assert summary["previous_progress"]["item_count"] == 135
                assert summary["evidence_visible"] is False
                assert summary["sources"]["complete"] is False
                assert summary["previous_progress"]["history_complete"] is False
                hidden_sources = (await client.get(url, params={"section": "sources"})).json()
                assert all("evidence" not in row for row in hidden_sources["items"])
                assert "worker_results" not in summary and "grant" not in summary
                first = (await client.get(url, params={"section": "previous_progress", "limit": 40})).json()
                assert first["has_more"] and len(first["items"]) == 40
                assert "evidence_keys" not in first["items"][0]
                facts_url = f"/desktop/api/agent-loops/{loop_id}/facts"
                page = (await client.get(facts_url, params={"outcome_status": "failed"})).json()
                assert len(page["facts"]) == 1 and page["facts"][0]["status"] == "verified"
                assert "evidence" not in page["facts"][0]
                assert (await client.get(facts_url, params={"status": "contradicted"})).json()["facts"] == []
                detail = (await client.get(f"{facts_url}/{fact_id}")).json()
                assert "evidence" not in detail["fact"] and "do-not-expose" not in str(detail)
                async with sessions.begin() as session:
                    head = await session.get(LoopProgressHead, loop_id)
                    current = await session.get(LoopTaskProgress, head.progress_id)
                    current.document = {**current.document, "items": []}
                    grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop_id).order_by(LoopDelegationGrant.revision.desc()).limit(1))
                    grant.permission_scope = ["read", "view_evidence"]
                second = (await client.get(url, params={"section": "previous_progress", "cursor": first["next_cursor"], "limit": 120})).json()
                assert len(second["items"]) == 95 and not second["has_more"]
                assert second["inputs_hash"] == first["inputs_hash"]
                assert second["items"][0]["item_id"] == "40"
                assert second["items"][0]["evidence_keys"] == ["private-evidence"]
                sources = (await client.get(url, params={"section": "sources"})).json()
                assert private_marker not in json.dumps(sources)
                assert [row["evidence"]["metrics"] for row in sources["items"]] == [
                    {"count_status": "unknown"}, {"count_status": "unknown"}, {"count_status": "unknown"},
                    {"count_status": "exact", "passed": 12, "failed": 1, "skipped": 0, "total": 13},
                    {"count_status": "not_applicable"}, {"count_status": "unknown"},
                ]
                assert sources["sources"]["complete"] is False and sources["has_more"] is False
                detail = (await client.get(f"{facts_url}/{fact_id}")).json()
                assert detail["fact"]["evidence"] == [{"run_id": "safe-source"}]
                assert "do-not-expose" not in str(detail)
                assert len({i["item_id"] for i in first["items"] + second["items"]}) == 135
                assert (await client.get(url, params={"section": "sources", "cursor": first["next_cursor"]})).status_code == 422
                assert (await client.get(url, params={"section": "previous_progress", "cursor": "broken"})).status_code == 422
                assert (await client.get(url, params={"section": "unknown"})).status_code == 422
                assert (await client.get(url.replace(loop_id, other["loop_id"]))).status_code == 404
                assert (await client.get(url.replace(identity, "missing"))).status_code == 404
                async with sessions.begin() as session:
                    frozen = await session.get(LoopDecisionInputs, identity)
                    await session.delete(frozen)
                legacy = (await client.get(url)).json()
                assert legacy["availability"] == "legacy"
                assert legacy["previous_progress"]["progress_id"] is None
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(LoopTaskProgress)) == baseline_count
        finally:
            await _stop(seed["service"], loop_id)
            await _stop(other["service"], other["loop_id"])
            await engine.dispose()
    asyncio.run(run())
