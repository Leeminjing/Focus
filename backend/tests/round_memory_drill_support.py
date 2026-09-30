"""本文件提供演练进程管理、保留数据快照、旧版本提取及隔离备份恢复函数。

输入为测试目录、已隔离数据库和明确的 release root；输出为进程阶段与逐行数据签名。
工作流为独立启动服务进程、等待可观测边界、强杀或正常收口；旧 HEAD 提取到测试目录，备份恢复到另一个随机测试库。
示例：process = DrillProcess(root, tmp, 'publish', loop_id); await process.finish()。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[2]
WORKER = Path(__file__).with_name("round_memory_drill_worker.py")


class DrillProcess:
    def __init__(
        self, release: Path, directory: Path, mode: str, loop_id: str, entity_id=None
    ):
        self.directory = directory
        directory.mkdir()
        self._log = (directory / "process.log").open("w", encoding="utf-8")
        environment = dict(
            os.environ,
            PYTHONPATH=os.pathsep.join(
                (str(release / "backend/packages/harness"), str(release))
            ),
        )
        command = [sys.executable, str(WORKER), mode, loop_id, str(directory)]
        if entity_id is not None:
            command.extend(("--entity-id", entity_id))
        try:
            self._process = subprocess.Popen(
                command,
                cwd=release,
                env=environment,
                stdout=self._log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except BaseException:
            self._log.close()
            raise
        self.evidence = {
            "pid": self._process.pid,
            "mode": mode,
            "release": str(release),
            "killed": False,
        }

    async def signal(self, name: str):
        deadline = time.monotonic() + 25
        target = self.directory / f"{name}.json"
        while time.monotonic() < deadline:
            if target.exists():
                return json.loads(target.read_text(encoding="utf-8"))
            if self._process.poll() is not None:
                raise AssertionError(
                    (self.directory / "process.log").read_text(encoding="utf-8")
                )
            await asyncio.sleep(0.03)
        raise AssertionError(
            f"No {name}: {(self.directory / 'process.log').read_text(encoding='utf-8')}"
        )

    async def finish(self):
        await self.signal("done")
        code = await asyncio.to_thread(self._process.wait, 5)
        self.evidence["exit_code"] = code
        assert code == 0
        self._log.close()
        return self.evidence

    def close(self):
        if self._process.poll() is None:
            self._process.kill()
            self._process.wait(timeout=5)
            self.evidence["killed"] = True
        self.evidence["exit_code"] = self._process.returncode
        self._log.close()


def extract_legacy_release(directory: Path):
    archive = directory / "legacy.zip"
    directory.mkdir()
    with archive.open("wb") as output:
        subprocess.run(
            ["git", "archive", "--format=zip", "HEAD"],
            cwd=ROOT,
            stdout=output,
            check=True,
        )
    release = directory / "release"
    release.mkdir()
    with zipfile.ZipFile(archive) as packed:
        for member in packed.infolist():
            assert (
                (release / member.filename).resolve().is_relative_to(release.resolve())
            )
        packed.extractall(release)
    (directory / "commit.txt").write_text(
        subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        encoding="utf-8",
    )
    return release


async def memory_snapshot(sessions, loop_id: str):
    tables = (
        "loop_observations",
        "loop_decision_inputs",
        "loop_task_progress",
        "loop_progress_heads",
        "loop_progress_receipts",
        "loop_progress_work",
        "desktop_domain_results",
    )
    async with sessions() as session:
        result = {}
        for table in tables:
            rows = (
                await session.scalars(
                    text(f"SELECT to_jsonb(t) FROM {table} t WHERE loop_id=:loop"),
                    {"loop": loop_id},
                )
            ).all()
            result[table] = sorted(
                rows, key=lambda row: json.dumps(row, sort_keys=True)
            )
        publications = (
            await session.scalars(
                text(
                    "SELECT to_jsonb(p) FROM desktop_context_publications p JOIN desktop_context_revisions r USING(revision_id) JOIN desktop_threads c ON c.task_id=r.context_id JOIN agent_loops l ON l.workspace_id=c.workspace_id WHERE l.loop_id=:loop"
                ),
                {"loop": loop_id},
            )
        ).all()
        result["publications"] = sorted(
            publications, key=lambda row: json.dumps(row, sort_keys=True)
        )
        for name, query in {
            "context_revisions": "SELECT to_jsonb(r) FROM desktop_context_revisions r JOIN desktop_threads c ON c.task_id=r.context_id JOIN agent_loops l ON l.workspace_id=c.workspace_id WHERE l.loop_id=:loop",
            "revision_sources": "SELECT to_jsonb(s) FROM desktop_context_revision_sources s JOIN desktop_context_revisions r ON r.revision_id=s.target_revision_id JOIN desktop_threads c ON c.task_id=r.context_id JOIN agent_loops l ON l.workspace_id=c.workspace_id WHERE l.loop_id=:loop",
            "context_pointers": "SELECT jsonb_build_object('context_id', c.task_id, 'revision_id', c.current_revision_id) FROM desktop_threads c JOIN agent_loops l ON l.workspace_id=c.workspace_id WHERE l.loop_id=:loop",
        }.items():
            rows = (await session.scalars(text(query), {"loop": loop_id})).all()
            result[name] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
        return result


async def backup_restore(sessions, loop_id: str, directory: Path):
    container = os.environ["FOCUS_DRILL_CONTAINER"]
    label = await asyncio.to_thread(
        subprocess.check_output,
        [
            "docker",
            "inspect",
            container,
            "--format",
            '{{index .Config.Labels "codex.round-memory-drill"}}',
        ],
        text=True,
    )
    assert label.strip() == "20260930"
    source_url = make_url(os.environ["FOCUS_DATABASE_URL"])
    bindings = json.loads(
        await asyncio.to_thread(
            subprocess.check_output,
            [
                "docker",
                "inspect",
                container,
                "--format",
                "{{json .NetworkSettings.Ports}}",
            ],
            text=True,
        )
    )["5432/tcp"]
    assert source_url.host == "127.0.0.1" and source_url.username == "focus"
    assert any(
        binding["HostIp"] == "127.0.0.1" and int(binding["HostPort"]) == source_url.port
        for binding in bindings
    )
    assert source_url.database.startswith("focus_test_")
    database = f"focus_drill_restore_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    backup = directory / "memory.dump"
    before = await memory_snapshot(sessions, loop_id)
    with backup.open("wb") as output:
        await asyncio.to_thread(
            subprocess.run,
            [
                "docker",
                "exec",
                container,
                "pg_dump",
                "-U",
                "focus",
                "-Fc",
                source_url.database,
            ],
            stdout=output,
            check=True,
        )
    admin = create_engine(
        source_url.set(drivername="postgresql+psycopg", database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    restored = None
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database}"'))
        with backup.open("rb") as source:
            await asyncio.to_thread(
                subprocess.run,
                [
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "pg_restore",
                    "-U",
                    "focus",
                    "--exit-on-error",
                    "-d",
                    database,
                ],
                stdin=source,
                check=True,
            )
        restored = create_async_engine(source_url.set(database=database))
        assert await memory_snapshot(async_sessionmaker(restored), loop_id) == before
        return {
            "backup": str(backup),
            "restored_database": database,
            "rows": {table: len(rows) for table, rows in before.items()},
            "equal": True,
        }
    finally:
        if restored is not None:
            await restored.dispose()
        assert (
            database.startswith("focus_drill_restore_")
            and database != source_url.database
        )
        with admin.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
            )
        admin.dispose()
