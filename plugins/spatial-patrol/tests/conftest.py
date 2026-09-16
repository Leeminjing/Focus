"""本文件为 spatial-patrol 测试套件提供隔离 PostgreSQL 生命周期。

输入为可选的 ``FOCUS_TEST_DATABASE_URL`` 或 Focus 默认测试 PostgreSQL 地址；前者表示
调用方管理的专用数据库，后者会派生进程唯一数据库。输出为已升级到当前 Alembic head
的 ``FOCUS_DATABASE_URL``。具体工作流为在测试收集前绑定隔离 URL，在 session fixture
中创建并迁移数据库，测试结束后强制断开并删除自动创建的数据库。例如，直接运行
``pytest plugins/spatial-patrol/tests`` 不需要预先启动 Focus 应用或污染桌面正式库。
"""

from __future__ import annotations

import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy.engine import make_url


_DEFAULT_DATABASE_URL = "postgresql+asyncpg://focus:qweasdzxc123@127.0.0.1:7221/focus"
_external_database_url = os.environ.get("FOCUS_TEST_DATABASE_URL")
_database_source_url = make_url(
    _external_database_url
    or os.environ.get("FOCUS_DATABASE_URL")
    or _DEFAULT_DATABASE_URL
)
_managed_database = _external_database_url is None
_database_name = (
    _database_source_url.database
    if not _managed_database
    else f"focus_plugin_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"
)
if not _database_name or _database_name == "focus":
    raise RuntimeError("FOCUS_TEST_DATABASE_URL 必须指向独立测试库，不能使用桌面正式库 'focus'")
_database_url = _database_source_url.set(database=_database_name)
os.environ["FOCUS_DATABASE_URL"] = _database_url.render_as_string(hide_password=False)


@pytest.fixture(scope="session", autouse=True)
def isolated_plugin_database():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    admin_url = _database_source_url.set(
        drivername="postgresql+psycopg",
        database="postgres",
        query={},
    )
    admin_engine = create_engine(admin_url)
    created = False
    try:
        if _managed_database:
            with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.execute(text(f'CREATE DATABASE "{_database_name}"'))
            created = True
        migrations = (
            Path(__file__).parents[3]
            / "backend"
            / "packages"
            / "harness"
            / "focus"
            / "persistence"
            / "migrations"
            / "alembic.ini"
        )
        command.upgrade(Config(str(migrations)), "head")
        from focus.config import get_app_config

        app_config = get_app_config("config.yaml")
        if app_config.database is None:
            raise RuntimeError("插件数据库测试要求 PostgreSQL 配置")
        app_config.database.url = os.environ["FOCUS_DATABASE_URL"]
        yield
    finally:
        if created:
            with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.execute(text(f'DROP DATABASE IF EXISTS "{_database_name}" WITH (FORCE)'))
        admin_engine.dispose()
