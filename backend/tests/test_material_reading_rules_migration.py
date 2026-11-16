"""Verify removal of reading rules preserves materials and restores schema defaults on rollback."""

import os
import uuid
from pathlib import Path

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
        command.upgrade(config, "4a5b6c7d8e9f")
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
        command.upgrade(config, "4a5b6c7d8e9f")
    finally:
        engine.dispose()


def test_remove_image_gate_preserves_material_notes(isolated_postgres_database):
    from backend.app.desktop.persistence.models import DesktopRun

    config = Config(str(Path(__file__).parents[1] / "packages/harness/focus/persistence/migrations/alembic.ini"))
    command.downgrade(config, "28c3d4e5f6a7")
    engine = create_engine(make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg", query={}))
    workspace, task, run, binding = [uuid.uuid4().hex for _ in range(4)]
    note = "核对尺寸\n保留原文 <tag>"
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO desktop_workspaces (workspace_id,path,display_name) VALUES (:w,:p,'migration')"),
                {"w": workspace, "p": "migration-" + workspace})
            connection.execute(text("INSERT INTO desktop_threads (task_id,workspace_id,thread_id,title) VALUES (:t,:w,:t,'material')"),
                {"t": task, "w": workspace})
            connection.execute(DesktopRun.__table__.insert().values(
                run_id=run, task_id=task, agent_id="main:" + task, kind="main", status="success", input_messages=[]))
            connection.execute(text("INSERT INTO desktop_run_material_bindings (binding_id,run_id,task_id,message_id,material_id_snapshot,relative_path_snapshot,digest_snapshot,material_kind_snapshot,note,ordinal,must_view_requested) VALUES (:b,:r,:t,'message','image','shot.png',:d,'image',:n,0,true)"),
                {"b": binding, "r": run, "t": task, "d": "a" * 64, "n": note})
        command.upgrade(config, "4a5b6c7d8e9f")
        with engine.connect() as connection:
            columns = {column["name"] for column in inspect(connection).get_columns("desktop_run_material_bindings")}
            assert "must_view_requested" not in columns
            row = connection.execute(text("SELECT material_id_snapshot,note,ordinal FROM desktop_run_material_bindings WHERE binding_id=:b"), {"b": binding}).one()
            assert tuple(row) == ("image", note, 0)
        command.downgrade(config, "28c3d4e5f6a7")
        with engine.connect() as connection:
            row = connection.execute(text("SELECT must_view_requested,note FROM desktop_run_material_bindings WHERE binding_id=:b"), {"b": binding}).one()
            assert tuple(row) == (False, note)
        command.upgrade(config, "4a5b6c7d8e9f")
    finally:
        engine.dispose()
