"""backend/tests 共享 fixture：隔离 PostgreSQL 与统一轮询等待入口。"""

import os
from pathlib import Path
import time
import uuid

import pytest
from sqlalchemy.engine import make_url


_DESKTOP_DATABASE_URL = "postgresql+asyncpg://focus:qweasdzxc123@127.0.0.1:7221/focus"
_external_test_database_url = os.environ.get("FOCUS_TEST_DATABASE_URL")
_database_source_url = make_url(
    _external_test_database_url
    or os.environ.get("FOCUS_DATABASE_URL")
    or _DESKTOP_DATABASE_URL
)
_managed_test_database = _external_test_database_url is None
_test_database_name = (
    _database_source_url.database
    if not _managed_test_database
    else f"focus_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"
)
if not _test_database_name or _test_database_name == "focus":
    raise RuntimeError("FOCUS_TEST_DATABASE_URL 必须指向独立测试库，不能使用桌面正式库 'focus'")
_test_database_url = _database_source_url.set(database=_test_database_name)

# conftest 会在测试模块收集前导入。先覆盖环境变量，避免模块级 engine 或 Gateway
# 在 import 阶段捕获桌面程序的持久数据库 URL。
os.environ["FOCUS_DATABASE_URL"] = _test_database_url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def isolated_postgres_database():
    """为 PostgreSQL 集成测试创建、迁移并最终销毁独立数据库。"""

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
        if _managed_test_database:
            with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.execute(text(f'CREATE DATABASE "{_test_database_name}"'))
            created = True

        migrations = (
            Path(__file__).parents[1]
            / "packages"
            / "harness"
            / "focus"
            / "persistence"
            / "migrations"
            / "alembic.ini"
        )
        command.upgrade(Config(str(migrations)), "head")
        yield
    finally:
        if created:
            with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.execute(text(f'DROP DATABASE IF EXISTS "{_test_database_name}" WITH (FORCE)'))
        admin_engine.dispose()


@pytest.fixture
def wait_until():
    """轮询等待条件成立；超时抛 AssertionError。"""

    def _wait(condition, timeout=20, interval=0.2, message=""):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(interval)
        raise AssertionError(message or f"条件在 {timeout}s 内未成立")

    return _wait


def _memory_status(service, run_id):
    """运行中的状态（pending/running）只存在于内存 RunManager；DB 在 worker 结束才同步。"""
    record = service.run_manager.get(run_id)
    return record.status.value if record else None


@pytest.fixture
def wait_for_memory_status(wait_until):
    """等待 run 内存态收敛（RunManager 权威，DB 由 worker 结束后薄任务同步）。"""

    def _wait(client, service, run_id, statuses, timeout=20):
        def check():
            status = client.portal.call(_memory_status, service, run_id)
            return status in statuses

        wait_until(
            check, timeout=timeout,
            message=f"run {run_id} 未在 {timeout}s 内到达 {statuses}",
        )

    return _wait
