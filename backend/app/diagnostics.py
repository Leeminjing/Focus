"""Read-only, bounded diagnostic report shared by HTTP and `focus doctor`.

Only selected operational fields leave this boundary: never config documents,
credentials, user messages, tool payloads, SQL errors or provider error text.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from functools import lru_cache
import json
from pathlib import Path
import platform
import subprocess
from typing import Any

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import func, select

from backend.app.desktop.agent_loop.models import (
    AgentLoop, LoopBudgetUsage, LoopDecision, LoopDirective, LoopEventOutbox,
    LoopWorkerRequest,
)
from backend.app.desktop.agent_loop.projection_models import LoopProjectionFailure
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.agent_loop.task_progress.models import LoopProgressWork
from backend.app.desktop.models import DesktopRun, PatrolContextBinding
from backend.app.desktop.run_orchestration.models import RunDispatch, RunOutboxEvent

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_LIMIT = 20


@lru_cache(maxsize=1)
def version_info() -> dict[str, Any]:
    package = json.loads((ROOT / "desktop/package.json").read_text(encoding="utf-8"))
    result = {"application": package["version"], "python": platform.python_version(), "commit": None, "release": None}
    try:
        result["commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, timeout=2,
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        result["release"] = subprocess.check_output(
            ["git", "describe", "--tags", "--exact-match", "HEAD"], cwd=ROOT, timeout=2,
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return result


@lru_cache(maxsize=1)
def expected_schema_heads() -> tuple[str, ...]:
    ini = ROOT / "backend/packages/harness/focus/persistence/migrations/alembic.ini"
    return tuple(sorted(ScriptDirectory.from_config(Config(str(ini))).get_heads()))


def startup_snapshot() -> dict[str, Any]:
    """Include only the last startup's bounded timing records, never log text."""
    from focus.config.layered import global_home

    path = global_home() / "startup-timing.jsonl"
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 65536))
            lines = stream.read().splitlines()
    except OSError:
        return {"status": "unavailable"}
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict) or entry.get("status") not in {"ok", "failed"}:
                continue
            if entry.get("stage") not in {"splash", "runtime", "gateway-health", "interface", "ready",
                                           "environment-prepare", "database-check", "data-services", "database-recheck",
                                           "migrations", "migration-verify", "database-schema"}:
                continue
            if not isinstance(entry.get("startup"), str) or len(entry["startup"]) > 64:
                continue
            if not all(isinstance(entry.get(key), (int, float)) for key in ("duration_ms", "elapsed_ms")):
                continue
            entries.append({key: entry[key] for key in ("startup", "stage", "status", "duration_ms", "elapsed_ms")})
        except (ValueError, TypeError):
            continue
    if not entries:
        return {"status": "unavailable"}
    latest = entries[-1]["startup"]
    return {"status": "available", "stages": [entry for entry in entries if entry["startup"] == latest][-20:]}


async def _counts(session, column) -> dict[str, int]:
    rows = await session.execute(select(column, func.count()).group_by(column))
    return {str(state): count for state, count in rows}


