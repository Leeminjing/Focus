"""访问模式在运行请求、装备与派生执行主体记录上的持久化往返用例。

输入为运行请求取值、装备字典与数据库行；输出为归一后的模式、迁移链形状与往返后的取值。
工作流先锁定迁移存在且在链上只有一个头、重复升级不报错且列默认最严，
再锁定装备经一次真实写入与重新读取后模式不丢失，最后锁定缺失或无法识别的取值按工作区保护处理。
"""

import asyncio
import atexit
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
pytestmark = pytest.mark.usefixtures("isolated_postgres_database")

_LOOP = asyncio.new_event_loop()
_ENGINE = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
_SESSION_FACTORY = async_sessionmaker(_ENGINE, expire_on_commit=False)
atexit.register(lambda: _LOOP.run_until_complete(_ENGINE.dispose()))

from backend.app.desktop.models import (  # noqa: E402
    DesktopThread,
    DesktopWorkspace,
    MainRunCreate,
    SwarmAgent,
)
from backend.app.desktop.service import (  # noqa: E402
    _MAIN_RUNTIME_EQUIPMENT_KEY,
    _resolve_access_mode,
    DesktopService,
)
from focus.security.policy import AccessMode  # noqa: E402

_MIGRATION_NAME = "a1b2c3d4e5f6_add_swarm_agent_access_mode.py"
_VERSIONS = (
    Path(__file__).parents[1]
    / "packages" / "harness" / "focus" / "persistence" / "migrations" / "versions"
)
_MIGRATION = _VERSIONS / _MIGRATION_NAME


