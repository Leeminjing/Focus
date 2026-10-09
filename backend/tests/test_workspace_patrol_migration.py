"""本文件对外提供工作区 Patrol 的旧数据升级与受保护回退回归。

输入为隔离数据库中的上一版本 schema 和旧完整 Mission；输出为默认旧模式、原文/hash 保持及新增唯一约束断言。
工作流为仅在 runtime 测试库回退一代，写真实旧列，再升级并验证；新模式数据存在时回退必须失败关闭。
示例：python -m pytest backend/tests/test_workspace_patrol_migration.py -q。
"""

import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url


def test_old_data_is_preserved_and_new_mode_downgrade_is_guarded(
    tmp_path, runtime_postgres_database
):
    configuration = Config(
        str(Path(__file__).parents[1] / "packages/harness/focus/persistence/migrations/alembic.ini")
    )
    engine = create_engine(
        make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg")
    )
    workspace, context, loop, mission = [uuid.uuid4().hex for _ in range(4)]
    try:
        command.downgrade(configuration, "e58b9c0d1e2f")
        assert "interaction_mode" not in {
            column["name"] for column in inspect(engine).get_columns("agent_loops")
        }
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO desktop_workspaces(workspace_id,path,display_name) VALUES(:id,:path,'旧工作区')"
                ),
                {"id": workspace, "path": str(tmp_path)},
            )
            connection.execute(
                text(
                    "INSERT INTO desktop_threads(task_id,workspace_id,thread_id,title) VALUES(:id,:workspace,:thread,'旧会话')"
                ),
                {"id": context, "workspace": workspace, "thread": uuid.uuid4().hex},
            )
            connection.execute(
                text(
                    "INSERT INTO agent_loops(loop_id,workspace_id,initial_context_id,holder_id,status) VALUES(:id,:workspace,:context,'旧持有人','draft')"
                ),
                {"id": loop, "workspace": workspace, "context": context},
            )
            connection.execute(
                text(
                    "INSERT INTO loop_mission_revisions(mission_revision_id,loop_id,revision,outcome,authored_by) VALUES(:id,:loop,1,'旧完整目标','user')"
                ),
                {"id": mission, "loop": loop},
            )
        command.upgrade(configuration, "head")
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT interaction_mode,status FROM agent_loops WHERE loop_id=:id"),
                {"id": loop},
            ).one()
            assert tuple(row) == ("context_loop", "draft")
            row = connection.execute(
                text(
                    "SELECT outcome,input_sources FROM loop_mission_revisions WHERE mission_revision_id=:id"
                ),
                {"id": mission},
            ).one()
            assert tuple(row) == ("旧完整目标", {})
        assert next(
            column
            for column in inspect(engine).get_columns("loop_mission_revisions")
            if column["name"] == "outcome"
        )["nullable"]
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE agent_loops SET interaction_mode='workspace_patrol' WHERE loop_id=:id"
                ),
                {"id": loop},
            )
        with pytest.raises(RuntimeError, match="工作区 Patrol 数据存在"):
            command.downgrade(configuration, "e58b9c0d1e2f")
        assert "interaction_mode" in {
            column["name"] for column in inspect(engine).get_columns("agent_loops")
        }
    finally:
        engine.dispose()