async def database_snapshot(sessions) -> dict[str, Any]:
    """Inspect the schema before querying application tables, without migrations."""
    async with sessions() as session:
        connection = await session.connection()
        actual = await connection.run_sync(
            lambda conn: tuple(sorted(MigrationContext.configure(conn).get_current_heads()))
        )
        expected = expected_schema_heads()
        result = {"status": "ok" if actual == expected else "mismatch",
                  "schema": {"current": list(actual), "expected": list(expected)}}
        if actual != expected:
            return result

        loops = await _counts(session, AgentLoop.status)
        runs = await _counts(session, DesktopRun.status)
        # All queues expose counts and the age of their oldest waiting item;
        # a backlog by itself is not proof of an unhealthy component.
        queues = {}
        for name, column, waiting, timestamp in (
            ("run_dispatch", RunDispatch.status, ("accepted",), RunDispatch.accepted_at),
            ("run_events", RunOutboxEvent.status, ("pending", "claimed", "error"), RunOutboxEvent.created_at),
            ("loop_events", LoopEventOutbox.status, ("pending",), LoopEventOutbox.created_at),
            ("workers", LoopWorkerRequest.status, ("pending", "running"), LoopWorkerRequest.created_at),
            ("publications", LoopDecision.status, ("publishing", "publishing_run"), LoopDecision.created_at),
            ("context_runs", LoopDirective.status, ("created",), LoopDirective.created_at),
            ("task_progress", LoopProgressWork.state, ("pending", "claimed", "superseding"), LoopProgressWork.created_at),
        ):
            counts = await _counts(session, column)
            oldest = await session.scalar(select(func.min(timestamp)).where(column.in_(waiting)))
            queues[name] = {"states": counts, "pending": sum(counts.get(key, 0) for key in waiting),
                            "oldest_pending_at": oldest.isoformat() if oldest else None}

        loop_rows = (await session.execute(select(
            AgentLoop.loop_id, AgentLoop.status, AgentLoop.health,
        ).where(AgentLoop.status.not_in(("completed", "stopped")))
            .order_by(AgentLoop.updated_at.desc(), AgentLoop.loop_id).limit(SAMPLE_LIMIT))).mappings().all()
        run_rows = (await session.execute(select(
            DesktopRun.run_id, DesktopRun.loop_id, DesktopRun.round_id, DesktopRun.status,
        ).where(DesktopRun.status.in_(("pending", "running", "error", "timeout")))
            .order_by(DesktopRun.updated_at.desc(), DesktopRun.run_id).limit(SAMPLE_LIMIT))).mappings().all()
        budget_columns = [LoopBudgetUsage.loop_id, LoopBudgetUsage.rounds, LoopBudgetUsage.model_calls,
                          LoopBudgetUsage.input_tokens, LoopBudgetUsage.output_tokens, LoopBudgetUsage.retries]
        budgets = (await session.execute(select(*budget_columns).where(
            LoopBudgetUsage.loop_id.in_([row["loop_id"] for row in loop_rows])
        ).order_by(LoopBudgetUsage.loop_id))).mappings().all()
        events = (await session.execute(select(
            LoopJournalEvent.event_id, LoopJournalEvent.loop_id, LoopJournalEvent.sequence,
            LoopJournalEvent.kind, LoopJournalEvent.entity_type, LoopJournalEvent.entity_id,
        ).order_by(LoopJournalEvent.occurred_at.desc(), LoopJournalEvent.event_id)
            .limit(SAMPLE_LIMIT))).mappings().all()
        failures = (await session.execute(select(
            LoopProjectionFailure.failure_id, LoopProjectionFailure.loop_id,
            LoopProjectionFailure.projector_name, LoopProjectionFailure.status,
            LoopProjectionFailure.attempt_count,
        ).where(LoopProjectionFailure.status != "resolved")
            .order_by(LoopProjectionFailure.last_failed_at.desc(), LoopProjectionFailure.failure_id)
            .limit(SAMPLE_LIMIT))).mappings().all()
        failure_count = await session.scalar(select(func.count()).select_from(LoopProjectionFailure)
                                              .where(LoopProjectionFailure.status != "resolved"))
        curator_errors = await session.scalar(select(func.count()).select_from(PatrolContextBinding)
                                              .where(PatrolContextBinding.last_error.is_not(None)))
        curator_rows = (await session.execute(select(
            PatrolContextBinding.binding_id, PatrolContextBinding.managed_context_id,
            PatrolContextBinding.health_state, PatrolContextBinding.control_state,
        ).where(PatrolContextBinding.last_error.is_not(None))
            .order_by(PatrolContextBinding.updated_at.desc(), PatrolContextBinding.binding_id)
            .limit(SAMPLE_LIMIT))).mappings().all()
        progress_errors = await session.scalar(select(func.count()).select_from(LoopProgressWork)
                                               .where(LoopProgressWork.error.is_not(None), LoopProgressWork.state.in_(("pending", "claimed", "blocked"))))
        progress_rows = (await session.execute(select(
            LoopProgressWork.work_id, LoopProgressWork.loop_id, LoopProgressWork.state, LoopProgressWork.attempts,
        ).where(LoopProgressWork.error.is_not(None), LoopProgressWork.state.in_(("pending", "claimed", "blocked")))
            .order_by(LoopProgressWork.created_at.desc(), LoopProgressWork.work_id)
            .limit(SAMPLE_LIMIT))).mappings().all()
        delivery_errors = (await session.execute(select(
            RunOutboxEvent.event_id, RunOutboxEvent.run_id, RunOutboxEvent.delivery_count,
        ).where(RunOutboxEvent.status == "error")
            .order_by(RunOutboxEvent.created_at.desc(), RunOutboxEvent.event_id)
            .limit(SAMPLE_LIMIT))).mappings().all()
        result.update({"loops": {"states": loops, "sample": [dict(row) for row in loop_rows]},
                       "runs": {"states": runs, "sample": [dict(row) for row in run_rows]},
                       "budgets": [dict(row) for row in budgets], "queues": queues,
                       "events": [dict(row) for row in events],
                       "errors": {"projection_count": failure_count, "projections": [dict(row) for row in failures],
                                  "curator_count": curator_errors, "curators": [dict(row) for row in curator_rows],
                                  "task_progress_count": progress_errors, "task_progress": [dict(row) for row in progress_rows],
                                  "run_event_deliveries": [dict(row) for row in delivery_errors]},
                       "sample_limit": SAMPLE_LIMIT})
        return result


async def collect_diagnostics(sessions, *, runtime=None, run_manager=None, scheduling_enabled: bool | None = None,
                              timeout: float = 5.0) -> dict[str, Any]:
    report = {"report_version": 1, "generated_at": datetime.now(UTC).isoformat(),
              "version": version_info(), "startup": startup_snapshot(), "status": "ok", "issues": []}
    if runtime is None:
        report["runtime"] = {"status": "unavailable"}
        if scheduling_enabled:
            report["issues"].append("supervisor_unavailable")
    else:
        report["runtime"] = {"status": "ok", **runtime.diagnostic_snapshot(),
                             "scheduling_enabled": scheduling_enabled}
        if scheduling_enabled and not report["runtime"]["started"]:
            report["issues"].append("supervisor_not_started")
            report["runtime"]["status"] = "degraded"
        if report["runtime"]["unhealthy_components"]:
            report["issues"].append("component_unhealthy")
            report["runtime"]["status"] = "degraded"
    if run_manager is not None:
        report["runtime"]["runs"] = run_manager.diagnostic_snapshot(SAMPLE_LIMIT)
    try:
        database = await asyncio.wait_for(database_snapshot(sessions), timeout=timeout)
        report["database"] = database
        if database["status"] != "ok":
            report["issues"].append("schema_mismatch")
        else:
            errors = database["errors"]
            if errors["projection_count"] or errors["curator_count"] or errors["task_progress_count"]:
                report["issues"].append("persisted_component_errors")
            if database["queues"]["run_events"]["states"].get("error", 0):
                report["issues"].append("run_event_delivery_errors")
    except Exception as exc:
        report["database"] = {"status": "unavailable", "error_type": type(exc).__name__}
        report["issues"].append("database_check_failed")
    if report["issues"]:
        report["status"] = "degraded"
    return report
