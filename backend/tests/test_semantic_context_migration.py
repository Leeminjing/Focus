r"""本文件对外提供 semantic Context derivation 增量迁移的真实 PostgreSQL 往返测试。

输入为已迁移到 head 的隔离数据库；输出为新冻结合同列、表达式索引、downgrade 兼容和再次 upgrade 恢复断言。
具体工作流为执行 `head → 3d4e5f6a7b8c → head` 并检查 loop_context_expansions schema。示例：
`pytest backend/tests/test_semantic_context_migration.py -q`。
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url


def test_semantic_context_derivation_migration_round_trip(isolated_postgres_database) -> None:
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
        "work_spec",
        "manifest_ids",
        "source_frontier",
        "resolution",
        "evidence_frontier",
        "stage_identities",
        "compiled_plan",
        "definition_hash",
        "planner_version",
        "projector_version",
        "resolver_version",
    }
    try:
        assert expected <= {column["name"] for column in inspect(engine).get_columns("loop_context_expansions")}
        command.downgrade(config, "3d4e5f6a7b8c")
        assert not expected & {column["name"] for column in inspect(engine).get_columns("loop_context_expansions")}
        command.upgrade(config, "head")
        assert expected <= {column["name"] for column in inspect(engine).get_columns("loop_context_expansions")}
        indexes = {item["name"] for item in inspect(engine).get_indexes("loop_context_expansions")}
        assert {
            "ix_loop_context_expansions_work_spec_id",
            "ix_loop_context_expansions_resolution_id",
            "ix_loop_context_expansions_definition_hash",
        } <= indexes
    finally:
        command.upgrade(config, "head")
        engine.dispose()
