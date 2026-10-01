"""本文件对外提供 isolated_migration_database 的独立 PostgreSQL DDL 验证环境。

输入为会话隔离数据库的连接和单个迁移测试；输出为独立临时数据库，避免读取或降级其它用例产生的 V2 权威事实。
具体工作流为只为 DDL／回填用例创建随机测试库、升级正式 head、测试后仅删除该确切库并恢复环境变量。
示例：pytest 的 migration/backfill 用例自动启用本 fixture；生产数据库不作为测试目标。
"""

import inspect
import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
import psycopg
from psycopg import sql
import pytest
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
    config = Config(str(Path(__file__).parents[1] / "packages/harness/focus/persistence/migrations/alembic.ini"))
    try:
        command.upgrade(config, "head")
        yield
    finally:
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()", (name,))
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
