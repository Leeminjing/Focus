r"""本文件验证 Context 当前状态、任务输入快照与 Portfolio publication 的唯一运行时权威。

输入为运行时 Python 源码、SQLAlchemy metadata、最终迁移后的 PostgreSQL schema 与 AppConfig；输出为
旧 definition/source 表、迁移 adapter、双读开关和旧一对一 publication adapter 均不可达的断言。
具体工作流为扫描生产包并检查注册表和真实数据库表，防止后续改动重新引入双主路径。示例：
`pytest backend/tests/test_context_revision_authority.py`。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from focus.config.app_config import AppConfig
from focus.persistence.base import Base
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

import backend.app.desktop.persistence.registry  # noqa: F401

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")

_REMOVED_TABLES = {
    "desktop_context_definitions",
    "desktop_context_sources",
    "desktop_context_revisions",
    "desktop_context_revision_sources",
    "desktop_context_publications",
    "desktop_context_states",
    "desktop_context_input_snapshots",
}
_REMOVED_RUNTIME_NAMES = {
    "ContextEvolutionMigrationAdapter",
    "LegacyCurationProgramAdapter",
    "DesktopContextDefinition",
    "DesktopContextSource",
}


def test_runtime_has_one_revision_authority_path() -> None:
    root = Path(__file__).resolve().parents[1]
    production_roots = (root / "app", root / "packages" / "harness" / "focus")
    offenders: list[str] = []
    for production_root in production_roots:
        for source in production_root.rglob("*.py"):
            if "migrations" in source.parts:
                continue
            text = source.read_text(encoding="utf-8")
            removed = sorted(name for name in _REMOVED_RUNTIME_NAMES if name in text)
            if removed:
                offenders.append(f"{source.relative_to(root)}: {', '.join(removed)}")
    assert offenders == []
    assert _REMOVED_TABLES.isdisjoint(Base.metadata.tables)
    assert "context_evolution" not in AppConfig.model_fields


def test_final_schema_drops_identity_level_context_authority() -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        try:
            async with engine.connect() as connection:
                tables = set(
                    await connection.run_sync(
                        lambda sync_connection: inspect(sync_connection).get_table_names()
                    )
                )
            async with engine.connect() as connection:
                thread_columns = await connection.run_sync(lambda sync_connection: {
                    column["name"] for column in inspect(sync_connection).get_columns("desktop_threads")
                })
                state_constraints = await connection.run_sync(lambda sync_connection:
                    inspect(sync_connection).get_unique_constraints("desktop_context_inputs"))
                snapshot_constraints = await connection.run_sync(lambda sync_connection:
                    inspect(sync_connection).get_unique_constraints("desktop_context_input_owners"))
            assert {"authored_messages", "execution_messages", "definition_hash", "projection_hash", "projection_status"}.isdisjoint(thread_columns)
            assert "current_revision_id" in thread_columns
            assert not any(item["column_names"] == ["context_id"] for item in state_constraints)
            assert any(item["column_names"] == ["owner_kind", "owner_id", "revision_id"] for item in snapshot_constraints)
            assert _REMOVED_TABLES.isdisjoint(tables)
            assert {
                "desktop_context_inputs",
                "desktop_context_input_owners",
                "curation_portfolio_revisions",
                "curation_portfolio_lane_candidates",
            }.issubset(tables)
        finally:
            await engine.dispose()

    asyncio.run(run())
