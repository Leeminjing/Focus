"""Opt-in full-Run real-provider load matrix in disposable PostgreSQL databases.

FOCUS_DESKTOP_REAL_LOAD=1 explicitly enables billed model calls. Defaults: three
15-minute repetitions per case. No production task, cache or database is reused.
"""

import asyncio
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
from time import monotonic, sleep
import uuid

import pytest
from sqlalchemy import func, select

from backend.tests.test_desktop_poc import _client, SESSION
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.incremental_index_support import Checkpoints, messages, revision
from backend.app.desktop.agent_loop.context_expansion.portfolio_index import PortfolioSemanticIndexService
from backend.app.desktop.agent_loop.context_expansion.index_prewarm import LocalIndexPrewarmer
from backend.app.desktop.agent_loop.derivation_worker import RoleBoundStructuredModel
from backend.app.desktop.agent_loop.models import LoopDelegationGrant
from backend.app.desktop.models import DesktopRun
from backend.scripts.desktop_performance_report import summarize


pytestmark = [pytest.mark.usefixtures("runtime_postgres_database"),
              pytest.mark.skipif(os.environ.get("FOCUS_DESKTOP_REAL_LOAD") != "1", reason="billed provider load is opt-in")]


@pytest.mark.parametrize("scenario", ["single", "two", "two-prewarm", "idle"])
@pytest.mark.parametrize("repeat", range(int(os.environ.get("FOCUS_DESKTOP_LOAD_REPEATS", "3"))))
def test_real_desktop_load(tmp_path, monkeypatch, caplog, scenario, repeat):
    from backend.app.gateway.app import app
    import backend.app.desktop.service as service_module

    async def no_external_tools(**_kwargs):
        return []
    monkeypatch.setattr(service_module, "get_available_tools", no_external_tools)
    monkeypatch.setenv("FOCUS_LOOP_SUPERVISOR", "0")
    monkeypatch.setenv("FOCUS_PERFORMANCE_METRICS", "1")
    caplog.set_level(logging.INFO, logger="focus.performance")
    duration = float(os.environ.get("FOCUS_DESKTOP_IDLE_SECONDS" if scenario == "idle" else "FOCUS_DESKTOP_LOAD_SECONDS",
                                    "3600" if scenario == "idle" else "900"))
    interval = float(os.environ.get("FOCUS_DESKTOP_SUBMIT_SECONDS", "5"))
    if duration <= 0 or interval <= 0:
        pytest.fail("duration and submission interval must be positive")
    owned_runs, statuses, active_samples = [], [], []
    seed = None
    report = {"mode": "real_provider", "scenario": scenario, "repeat": repeat,
              "duration_seconds": duration, "submit_interval_seconds": interval,
              "corpus": "synthetic 24-message index history; fresh read-only main tasks",
              "limitations": ["Only backend resources are sampled; capture Electron logs separately.",
                              "Run overlap depends on provider latency; verify dispatch/request timelines."]}
    with _client() as client:
        service = app.state.desktop_service
        folder = tmp_path / "workload"
        folder.mkdir()
        response = client.post("/desktop/api/workspaces", headers=SESSION, json={"path": str(folder)})
        assert response.status_code == 200, response.text
        workspace = response.json()

        def submit(index):
            task = client.post(f"/desktop/api/workspaces/{workspace['workspace_id']}/threads", headers=SESSION,
                               json={"thread_id": uuid.uuid4().hex, "title": "isolated real load"})
            task.raise_for_status()
            response = client.post(f"/desktop/api/tasks/{task.json()['task_id']}/main/runs", headers=SESSION,
                json={"message": f"这是隔离的并发负载实验，第 {index} 个独立要求。请解释数据库事务提交与回滚的区别，输出约 300 字，不调用任何工具。",
                      "permissions": ["read"]})
            statuses.append(response.status_code)
            response.raise_for_status()
            owned_runs.append(response.json()["run_id"])

        async def prepare():
            nonlocal seed
            seed = await _seed_loop(service.session_factory, tmp_path, label="real-load", started_at=datetime.now(UTC))
            async with service.session_factory.begin() as session:
                grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == seed["loop_id"]))
                grant.budgets = {**grant.budgets, "max_model_calls": 10000,
                                 "max_input_tokens": 10000000, "max_output_tokens": 10000000}
            await revision(service.session_factory, seed["context_id"], messages(24))
            config = service.app_config
            index = PortfolioSemanticIndexService(service.session_factory, Checkpoints(),
                semantic_projector_factory=lambda: RoleBoundStructuredModel(config, "semantic_index_projector"),
                semantic_claim_verifier_factory=lambda: RoleBoundStructuredModel(config, "claim_verifier"))
            return asyncio.create_task(LocalIndexPrewarmer(service.session_factory, index).drain(seed["loop_id"]))

        async def cleanup():
            for run_id in owned_runs:
                record = service.run_manager.get(run_id)
                if record is not None and record.task is not None and not record.task.done():
                    await service.cancel_run(run_id)
            tasks = [service.run_manager.get(run_id).task for run_id in owned_runs
                     if service.run_manager.get(run_id) is not None and service.run_manager.get(run_id).task is not None]
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 30)
            if seed is not None:
                await _stop(seed["service"], seed["loop_id"])

        background = None
        try:
            initial = 0 if scenario == "idle" else 1 if scenario == "single" else 2
            for index in range(initial):
                submit(index)
            if scenario == "two-prewarm":
                background = client.portal.call(prepare)
            deadline = monotonic() + duration
            while monotonic() < deadline:
                sleep(min(interval, max(0, deadline - monotonic())))
                async def active_runs():
                    if scenario == "idle":
                        return {"pending": 0, "running": 0}
                    async with service.session_factory() as session:
                        rows = await session.execute(select(DesktopRun.status, func.count()).where(
                            DesktopRun.run_id.in_(owned_runs), DesktopRun.status.in_(("pending", "running"))).group_by(DesktopRun.status))
                        return {"pending": 0, "running": 0, **dict(rows.all())}
                counts = client.portal.call(active_runs)
                active_count = counts["pending"] + counts["running"]
                active_samples.append({"timestamp": datetime.now(UTC).isoformat(), **counts})
                should_submit = scenario in {"two", "two-prewarm"} or (scenario == "single" and active_count == 0)
                if should_submit and monotonic() < deadline:
                    submit(len(owned_runs))
            if background is not None:
                async def drain():
                    return await asyncio.wait_for(background, 120)
                report["prewarmed_revisions"] = client.portal.call(drain)
        finally:
            try:
                client.portal.call(cleanup)
            finally:
                if background is not None and not background.done():
                    client.portal.call(background.cancel)
                client.portal.call(service._performance.sample)
                records = [json.loads(record.getMessage().split("focus_metric ", 1)[1]) for record in caplog.records
                           if record.getMessage().startswith("focus_metric ")]
                report.update(submissions=len(owned_runs), submission_statuses=statuses,
                              active_run_samples=active_samples,
                              request_timeline=[row for row in records if row["event"] in {
                                  "provider_request", "run_dispatch", "run_first_token", "run_cancelled"}],
                              summary=summarize(records))
                output = Path(os.environ.get("FOCUS_DESKTOP_LOAD_REPORT", tmp_path))
                output.mkdir(parents=True, exist_ok=True)
                (output / f"real-{scenario}-{repeat}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if scenario != "idle":
            assert report["summary"]["provider_requests"], "No measured provider requests; check configured HTTP client instrumentation"
