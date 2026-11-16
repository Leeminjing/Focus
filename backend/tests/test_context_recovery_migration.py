r"""本文件对外提供 Context recovery opportunity 增量迁移的 PostgreSQL 往返验证。

输入为已迁移到 head 的隔离数据库；输出为 authority/freshness/consumption 列、唯一约束与 downgrade/upgrade 恢复断言。具体工作流为
执行 `head → 5f6a7b8c9d0e → head`，确认只增删恢复表且最终 head 可重建。示例：
`pytest backend/tests/test_context_recovery_migration.py -q`。
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url


def test_context_recovery_opportunity_migration_round_trip(isolated_postgres_database) -> None:
    migrations = (
        Path(__file__).parents[1]
        / "packages"
        / "harness"
        / "focus"
        / "persistence"
        / "migrations"
        / "alembic.ini"
    )
    config = Config(str(migrations))
    database_url = make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg")
    engine = create_engine(database_url)
    expected = {
        "opportunity_id",
        "source_frontier_hash",
        "goal_revision",
        "workspace_revision",
        "authority_revision",
        "grant_id",
        "grant_revision",
        "compiler_version",
        "expires_at",
        "status",
        "consumed_by_decision_id",
        "result",
    }
    try:
        assert expected <= {
            column["name"]
            for column in inspect(engine).get_columns("loop_context_recovery_opportunities")
        }
        command.downgrade(config, "5f6a7b8c9d0e")
        assert "loop_context_recovery_opportunities" not in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        assert expected <= {
            column["name"]
            for column in inspect(engine).get_columns("loop_context_recovery_opportunities")
        }
    finally:
        command.upgrade(config, "head")
        engine.dispose()
