"""Storage inventory, previewed cleanup, database backups and in-app scheduling.

Only journal prefixes and recognised backup directories are eligible. Backups use
the protected updater lock; domain evidence and user workspace files are retained.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import func, select, text

from focus.config.layered import global_home
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent, LoopJournalSequence, LoopProjectorCursor
from backend.app.desktop.agent_loop.journal_retention import cleanup_boundary

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[3]


class StoragePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    journal_retention_days: int = Field(default=7, ge=1, le=3650)
    backup_retention_days: int = Field(default=30, ge=1, le=3650)
    backup_keep_count: int = Field(default=3, ge=1, le=100)
    automatic_cleanup: StrictBool = False
    backup_interval_hours: int = Field(default=0, ge=0, le=8760)


class StorageMaintenance:
    def __init__(self, sessions, *, home=None, database_url=None):
        self.sessions = sessions
        self.home = Path(home) if home else global_home()
        self.database_url = database_url
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task = None
        self._previews = {}
        self.last_error = None
        self.last_run = None

    def policy(self):
        file = self.home / "storage-policy.json"
        return StoragePolicy.model_validate_json(file.read_text(encoding="utf-8")) if file.exists() else StoragePolicy()

    async def save_policy(self, policy):
        async with self._lock:
            self.home.mkdir(parents=True, exist_ok=True)
            file = self.home / "storage-policy.json"
            temporary = file.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(policy.model_dump_json(indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(file)
            self._previews.clear()
            return policy.model_dump()

    def backup_available(self):
        # The archive helper manages only the bundled database. Never back up a
        # different DB from the one actually configured on this Gateway.
        expected = "postgresql+asyncpg://focus:qweasdzxc123@127.0.0.1:7221/focus"
        return (self.database_url == expected
                and not any(os.getenv(key) for key in ("FOCUS_DB_USER", "FOCUS_DB_PASSWORD", "FOCUS_DB_NAME", "FOCUS_DB_PORT"))
                and shutil.which("node") is not None)

    async def _files(self, command, **payload):
        def run():
            environment = {**os.environ, "FOCUS_DATABASE_URL": self.database_url or "", "PYTHONUTF8": "1"}
            result = subprocess.run(
                [shutil.which("node") or "node", str(ROOT / "scripts/storage-maintenance.cjs"), command,
                 str(self.home), str(ROOT), sys.executable],
                input=json.dumps({"policy": self.policy().model_dump(), **payload}),
                capture_output=True, text=True, encoding="utf-8", env=environment,
                timeout=1800,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.strip()[-1500:] or "存储维护失败")
            return json.loads(result.stdout)
        return await asyncio.to_thread(run)

    async def status(self):
        files = await self._files("inventory")
        async with self.sessions() as session:
            database_bytes = await session.scalar(text("SELECT pg_database_size(current_database())"))
            journal_bytes = await session.scalar(text("SELECT pg_total_relation_size('loop_journal_events')"))
            journal_events = await session.scalar(select(func.count()).select_from(LoopJournalEvent))
        return {"policy": self.policy().model_dump(), "database_bytes": database_bytes,
                "journal_bytes": journal_bytes, "journal_events": journal_events, "backups": files,
                "backup_directory": str(self.home / "backups"),
                "backup_available": self.backup_available(), "last_run": self.last_run, "last_error": self.last_error}

    async def _plan(self):
        policy = self.policy()
        files = await self._files("inventory")
        loops = []
        async with self.sessions() as session:
            await session.execute(text("SET LOCAL statement_timeout = '30s'"))
            # Page through all loops, including those whose last event is old.
            after = ""
            while True:
                ids = tuple((await session.scalars(select(LoopJournalSequence.loop_id)
                                                  .where(LoopJournalSequence.loop_id > after)
                                                  .order_by(LoopJournalSequence.loop_id).limit(100))).all())
                if not ids:
                    break
                for loop_id in ids:
                    boundary = await cleanup_boundary(session, loop_id, policy.journal_retention_days * 86400)
                    count = await session.scalar(select(func.count()).select_from(LoopJournalEvent).where(
                        LoopJournalEvent.loop_id == loop_id, LoopJournalEvent.sequence < boundary))
                    if count:
                        loops.append({"loop_id": loop_id, "boundary": boundary, "events": count})
                after = ids[-1]
        backups = [row for row in files["entries"] if not row["protected_reason"]]
        return {"loops": loops, "journal_events": sum(row["events"] for row in loops),
                "backup_ids": [row["id"] for row in backups], "backup_bytes": sum(row["bytes"] for row in backups),
                "policy": policy.model_dump()}

    async def preview(self):
        async with self._lock:
            plan = await self._plan()
            token = secrets.token_urlsafe(24)
            self._previews = {token: (time.monotonic() + 600, plan)}
            return {"token": token, "expires_in_seconds": 600, **plan}

    async def _apply(self, plan):
        if (self.home / "update-state.json").exists():
            raise ValueError("请先完成升级恢复，再执行清理")
        removed = 0
        errors = []
        for row in plan["loops"]:
            try:
                removed += await self._prune_loop(row, plan["policy"])
            except Exception:
                logger.exception("Journal cleanup rolled back for loop %s", row["loop_id"])
                errors.append(f"Loop {row['loop_id']} 清理未完成，事务已回滚；请重新预览后重试")
        backups_removed = []
        if plan["backup_ids"]:
            try:
                files = await self._files("cleanup", ids=plan["backup_ids"])
                backups_removed = files["removed"]
                errors.extend(f"备份 {row['id']} 清理未完成：{row['message']}" for row in files.get("errors", []))
            except Exception as error:
                logger.exception("Backup cleanup failed")
                errors.append(f"备份清理未完成：{error}")
        return {"journal_events_removed": removed, "backups_removed": backups_removed, "errors": errors}

    async def _prune_loop(self, row, policy):
        async with self.sessions.begin() as session:
            await session.execute(text("SET LOCAL lock_timeout = '3s'"))
            await session.execute(text("SET LOCAL statement_timeout = '30s'"))
            from backend.app.desktop.agent_loop.models import AgentLoop
            from backend.app.desktop.agent_loop.live_projection_projector import LoopLiveSnapshotProjector
            from backend.app.desktop.agent_loop.live_checkpoint import LoopLiveCheckpointStore
            # Cursor -> parent -> sequence is the fact projector's write order.
            await session.execute(select(LoopProjectorCursor).where(LoopProjectorCursor.loop_id == row["loop_id"])
                                  .order_by(LoopProjectorCursor.projector_name).with_for_update())
            await session.get(AgentLoop, row["loop_id"], with_for_update=True)
            projection = await LoopLiveSnapshotProjector().project(session, row["loop_id"])
            await LoopLiveCheckpointStore().save(session, projection)
            journal = LoopEventJournal()
            await journal.configure_retention(session, row["loop_id"], policy["journal_retention_days"] * 86400)
            removed = await journal.prune(session, row["loop_id"], maximum_sequence=row["boundary"])
        return removed

    async def cleanup(self, token):
        async with self._lock:
            cached = self._previews.pop(token, None)
            if cached is None or cached[0] < time.monotonic():
                raise ValueError("清理预览已过期，请重新预览")
            plan = cached[1]
            if plan["policy"] != self.policy().model_dump():
                raise ValueError("保留策略已变化，请重新预览")
            return await self._apply(plan)

    async def create_backup(self):
        async with self._lock:
            if not self.backup_available():
                raise ValueError("常规备份仅支持内置本地 PostgreSQL；自定义数据库请使用其备份工具")
            return await self._files("backup")

    async def tick(self):
        async with self._lock:
            policy = self.policy()
            if policy.backup_interval_hours and self.backup_available():
                files = await self._files("inventory")
                latest = max((datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).timestamp()
                              for row in files["entries"] if row["kind"] == "regular"), default=0)
                if time.time() - latest >= policy.backup_interval_hours * 3600:
                    await self._files("backup")
            if policy.automatic_cleanup:
                result = await self._apply(await self._plan())
                if result["errors"]:
                    raise RuntimeError("；".join(result["errors"]))
            self.last_run = datetime.now(UTC).isoformat()
            self.last_error = None

    async def _run(self):
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as error:
                self.last_error = str(error)
                logger.exception("Storage maintenance failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=3600)
            except TimeoutError:
                pass

    async def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="storage-maintenance")

    async def close(self):
        self._stop.set()
        if self._task:
            # Let any archive/transaction finish before the DB runtime shuts down.
            await self._task
            self._task = None
