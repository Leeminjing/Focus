"""本文件验证材料 owner 迁移的真实 PostgreSQL 约束与可逆边界。

输入为独立迁移数据库中的旧任务材料；输出为旧数据保留、排他归属、路径/上传身份唯一和有数据拒绝降级的断言。
工作流先降到上一版写入旧行，再升级并用真实约束验证；测试 fixture 只销毁本例临时库。
示例：python -m pytest backend/tests/test_workspace_material_migration.py。
"""

import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
import pytest


def test_workspace_material_migration_preserves_legacy_and_refuses_destructive_downgrade(isolated_postgres_database):
    config = Config(str(Path(__file__).parents[1] / "packages/harness/focus/persistence/migrations/alembic.ini"))
    command.downgrade(config, "b81e2f3a4c5d")
    engine = create_engine(make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg", query={}))
    workspace, task, legacy = [uuid.uuid4().hex for _ in range(3)]
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO desktop_workspaces (workspace_id,path,display_name) VALUES (:w,:p,'migration')"),
            {"w": workspace, "p": "migration-" + workspace})
        connection.execute(text("INSERT INTO desktop_threads (task_id,workspace_id,thread_id,title) VALUES (:t,:w,:t,'legacy')"),
            {"t": task, "w": workspace})
        connection.execute(text("INSERT INTO desktop_materials (material_id,task_id,relative_path,reading_mode,instruction_mode,retention) VALUES (:m,:t,'same.txt','full','reference','removable')"),
            {"m": legacy, "t": task})
    command.upgrade(config, "head")
    statement = text("INSERT INTO desktop_materials (material_id,task_id,workspace_id,relative_path,intake_upload_key,reading_mode,instruction_mode,retention) VALUES (:m,:t,:w,:p,:k,'full','reference','removable')")
    try:
        with engine.begin() as connection:
            preserved = connection.execute(text("SELECT task_id,workspace_id,relative_path,intake_upload_key FROM desktop_materials WHERE material_id=:m"), {"m": legacy}).one()
            assert tuple(preserved) == (task, None, "same.txt", None)
        for owner in ({"t": None, "w": None}, {"t": task, "w": workspace}):
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(statement, {"m": uuid.uuid4().hex, "p": "invalid.txt", "k": None, **owner})
        material = uuid.uuid4().hex
        with engine.begin() as connection:
            connection.execute(statement, {"m": material, "t": None, "w": workspace, "p": "same.txt", "k": "a" * 65})
        for path, key in (("same.txt", "b" * 65), ("another.txt", "a" * 65)):
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(statement, {"m": uuid.uuid4().hex, "t": None, "w": workspace, "p": path, "k": key})
        with pytest.raises(RuntimeError, match="存在工作区材料"):
            command.downgrade(config, "b81e2f3a4c5d")
        with engine.begin() as connection:
            assert connection.execute(text("SELECT workspace_id FROM desktop_materials WHERE material_id=:m"), {"m": material}).scalar() == workspace
            connection.execute(text("DELETE FROM desktop_materials WHERE material_id=:m"), {"m": material})
        command.downgrade(config, "b81e2f3a4c5d")
        with engine.begin() as connection:
            assert connection.execute(text("SELECT task_id FROM desktop_materials WHERE material_id=:m"), {"m": legacy}).scalar() == task
    finally:
        engine.dispose()
