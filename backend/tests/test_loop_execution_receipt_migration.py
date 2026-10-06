"""本文件对外提供 Loop 执行父链及消费审计增量迁移的 PostgreSQL 验证。

输入为独立随机迁移数据库及旧版执行审计；输出为历史未知来源保持、外键限制和有审计时拒绝降级的断言。
具体工作流为经正式 Alembic 入口降级再升级，核对历史值与 RESTRICT 约束；有父链或 receipt 时验证事务回滚保留全部数据。
示例：pytest backend/tests/test_loop_execution_receipt_migration.py；不连接生产或原生验收数据库。
"""

from contextlib import contextmanager
import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import backend.app.desktop.persistence_registry
from backend.app.desktop.models import DesktopWorkspace, DesktopThread, DesktopRun, ModelAttemptAudit


@contextmanager
def _database():
    config = Config(str(Path(__file__).parents[1] / "packages/harness/focus/persistence/migrations/alembic.ini"))
    engine = create_engine(make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg"))
    try:
        yield config, engine
    finally:
        engine.dispose()


def _seed(engine, tmp_path):
    workspace_id, task_id, run_id = (uuid.uuid4().hex for _ in range(3))
    with Session(engine) as session, session.begin():
        session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="migration"))
        session.flush()
        session.add(DesktopThread(task_id=task_id, workspace_id=workspace_id, thread_id="legacy", title="legacy"))
        session.flush()
        session.add(DesktopRun(run_id=run_id, task_id=task_id, agent_id="legacy", kind="worker", status="success"))
        session.flush()
        session.add(ModelAttemptAudit(attempt_id="legacy-audit", run_id=run_id, execution_thread_id="legacy",
            checkpoint_ns="", checkpoint_id="frozen", source_manifest={"hash": "historical"}, status="completed",
            usage={"input_tokens": 17, "output_tokens": 9}))
    return run_id, task_id


def test_receipt_migration_preserves_unknown_history_and_enforces_parent_restrict(tmp_path, isolated_postgres_database):
    with _database() as (config, engine):
        run_id, task_id = _seed(engine, tmp_path)
        command.downgrade(config, "c36f7a8b9c0d")
        assert "parent_run_id" not in {c["name"] for c in inspect(engine).get_columns("desktop_runs")}
        assert "usage_accounting" not in {c["name"] for c in inspect(engine).get_columns("model_attempt_audits")}
        command.upgrade(config, "head")
        with Session(engine) as session:
            run = session.get(DesktopRun, run_id)
            audit = session.get(ModelAttemptAudit, "legacy-audit")
            assert (run.loop_id, run.round_id, run.parent_run_id) == (None, None, None)
            assert audit.usage_accounting == {}
            assert audit.usage == {"input_tokens": 17, "output_tokens": 9}
            assert audit.source_manifest == {"hash": "historical"}
        foreign_key = next(f for f in inspect(engine).get_foreign_keys("desktop_runs") if f["name"] == "fk_desktop_runs_parent_run")
        assert foreign_key["options"]["ondelete"] == "RESTRICT"
        with Session(engine) as session, session.begin():
            session.add(DesktopRun(run_id=uuid.uuid4().hex, task_id=task_id, agent_id="child", kind="worker",
                status="success", parent_run_id=run_id))
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text("DELETE FROM desktop_runs WHERE run_id=:run_id"), {"run_id": run_id})


@pytest.mark.parametrize("retained", ["parent", "receipt"])
def test_receipt_migration_refuses_lossy_downgrade_and_preserves_audit(tmp_path, isolated_postgres_database, retained):
    with _database() as (config, engine):
        run_id, task_id = _seed(engine, tmp_path)
        with Session(engine) as session, session.begin():
            if retained == "parent":
                session.add(DesktopRun(run_id=uuid.uuid4().hex, task_id=task_id, agent_id="child", kind="worker",
                    status="success", parent_run_id=run_id))
            else:
                session.get(ModelAttemptAudit, "legacy-audit").usage_accounting = {"state": "reported", "input_tokens": 17}
        with pytest.raises(RuntimeError, match="保留增量 schema"):
            command.downgrade(config, "c36f7a8b9c0d")
        assert "parent_run_id" in {c["name"] for c in inspect(engine).get_columns("desktop_runs")}
        assert "usage_accounting" in {c["name"] for c in inspect(engine).get_columns("model_attempt_audits")}
        with Session(engine) as session:
            audit = session.get(ModelAttemptAudit, "legacy-audit")
            assert audit.source_manifest == {"hash": "historical"}
            assert audit.usage == {"input_tokens": 17, "output_tokens": 9}
            assert session.scalar(text("SELECT version_num FROM alembic_version")) == "e58b9c0d1e2f"
            if retained == "parent":
                assert session.scalar(select(DesktopRun.run_id).where(DesktopRun.parent_run_id == run_id)) is not None
            else:
                assert audit.usage_accounting == {"state": "reported", "input_tokens": 17}
