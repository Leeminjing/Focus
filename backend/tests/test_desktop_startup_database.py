"""原生启动的数据库版本检查必须只读，且能识别空库、单头和多头。"""
import asyncio
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

from alembic.config import Config
from sqlalchemy import create_engine, text


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("desktop_database_state", ROOT / "desktop/database-state.py")
database_state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(database_state)


def test_empty_and_branched_revision_states_are_read_only():
    engine = create_engine("sqlite://")
    with engine.connect() as connection:
        assert database_state.revision_status(connection, ["head"]) == "pending"
        assert connection.execute(text("SELECT name FROM sqlite_master WHERE name='alembic_version'")).all() == []
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('a'), ('b')"))
        assert database_state.revision_status(connection, ["b", "a"]) == "current"
        assert database_state.revision_status(connection, ["c"]) == "pending"
        assert connection.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num")).scalars().all() == ["a", "b"]
    engine.dispose()


def test_actual_postgres_head_is_current(isolated_postgres_database):
    config = Config(str(ROOT / "backend/packages/harness/focus/persistence/migrations/alembic.ini"))
    assert asyncio.run(database_state.database_status(os.environ["FOCUS_DATABASE_URL"], config)) == "current"


def test_refused_connection_is_unavailable():
    config = Config(str(ROOT / "backend/packages/harness/focus/persistence/migrations/alembic.ini"))
    assert asyncio.run(database_state.database_status("postgresql+asyncpg://unused:unused@127.0.0.1:1/unused", config)) == "unavailable"


def test_reading_revision_graph_does_not_import_gateway_or_desktop():
    result = subprocess.run(
        [sys.executable, "-c", "from alembic.config import Config; from alembic.script import ScriptDirectory; import sys; "
         f"assert ScriptDirectory.from_config(Config({str(ROOT / 'backend/packages/harness/focus/persistence/migrations/alembic.ini')!r})).get_heads(); "
         "assert not any(name.startswith('backend.app.desktop') for name in sys.modules)"],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
