"""Verify removal of reading rules preserves materials and restores schema defaults on rollback."""

import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url


def test_remove_material_reading_rules_preserves_materials(isolated_postgres_database):
    config = Config(str(Path(__file__).parents[1] / "packages/harness/focus/persistence/migrations/alembic.ini"))
    command.downgrade(config, "17b2c3d4e5f6")
    engine = create_engine(make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg", query={}))
    workspace, task, material = [uuid.uuid4().hex for _ in range(3)]
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO desktop_workspaces (workspace_id,path,display_name) VALUES (:w,:p,'migration')"),
                {"w": workspace, "p": "migration-" + workspace})
            connection.execute(text("INSERT INTO desktop_threads (task_id,workspace_id,thread_id,title) VALUES (:t,:w,:t,'material')"),
                {"t": task, "w": workspace})
            connection.execute(text("INSERT INTO desktop_materials (material_id,task_id,relative_path,reading_mode,instruction_mode,retention,digest,git_ref) VALUES (:m,:t,'notes.md','rough','strict','irreplaceable',:d,'refs/focus/materials/saved')"),
                {"m": material, "t": task, "d": "a" * 64})
        command.upgrade(config, "head")
        with engine.connect() as connection:
            columns = {column["name"] for column in inspect(connection).get_columns("desktop_materials")}
            assert "reading_mode" not in columns
            assert "instruction_mode" not in columns
            row = connection.execute(text("SELECT task_id,relative_path,retention,digest,git_ref FROM desktop_materials WHERE material_id=:m"), {"m": material}).one()
            assert tuple(row) == (task, "notes.md", "irreplaceable", "a" * 64, "refs/focus/materials/saved")
        command.downgrade(config, "17b2c3d4e5f6")
        with engine.connect() as connection:
            row = connection.execute(text("SELECT reading_mode,instruction_mode,retention FROM desktop_materials WHERE material_id=:m"), {"m": material}).one()
            assert tuple(row) == ("full", "reference", "irreplaceable")
        command.upgrade(config, "head")
    finally:
        engine.dispose()
