r"""本文件对外提供索引独立进程重启与 additive migration 的 PostgreSQL 演练。

输入为真实隔离数据库、冻结输入和独立子进程；输出为强杀提交前不泄漏、提交后零调用恢复及迁移不改权威记忆的断言。
工作流为只在测试目录启动隐藏子进程，等待信号后强杀，另一个新进程重放并继续继承；迁移使用独立随机数据库。
综合参与真实计费及进程边界；崩溃未知预留仍占额度，结算后可升降级，bf2a3b4c5d6e 回撤新综合 artifact 保留权威记忆。
示例：python -m pytest backend/tests/test_incremental_index_recovery.py -q。
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from backend.app.desktop.agent_loop.context_expansion.models import (
    LoopIndexBudgetReservation,
    LoopSemanticIndexArtifact,
)
from backend.tests.incremental_index_support import messages, observation, revision
from backend.tests.test_incremental_revision_index import exercise

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def process(directory, mode, obs):
    directory.mkdir()
    input_path = directory / "input.json"
    input_path.write_text(json.dumps(vars(obs)), encoding="utf-8")
    output = (directory / "process.log").open("w", encoding="utf-8")
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "backend.tests.incremental_index_process",
            mode,
            str(input_path),
            str(directory),
        ],
        stdout=output,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    return child, output


async def signal(directory, name, child):
    deadline = time.monotonic() + 30
    path = directory / f"{name}.json"
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        if child.poll() is not None:
            raise AssertionError(
                (directory / "process.log").read_text(encoding="utf-8")
            )
        await asyncio.sleep(0.03)
    raise AssertionError(f"process signal timeout: {directory}")


@pytest.mark.parametrize("boundary", ["before_commit", "after_commit"])
def test_independent_process_kill_and_persisted_inheritance(tmp_path, boundary):
    async def run(sessions, seed):
        first = await revision(sessions, seed["context_id"], messages(12))
        directory = tmp_path / "killed"
        child, log = process(directory, boundary, observation(seed, first))
        try:
            state = await signal(
                directory, "prepared" if boundary == "before_commit" else "done", child
            )
            assert state["calls"] == 3
        finally:
            child.kill()
            await asyncio.to_thread(child.wait, 5)
            log.close()
        async with sessions() as session:
            stored = await session.scalar(
                select(LoopSemanticIndexArtifact.index_id).where(
                    LoopSemanticIndexArtifact.revision_id == first.ref.revision_id
                )
            )
            assert bool(stored) == (boundary == "after_commit")
            pending = (
                await session.scalars(
                    select(LoopIndexBudgetReservation).where(
                        LoopIndexBudgetReservation.loop_id == seed["loop_id"],
                        LoopIndexBudgetReservation.settled_at.is_(None),
                    )
                )
            ).all()
            assert bool(pending) == (boundary == "before_commit")
            if pending:
                assert pending[0].model_calls == 3 and pending[0].grant_revision == 1
        directory = tmp_path / "restarted"
        child, log = process(directory, "build", observation(seed, first))
        try:
            replay = await signal(directory, "done", child)
            assert await asyncio.to_thread(child.wait, 5) == 0
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(5)
            log.close()
        assert replay["calls"] == (0 if boundary == "after_commit" else 3)
        if stored:
            assert replay["index_id"] == stored
        new = await revision(sessions, seed["context_id"], messages(13), parent=first)
        directory = tmp_path / "descendant"
        child, log = process(directory, "build", observation(seed, new))
        try:
            descendant = await signal(directory, "done", child)
            assert await asyncio.to_thread(child.wait, 5) == 0
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(5)
            log.close()
        assert (
            descendant["mode"] == "incremental"
            and descendant["reused"] == 1
            and descendant["calls"] == 3
        )
        assert descendant["pid"] != replay["pid"] != state["pid"]

    exercise(tmp_path, run)


def test_projection_record_migration_round_trip_preserves_control_tables(
    monkeypatch, tmp_path
):
    original = make_url(os.environ["FOCUS_DATABASE_URL"])
    name = f"focus_index_migration_{uuid.uuid4().hex}"
    admin = create_engine(
        original.set(drivername="postgresql+psycopg", database="postgres", query={})
    )
    engine = None
    created = False
    config = Config(
        str(
            Path(__file__).parents[1]
            / "packages/harness/focus/persistence/migrations/alembic.ini"
        )
    )
    try:
        with admin.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        created = True
        target = original.set(database=name)
        monkeypatch.setenv(
            "FOCUS_DATABASE_URL", target.render_as_string(hide_password=False)
        )
        command.upgrade(config, "8c9d0e1f2a3b")
        from datetime import UTC, datetime

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from backend.tests.test_agent_loop_round_liveness import _seed_loop

        async def seed_control():
            database = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
            try:
                from backend.tests.incremental_index_support import messages, revision

                sessions = async_sessionmaker(database, expire_on_commit=False)
                fixture = await _seed_loop(
                    sessions,
                    tmp_path,
                    label="index-migration",
                    started_at=datetime.now(UTC),
                )
                target_revision = await revision(
                    sessions, fixture["context_id"], messages(12)
                )
                return fixture, target_revision
            finally:
                await database.dispose()

        fixture, target_revision = asyncio.run(seed_control())
        engine = create_engine(target.set(drivername="postgresql+psycopg", query={}))
        tables = [
            "desktop_context_revisions",
            "desktop_context_publications",
            "loop_task_progress",
            "loop_observations",
        ]

        def snapshot():
            with engine.connect() as c:
                return {
                    t: c.execute(text(f"SELECT to_jsonb(x) FROM {t} x")).scalars().all()
                    for t in tables
                }

        before = snapshot()
        assert before["desktop_context_revisions"] and before["loop_task_progress"]
        command.upgrade(config, "head")
        with engine.connect() as c:
            assert c.scalar(
                text("SELECT to_regclass('loop_segment_projection_records')")
            )
        assert snapshot() == before

        async def seed_indexes():
            from backend.app.desktop.agent_loop.context_expansion.artifact_repository import (
                SemanticDerivationArtifactRepository,
            )
            from backend.app.desktop.agent_loop.context_expansion.semantic_indexer import (
                RevisionSemanticIndexer,
            )
            from backend.tests.incremental_index_support import ModelProbe, observation

            database = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
            sessions = async_sessionmaker(database, expire_on_commit=False)
            try:
                built = (
                    await ModelProbe()
                    .service(sessions)
                    .build(observation(fixture, target_revision))
                )
                assert built.blocker_code is None, built.blocker_summary
                current = built.indexes[0]
                legacy = RevisionSemanticIndexer().index(
                    source=target_revision.ref,
                    source_content_hash=target_revision.content_hash,
                    context_role="primary",
                    active_objective="proof-less artifact retained on downgrade",
                    raw_messages=target_revision.execution_messages,
                )
                async with sessions.begin() as session:
                    await SemanticDerivationArtifactRepository().put_index(
                        session, legacy
                    )
                return current.index_id, legacy.index_id
            finally:
                await database.dispose()

        current_id, legacy_id = asyncio.run(seed_indexes())
        with engine.connect() as c:
            assert (
                c.scalar(text("SELECT count(*) FROM loop_segment_projection_records"))
                == 1
            )
        assert snapshot() == before
        pending_id = uuid.uuid4().hex
        with engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO loop_index_budget_reservations (reservation_id, loop_id, grant_revision, model_calls, input_tokens, output_tokens) VALUES (:id, :loop, 1, 1, 1, 1)"
                ),
                {"id": pending_id, "loop": fixture["loop_id"]},
            )
        with pytest.raises(RuntimeError, match="unsettled index budget"):
            command.downgrade(config, "8c9d0e1f2a3b")
        with engine.begin() as c:
            assert (
                c.scalar(text("SELECT version_num FROM alembic_version"))
                == "bf2a3b4c5d6e"
            )
            c.execute(
                text(
                    "UPDATE loop_index_budget_reservations SET settled_at = now(), actual_usage = '{}'::jsonb WHERE reservation_id = :id"
                ),
                {"id": pending_id},
            )
        command.downgrade(config, "8c9d0e1f2a3b")
        with engine.connect() as c:
            assert (
                c.scalar(text("SELECT to_regclass('loop_segment_projection_records')"))
                is None
            )
            remaining = set(
                c.execute(
                    text("SELECT index_id FROM loop_semantic_index_artifacts")
                ).scalars()
            )
            assert current_id not in remaining and legacy_id in remaining
        command.upgrade(config, "head")
        assert snapshot() == before
    finally:
        if engine:
            engine.dispose()
        if created:
            with admin.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as connection:
                connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()
