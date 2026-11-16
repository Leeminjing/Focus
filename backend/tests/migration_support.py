"""本文件对外提供 isolated_migration_database 的独立 PostgreSQL DDL 验证环境。

输入为会话隔离数据库的连接和单个迁移测试；输出为独立临时数据库，避免读取或降级其它用例产生的 V2 权威事实。
具体工作流为只为 DDL／回填用例创建随机测试库、升级被测历史迁移断面、测试后仅删除该确切库并恢复环境变量。
示例：pytest 的 migration/backfill 用例自动启用本 fixture；生产数据库不作为测试目标。
"""

import inspect
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url


@pytest.fixture(autouse=True)
def isolated_migration_database(request, monkeypatch):
    source = inspect.getsource(request.function)
    if "command.downgrade" not in source and "backfill" not in request.path.name:
        yield
        return
    request.getfixturevalue("isolated_postgres_database")
    original = make_url(os.environ["FOCUS_DATABASE_URL"])
    name = "focus_migration_" + uuid.uuid4().hex
    admin = original.set(drivername="postgresql", database="postgres", query={}).render_as_string(hide_password=False)
    target = original.set(database=name, query={})
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    monkeypatch.setenv("FOCUS_DATABASE_URL", target.render_as_string(hide_password=False))
    legacy_boundaries = {
        'test_context_recovery_migration.py': '6a7b8c9d0e1f',
        'test_context_revision_backfill.py': 'b0c1d2e3f4a5',
        'test_context_revision_backfill_validation.py': 'b2c3d4e5f6a7',
        'test_loop_event_journal.py': 'b5c6d7e8f9a0',
        'test_loop_execution_receipt_migration.py': 'e58b9c0d1e2f',
        'test_loop_materialized_facts.py': '0a1b2c3d4e5f',
        'test_loop_mission_contract.py': 'a4b5c6d7e8f9',
        'test_semantic_context_migration.py': '5f6a7b8c9d0e',
        'test_task_memory_migration.py': '8c9d0e1f2a3b',
        'test_workspace_patrol_migration.py': 'f69c0d1e2f3a',
    }
    config = Config(str(Path(__file__).parents[1] / "packages/harness/focus/persistence/migrations/alembic.ini"))
    try:
        # Legacy reversible migration checks stop before history retirement.
        # The forward-only current-state upgrade has its own populated-old-head test.
        command.upgrade(config, legacy_boundaries.get(request.path.name, "4a5b6c7d8e9f"))
        yield
    finally:
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()", (name,))
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
