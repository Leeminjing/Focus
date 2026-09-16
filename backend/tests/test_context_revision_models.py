r"""本文件对外提供 Context revision ORM、枚举和统一 metadata 注册验证。

输入为 Desktop persistence registry 与 Context Evolution 实体；输出为表、约束、默认值和重复导入断言。
具体工作流为导入组合根、检查 metadata，再重载 registry 证明每张领域表只注册一次。
示例：`python -m pytest backend/tests/test_context_revision_models.py -q`。
"""

from __future__ import annotations

import importlib

from sqlalchemy import CheckConstraint, UniqueConstraint

from backend.app.desktop import persistence_registry
from backend.app.desktop.context_evolution.models import (
    ContextRevision,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionSource,
)
from backend.app.desktop.models import DesktopThread
from focus.persistence.base import Base


def test_context_revision_tables_register_once() -> None:
    expected = {
        "desktop_context_revisions",
        "desktop_context_revision_sources",
    }
    assert expected <= set(Base.metadata.tables)
    before = tuple(name for name in Base.metadata.tables if name in expected)
    importlib.reload(persistence_registry)
    after = tuple(name for name in Base.metadata.tables if name in expected)
    assert before == after


def test_context_revision_model_contracts() -> None:
    revision_constraints = ContextRevision.__table__.constraints
    source_constraints = ContextRevisionSource.__table__.constraints
    assert any(
        isinstance(item, UniqueConstraint) and item.name == "uq_context_revision_generation"
        for item in revision_constraints
    )
    assert any(
        isinstance(item, CheckConstraint) and item.name == "ck_context_revision_checkpoint_payload"
        for item in revision_constraints
    )
    assert any(
        isinstance(item, UniqueConstraint) and item.name == "uq_context_revision_source_position"
        for item in source_constraints
    )
    assert "current_revision_id" in DesktopThread.__table__.columns


def test_context_revision_enums_are_stable_strings() -> None:
    assert ContextRevisionPayloadMode.CHECKPOINT.value == "checkpoint"
    assert ContextRevisionProjectionStatus.APPROVAL_REQUIRED.value == "approval_required"
    assert ContextRevisionOriginKind.RUN_SETTLED.value == "run_settled"