def _revision_declarations() -> list[tuple[str, tuple[str, ...]]]:
    import ast

    declared: list[tuple[str, tuple[str, ...]]] = []
    for path in sorted(_VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        values: dict[str, object] = {}
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                try:
                    values[node.target.id] = ast.literal_eval(node.value)
                except ValueError:
                    values[node.target.id] = None
        revision = values.get("revision")
        if not isinstance(revision, str):
            continue
        down = values.get("down_revision")
        parents = (down,) if isinstance(down, str) else down if isinstance(down, tuple) else ()
        declared.append((revision, tuple(str(parent) for parent in parents)))
    return declared


def test_migration_adds_the_column_with_the_strictest_default():
    """迁移存在且在链上只有一个头：新增列非空，服务器默认取最严的工作区保护。"""
    source = _MIGRATION.read_text(encoding="utf-8")
    assert '"access_mode"' in source or "'access_mode'" in source
    assert 'server_default="workspace"' in source
    assert "nullable=False" in source

    declared = _revision_declarations()
    revisions = [revision for revision, _ in declared]
    downs = {parent for _, parents in declared for parent in parents}
    assert len(revisions) == len(set(revisions))
    assert set(revisions) - downs == {"a1b2c3d4e5f6"}


def test_reapplying_migrations_is_a_no_op():
    """升级到 head 后再次升级不报错，且列已是非空、默认取工作区保护。"""
    from alembic import command
    from alembic.config import Config

    migrations = (
        Path(__file__).parents[1]
        / "packages" / "harness" / "focus" / "persistence" / "migrations" / "alembic.ini"
    )
    command.upgrade(Config(str(migrations)), "head")
    command.upgrade(Config(str(migrations)), "head")

    async def _column() -> tuple[str, str | None]:
        async with _SESSION_FACTORY() as session:
            row = (
                await session.execute(
                    text(
                        "select is_nullable, column_default from information_schema.columns "
                        "where table_name = 'swarm_agents' and column_name = 'access_mode'"
                    )
                )
            ).one()
            return row[0], row[1]

    is_nullable, default = _LOOP.run_until_complete(_column())
    assert is_nullable == "NO"
    assert default is not None and "workspace" in default


async def _stub_task_entities(session, task_id):
    """返回真实的线程行，使装备经一次真实提交落到 ui_state 上。"""
    thread = await session.get(DesktopThread, task_id)
    return thread, SimpleNamespace(path="C:/ws", workspace_id=thread.workspace_id)


async def _stub_none(*_args, **_kwargs):
    return None


async def _stub_prepare(self, run, thread_id, workspace_id, workspace_path, messages, prompt,
                        equipment, _ns, _kind, checkpoint_id, **_kwargs):
    return SimpleNamespace(equipment=equipment)


def _seed_service(monkeypatch) -> DesktopService:
    import backend.app.desktop.service as service_module

    service = DesktopService.__new__(service_module.DesktopService)
    service.session_factory = _SESSION_FACTORY
    service.checkpointer = SimpleNamespace(aget_tuple=_stub_none)
    service._get_task_entities = _stub_task_entities
    service._commitment_recovery_payload = _stub_none
    service._freeze_skills = lambda *_: {}
    service._resolve_must_view_materials = _stub_none
    service._apply_memory_block = lambda prompt, _ids: _identity(prompt)
    service._prepare = _stub_prepare.__get__(service, DesktopService)
    service.contexts = SimpleNamespace(ensure_runnable=_stub_none)
    monkeypatch.setattr(service_module, "compression_recovery_payload", _stub_none)
    monkeypatch.setattr(service_module, "main_pending_interrupt", _stub_none)
    monkeypatch.setattr(service_module, "select_checkpoint_base", _stub_none)
    return service


async def _identity(prompt):
    return prompt


def test_run_request_model_round_trips_access_mode():
    """运行请求模型保留模式取值；缺失或无法识别的取值由服务归一为工作区保护。"""
    body = MainRunCreate(message="你好", access_mode="full")
    restored = MainRunCreate.model_validate(body.model_dump())
    assert restored.access_mode == "full"
    assert MainRunCreate(message="你好").access_mode is None

    assert _resolve_access_mode("full") is AccessMode.FULL
    assert _resolve_access_mode(None) is AccessMode.WORKSPACE
    assert _resolve_access_mode("bogus") is AccessMode.WORKSPACE


def test_equipment_round_trip_keeps_access_mode(monkeypatch):
    """主运行装备写入后重新读取，模式不丢失。"""
    task_id = "task-equipment"
    workspace_id = "ws-equipment"

    async def _run() -> dict:
        async with _SESSION_FACTORY() as session:
            session.add(
                DesktopWorkspace(
                    workspace_id=workspace_id, path="C:/ws", display_name="access-mode-equipment"
                )
            )
            await session.flush()
            session.add(
                DesktopThread(
                    task_id=task_id, workspace_id=workspace_id, thread_id="thread-1", title="equipment"
                )
            )
            await session.commit()
        service = _seed_service(monkeypatch)
        await service.start_main_run(task_id, "你好", None, ["read"], [], access_mode="full")
        async with _SESSION_FACTORY() as session:
            thread = await session.get(DesktopThread, task_id)
            return dict(thread.ui_state or {})[_MAIN_RUNTIME_EQUIPMENT_KEY]

    assert _LOOP.run_until_complete(_run())["access_mode"] == "full"


def test_derived_agent_records_keep_access_mode():
    """派生执行主体记录的默认取最严，显式写入的取值往返不丢失。"""
    task_id = "task-derived"

    async def _run() -> tuple[str, str]:
        async with _SESSION_FACTORY() as session:
            session.add(
                DesktopWorkspace(
                    workspace_id="ws-derived", path="C:/ws2", display_name="access-mode-derived"
                )
            )
            await session.flush()
            session.add(
                DesktopThread(
                    task_id=task_id, workspace_id="ws-derived", thread_id="thread-2", title="derived"
                )
            )
            await session.flush()
            session.add(
                SwarmAgent(
                    agent_id="agent-default", task_id=task_id, role="worker",
                    checkpoint_ns="swarm:agent-default", status="active", permissions=["read"],
                )
            )
            session.add(
                SwarmAgent(
                    agent_id="agent-full", task_id=task_id, role="worker",
                    checkpoint_ns="swarm:agent-full", status="active", permissions=["read"],
                    access_mode="full",
                )
            )
            await session.commit()
        async with _SESSION_FACTORY() as session:
            default = await session.get(SwarmAgent, "agent-default")
            widened = await session.get(SwarmAgent, "agent-full")
            return default.access_mode, widened.access_mode

    assert _LOOP.run_until_complete(_run()) == ("workspace", "full")
