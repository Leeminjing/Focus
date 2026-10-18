"""Controlled full-Run + index prewarm load, isolated DB, no external model calls.

Run normally for cancellation/contention correctness. Set FOCUS_DESKTOP_LOAD_REPORT
to retain reports. These timings describe a scripted workload, never real-provider
performance. Main Runs use the real admission/dispatch/graph/checkpoint/finalizer.
"""

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
import threading
import uuid

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from sqlalchemy import select

from backend.tests.test_desktop_poc import _client, SESSION
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.incremental_index_support import ModelProbe, messages, revision
from backend.app.desktop.agent_loop.context_expansion.index_prewarm import LocalIndexPrewarmer
from backend.app.desktop.agent_loop.models import LoopDelegationGrant
from backend.scripts.desktop_performance_report import summarize


@pytest.mark.usefixtures("runtime_postgres_database")
@pytest.mark.parametrize("prewarm", [False, True])
def test_two_runs_prewarm_and_continuous_submissions(tmp_path, monkeypatch, wait_until, caplog, prewarm):
    from backend.app.gateway.app import app
    import backend.app.desktop.service as service_module

    release = threading.Event()
    active = set()

    async def make_agent(**_kwargs):
        async def respond(state):
            content = state["messages"][-1].content
            if content.startswith("long"):
                active.add(content)
                try:
                    while not release.is_set():
                        await asyncio.sleep(0.01)
                finally:
                    active.discard(content)
            return {"messages": [AIMessage(content="controlled response")]}
        graph = StateGraph(MessagesState)
        graph.add_node("respond", respond)
        graph.add_edge(START, "respond")
        graph.add_edge("respond", END)
        return graph.compile()

    async def no_tools(**_kwargs):
        return []

    monkeypatch.setattr(service_module, "make_lead_agent", make_agent)
    monkeypatch.setattr(service_module, "get_available_tools", no_tools)
    monkeypatch.setenv("FOCUS_PERFORMANCE_METRICS", "1")
    caplog.set_level(logging.INFO, logger="focus.performance")
    seed = None
    with _client() as client:
        service = app.state.desktop_service
        # Freeze unrelated Loop supervision; run the production prewarmer explicitly.
        client.portal.call(app.state.agent_loop_runtime.close)
        folder = tmp_path / "workload"
        folder.mkdir()
        workspace = client.post("/desktop/api/workspaces", headers=SESSION, json={"path": str(folder)}).json()
        runs = []

        def submit(content):
            response = client.post(f"/desktop/api/workspaces/{workspace['workspace_id']}/threads", headers=SESSION,
                                   json={"thread_id": uuid.uuid4().hex, "title": "controlled load"})
            assert response.status_code == 200, response.text
            task = response.json()
            response = client.post(f"/desktop/api/tasks/{task['task_id']}/main/runs", headers=SESSION,
                                   json={"message": content, "permissions": ["read"]})
            assert response.status_code == 200, response.text
            runs.append(response.json()["run_id"])

        async def prepare_index():
            nonlocal seed
            seed = await _seed_loop(service.session_factory, tmp_path, label="load", started_at=datetime.now(UTC))
            async with service.session_factory.begin() as session:
                grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == seed["loop_id"]))
                grant.budgets = {**grant.budgets, "max_model_calls": 1000,
                                 "max_input_tokens": 10000000, "max_output_tokens": 10000000}
            await revision(service.session_factory, seed["context_id"], messages(24))
            async def model_delay(_role, _payload):
                await asyncio.sleep(0.02)
            probe = ModelProbe(gate=model_delay)
            index = probe.service(service.session_factory)
            return asyncio.create_task(LocalIndexPrewarmer(service.session_factory, index).drain(seed["loop_id"]))

        background = None
        try:
            submit("long-a")
            submit("long-b")
            wait_until(lambda: len(active) == 2, timeout=10)
            if prewarm:
                background = client.portal.call(prepare_index)
            for index in range(3):
                submit(f"new requirement {index}")
            response = client.post(f"/desktop/api/runs/{runs[0]}/cancel", headers=SESSION)
            assert response.status_code == 200
            wait_until(lambda: "long-a" not in active, timeout=5)
            assert "long-b" in active
            release.set()
            for run_id in runs[1:]:
                wait_until(lambda run_id=run_id: client.get(f"/desktop/api/runs/{run_id}", headers=SESSION).json()["status"] == "success", timeout=15)
            if background is not None:
                async def finished():
                    return await asyncio.wait_for(background, 10)
                assert client.portal.call(finished) == 1
        finally:
            release.set()
            if background is not None and not background.done():
                client.portal.call(background.cancel)
            if seed is not None:
                client.portal.call(_stop, seed["service"], seed["loop_id"])
        client.portal.call(service._performance.sample)
        records = [json.loads(record.getMessage().split("focus_metric ", 1)[1])
                   for record in caplog.records if record.getMessage().startswith("focus_metric ")]
        report = {"mode": "controlled", "prewarm": prewarm, "submitted_runs": len(runs),
                  "summary": summarize(records)}
        output = Path(os.environ.get("FOCUS_DESKTOP_LOAD_REPORT", tmp_path))
        output.mkdir(parents=True, exist_ok=True)
        (output / f"controlled-prewarm-{str(prewarm).lower()}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        assert len(runs) == 5
        assert bool(report["summary"]["queues"].get("index_prewarm")) == prewarm
