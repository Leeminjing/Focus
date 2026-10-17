"""Diagnostic exports exercise real SQL, HTTP discovery and failure isolation."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from threading import Thread
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app import diagnostics, doctor
from backend.app.diagnostic_endpoint import publish_endpoint
from backend.app.desktop.agent_loop.coordinator import LoopCoordinatorRuntime
from backend.app.desktop.agent_loop.supervisor import LoopSupervisor, SupervisorComponent


def test_real_gateway_health_retains_startup_probe_and_reports_live_runs(runtime_postgres_database, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from backend.app.gateway.app import app
    from focus.config import app_config
    from focus.runtime.runs.schemas import RunStatus

    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path))
    monkeypatch.setenv("FOCUS_UPDATE_CHECK", "1")
    monkeypatch.setattr(app_config, "_app_config", None)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        response = client.get("/health")
        assert response.status_code == 200
        report = response.json()
        assert report["status"] == "ok", report
        assert report["database"]["status"] == "ok"
        assert not report["runtime"]["scheduling_enabled"]
        assert not report["runtime"]["started"]
        record = app.state.run_manager.create("test-thread")
        app.state.run_manager.update(record.run_id, status=RunStatus.running)
        report = client.get("/health").json()
        assert report["runtime"]["runs"]["states"]["running"] == 1
        assert "session_key" not in json.dumps(report)


def test_postgres_report_correlates_work_without_exporting_content(runtime_postgres_database, monkeypatch, tmp_path):
    from backend.app.desktop.models import DesktopWorkspace, DesktopThread, DesktopRun
    from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage
    from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
    from backend.app.desktop.agent_loop.projection_models import LoopProjectionFailure
    from backend.app.desktop.run_orchestration.models import RunDispatch

    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path))
    secret = "sensitive-provider-credential-and-user-prompt"

    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine)
        try:
            report = await diagnostics.collect_diagnostics(sessions)
            assert report["status"] == "ok", report
            offline = await doctor.offline_report(5)
            assert offline["database"]["status"] == "ok", offline
            assert offline["runtime"]["status"] == "unavailable"
            assert offline["issues"] == ["gateway_unavailable"]
            identifier = uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=identifier, path=identifier, display_name=secret))
                await session.flush()
                session.add(DesktopThread(task_id=identifier, workspace_id=identifier, thread_id=identifier, title=secret))
                await session.flush()
                session.add(AgentLoop(loop_id=identifier, workspace_id=identifier, initial_context_id=identifier,
                                      holder_id=identifier, status="running", health="waiting_runs"))
                await session.flush()
                session.add(DesktopRun(run_id=identifier, task_id=identifier, agent_id=identifier, kind="main",
                                       loop_id=identifier, status="running", input_messages=[{"content": secret}]))
                session.add(LoopBudgetUsage(loop_id=identifier, model_calls=4, input_tokens=123))
                session.add(LoopJournalEvent(event_id=identifier, loop_id=identifier, sequence=1, kind="run.started",
                    entity_type="run", entity_id=identifier, entity_revision=1, payload={"secret": secret}, idempotency_key=identifier))
                session.add(LoopProjectionFailure(failure_id=identifier, loop_id=identifier, projector_name="loop_facts",
                    unit_kind="run", unit_id=identifier, status="quarantined", error_class="ValueError", error_message=secret, attempt_count=2))
                await session.flush()
                session.add(RunDispatch(dispatch_id=identifier, run_id=identifier, status="accepted"))
            report = await diagnostics.collect_diagnostics(sessions)
            assert report["status"] == "degraded"
            database = report["database"]
            assert database["schema"]["current"] == database["schema"]["expected"]
            assert database["loops"]["sample"][0]["loop_id"] == identifier
            assert database["runs"]["sample"][0]["run_id"] == identifier
            assert database["budgets"][0]["model_calls"] == 4
            assert database["queues"]["run_dispatch"]["pending"] == 1
            assert database["queues"]["run_dispatch"]["oldest_pending_at"]
            assert database["events"][0]["entity_id"] == identifier
            assert database["errors"]["projections"][0]["loop_id"] == identifier
            assert secret not in json.dumps(report)
            # Reading diagnostics must not claim or dispatch queued work.
            async with sessions() as session:
                dispatch = await session.get(RunDispatch, identifier)
                assert dispatch.status == "accepted" and dispatch.attempt == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_schema_mismatch_skips_missing_application_tables(monkeypatch, tmp_path):
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path))
    async def run():
        engine = create_async_engine("sqlite+aiosqlite://")
        try:
            report = await diagnostics.collect_diagnostics(async_sessionmaker(engine))
            assert report["issues"] == ["schema_mismatch"]
            assert report["database"]["schema"]["current"] == []
            assert "queues" not in report["database"]
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["exception", "timeout"])
def test_database_failure_is_bounded_and_does_not_expose_error(monkeypatch, failure):
    async def failed(sessions):
        if failure == "timeout":
            await asyncio.Event().wait()
        raise RuntimeError("postgresql://user:secret@private-server/db")
    monkeypatch.setattr(diagnostics, "database_snapshot", failed)
    report = asyncio.run(diagnostics.collect_diagnostics(None, timeout=0.01))
    assert report["issues"] == ["database_check_failed"]
    assert report["database"]["error_type"] == ("TimeoutError" if failure == "timeout" else "RuntimeError")
    assert "secret" not in json.dumps(report)


def test_live_component_failure_and_recovery_are_reported_without_error_text(monkeypatch):
    monkeypatch.setattr(diagnostics, "database_snapshot", AsyncMock(return_value={
        "status": "mismatch", "schema": {"current": [], "expected": ["head"]}}))
    async def run():
        failed = asyncio.Event()
        recover = asyncio.Event()
        async def tick():
            if not recover.is_set():
                failed.set()
                raise ValueError("secret-model-key")
        runtime = LoopCoordinatorRuntime(None, None)
        runtime._supervisor = LoopSupervisor("test", [SupervisorComponent("workers", tick, 0.001)])
        await runtime._supervisor.start()
        try:
            await failed.wait()
            report = await diagnostics.collect_diagnostics(None, runtime=runtime, scheduling_enabled=True)
            assert "component_unhealthy" in report["issues"]
            assert report["runtime"]["components"][0]["error_type"] == "ValueError"
            assert "secret-model-key" not in json.dumps(report)
            recover.set()
            for _ in range(50):
                if not runtime.diagnostic_snapshot()["unhealthy_components"]:
                    break
                await asyncio.sleep(0.001)
            report = await diagnostics.collect_diagnostics(None, runtime=runtime, scheduling_enabled=True)
            assert "component_unhealthy" not in report["issues"]
        finally:
            await runtime.close()
    asyncio.run(run())


def test_endpoint_discovery_cleanup_preserves_another_process(monkeypatch, tmp_path):
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path))
    monkeypatch.setenv("FOCUS_DESKTOP_PORT", "54321")
    path = tmp_path / "diagnostics-endpoint.json"
    with publish_endpoint():
        endpoint = json.loads(path.read_text())
        assert endpoint == {"pid": os.getpid(), "url": "http://127.0.0.1:54321"}
    assert not path.exists()
    with publish_endpoint():
        path.write_text('{"pid": -1, "url": "http://127.0.0.1:54322"}')
    assert json.loads(path.read_text())["pid"] == -1


def test_doctor_discovers_gateway_and_exports_json(monkeypatch, tmp_path, capsys):
    report = {"report_version": 1, "status": "ok", "database": {"status": "ok"},
              "runtime": {"status": "ok"}, "issues": []}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/health"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(report).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path))
    monkeypatch.delenv("FOCUS_DESKTOP_API", raising=False)
    (tmp_path / "diagnostics-endpoint.json").write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_port}"}))
    output = tmp_path / "report.json"
    try:
        monkeypatch.setenv("FOCUS_DOCTOR_CWD", str(tmp_path))
        assert doctor.main(["--json", "--output", "report.json"]) == 0
        stdout = json.loads(capsys.readouterr().out)
        assert stdout == json.loads(output.read_text(encoding="utf-8"))
        assert stdout["source"] == "gateway"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_doctor_stale_endpoint_falls_back_to_offline(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path))
    monkeypatch.delenv("FOCUS_DESKTOP_API", raising=False)
    (tmp_path / "diagnostics-endpoint.json").write_text("invalid json")
    monkeypatch.setattr(doctor, "offline_report", AsyncMock(return_value={
        "report_version": 1, "status": "degraded", "database": {"status": "unavailable"},
        "runtime": {"status": "unavailable"}, "issues": ["gateway_unavailable"]}))
    assert doctor.main(["--json"]) == 1
    assert json.loads(capsys.readouterr().out)["source"] == "offline"


@pytest.mark.parametrize("url", ["http://example.com:80", "https://127.0.0.1:8765", "http://user:secret@localhost:8765", "http://localhost:8765/path"])
def test_doctor_rejects_non_loopback_or_credential_urls(url):
    with pytest.raises(ValueError):
        doctor.loopback_url(url)


def test_startup_log_export_is_bounded_and_drops_unknown_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path))
    entries = ["truncated", json.dumps({"startup": "last", "stage": "runtime", "status": "failed",
               "duration_ms": 12, "elapsed_ms": 20, "secret": "credential"})]
    (tmp_path / "startup-timing.jsonl").write_text("\n".join(entries))
    report = diagnostics.startup_snapshot()
    assert report["stages"][0]["status"] == "failed"
    assert "credential" not in json.dumps(report)
