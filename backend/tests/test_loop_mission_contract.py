r"""本文件对外提供 Loop Mission contract 的迁移、有效投影、冻结分区证据、领域验证、机器边界、类型化完成证据、旧契约转换与修订服务回归测试。

输入为旧 Goal revision、新 Mission contract 请求及隔离 PostgreSQL；输出为 schema 往返、无损 legacy
边界、稳定完成检查标识与用户修订约束的断言。具体工作流为先验证 additive 迁移，再覆盖纯领域转换与
Repository/Service 事务行为。示例：`pytest backend/tests/test_loop_mission_contract.py`。
迁移验证覆盖新增输入来源列；旧非空 Mission 的身份与来源保持。
"""

import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from backend.app.desktop.contexts.curation.contracts import (
    MissionEvidenceRef,
    evidence_ref_key,
)
from backend.app.desktop.patrol.agent_loop.completion.completion_policy import (
    CompletionCheckPolicy,
)
from backend.app.desktop.patrol.agent_loop.context_expansion.mission_sections import (
    FrozenMissionSectionCatalog,
    MissionSectionMismatch,
)
from backend.app.desktop.patrol.agent_loop.missions.mission_contract import (
    CompletionCheckDefinition,
    ExecutionBoundaries,
    LegacyMissionAdapter,
    LoopMissionContract,
)
from backend.app.desktop.patrol.agent_loop.missions.mission_projection import (
    EffectiveMissionProjector,
)
from backend.app.desktop.patrol.agent_loop.missions.mission_service import (
    MissionRevisionService,
)
from backend.app.desktop.patrol.agent_loop.schemas import CriterionVerification


def test_mission_contract_migration_is_additive_and_reversible(isolated_postgres_database, monkeypatch) -> None:
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
    database = "focus_mission_migration_" + uuid.uuid4().hex[:12]
    admin = create_engine(database_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database}"'))
    monkeypatch.setenv("FOCUS_DATABASE_URL", database_url.set(drivername="postgresql+asyncpg", database=database).render_as_string(hide_password=False))
    engine = create_engine(database_url.set(database=database))
    try:
        command.upgrade(config, "f3a4b5c6d7e8")
        schema = inspect(engine)
        assert "loop_mission_revisions" not in schema.get_table_names()
        assert "loop_goal_revisions" in schema.get_table_names()

        command.upgrade(config, "a4b5c6d7e8f9")
        assert "loop_mission_revisions" in inspect(engine).get_table_names()
        command.downgrade(config, "f3a4b5c6d7e8")
        assert "loop_mission_revisions" not in inspect(engine).get_table_names()
        assert "loop_goal_revisions" in inspect(engine).get_table_names()

        command.upgrade(config, "a4b5c6d7e8f9")
        schema = inspect(engine)
        assert {"loop_goal_revisions", "loop_mission_revisions"} <= set(schema.get_table_names())
        columns = {column["name"] for column in schema.get_columns("loop_mission_revisions")}
        assert {
            "mission_revision_id",
            "loop_id",
            "revision",
            "outcome",
            "boundaries",
            "completion_checks",
            "legacy_goal_revision_id",
            "authored_by",
            "created_at",
            "input_sources",
        } == columns
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        admin.dispose()


def test_legacy_contract_is_lossless_and_check_ids_are_stable() -> None:
    source = {
        "goal": "交付新的 Loop",
        "task_contract": "保留现有 Kernel；不得伪造事件。\n原始换行必须保留。",
        "acceptance_criteria": [
            {"criterion_id": "tests", "text": "测试通过", "required": True},
            {"text": "用户确认交互", "required": False, "user_verification": True},
        ],
    }
    first = LegacyMissionAdapter.convert(**source)
    second = LegacyMissionAdapter.convert(**source)

    assert first.outcome == "交付新的 Loop"
    assert first.boundaries.legacy_text == source["task_contract"]
    assert first.completion_checks[0].check_id == "tests"
    assert first.completion_checks[1].check_id == second.completion_checks[1].check_id
    assert first.completion_checks[1].user_verification is True


def test_effective_mission_projection_preserves_sections_and_legacy_source() -> None:
    structured = SimpleNamespace(
        revision=3,
        outcome="交付插件",
        boundaries={"in_scope": ["测试 Vault"], "required_invariants": ["保留 Kernel"], "prohibited_actions": [], "legacy_text": None},
        completion_checks=[{"check_id": "build", "claim": "构建通过", "expected_evidence_kinds": ["test"]}],
    )
    legacy = SimpleNamespace(
        revision=2,
        goal="旧目标",
        task_contract="保留原始\n换行",
        acceptance_criteria=[{"criterion_id": "tests", "text": "测试通过", "required": True}],
    )

    current = EffectiveMissionProjector.from_rows(structured=structured, legacy=legacy)
    old = EffectiveMissionProjector.from_rows(structured=None, legacy=legacy)

    assert current.revision == 3
    assert current.section_hashes.keys() == {"outcome", "boundary:in_scope", "boundary:required_invariants", "completion_check:build"}
    assert current.completion_checks[0]["check_id"] == "build"
    assert current.legacy_source is None
    assert old.legacy_source == {"goal": legacy.goal, "task_contract": legacy.task_contract, "acceptance_criteria": legacy.acceptance_criteria}
    assert old.boundaries["legacy_text"] == legacy.task_contract
    assert "legacy_source" not in old.model_payload()
    assert old.section_hashes["completion_check:tests"] != old.section_hashes["outcome"]


def test_frozen_mission_section_refs_reject_wrong_section_revision_and_hash() -> None:
    structured = SimpleNamespace(
        revision=3,
        outcome="交付插件",
        boundaries={"in_scope": ["测试 Vault"], "required_invariants": [], "prohibited_actions": [], "legacy_text": None},
        completion_checks=[{"check_id": "outcome", "claim": "构建通过", "expected_evidence_kinds": ["test"]}],
    )
    mission = EffectiveMissionProjector.from_rows(structured=structured, legacy=None)
    catalog = FrozenMissionSectionCatalog.from_mission("loop-1", mission)
    refs = {(entry.ref.section_kind, entry.ref.item_id): entry.ref for entry in catalog.entries}
    outcome = refs[("outcome", "outcome")]
    check = refs[("completion_check", "outcome")]

    assert catalog.resolve(outcome).content == "交付插件"
    assert catalog.resolve(refs[("boundary", "in_scope")]).content == ["测试 Vault"]
    assert catalog.resolve(check).content["claim"] == "构建通过"
    assert evidence_ref_key(outcome) != evidence_ref_key(check)
    with pytest.raises(MissionSectionMismatch, match="revision"):
        catalog.resolve(outcome.model_copy(update={"goal_revision": 2}))
    with pytest.raises(MissionSectionMismatch, match="content hash"):
        catalog.resolve(outcome.model_copy(update={"content_hash": "f" * 64}))
    with pytest.raises(MissionSectionMismatch, match="section identity"):
        catalog.resolve(outcome.model_copy(update={"section_kind": "boundary"}))
    tampered = catalog.model_dump(mode="json")
    tampered["entries"][0]["content"] = "伪造的 Mission 内容"
    with pytest.raises(ValidationError, match="内容与来源哈希不一致"):
        FrozenMissionSectionCatalog.model_validate(tampered)
    with pytest.raises(ValidationError, match="boundary identity"):
        MissionEvidenceRef(identity_version="mission-section-v2", loop_id="loop-1", goal_revision=3, section_kind="boundary", item_id="invented", content_hash="a" * 64)


def test_mission_contract_rejects_cross_section_duplicates() -> None:
    try:
        LoopMissionContract(
            outcome="测试通过",
            boundaries=ExecutionBoundaries(required_invariants=("保留 Kernel",)),
            completion_checks=(
                CompletionCheckDefinition(
                    check_id="tests",
                    claim="测试通过",
                    expected_evidence_kinds=("test",),
                ),
            ),
        )
    except ValidationError as exc:
        assert "相同内容不能同时属于最终结果和完成检查" in str(exc)
    else:
        raise AssertionError("跨分区重复内容必须被拒绝")


def test_completion_check_requires_typed_or_user_evidence() -> None:
    try:
        CompletionCheckDefinition(check_id="missing", claim="已经完成")
    except ValidationError as exc:
        assert "完成检查必须声明证据类别或人工验证" in str(exc)
    else:
        raise AssertionError("没有证据契约的检查必须被拒绝")


def test_execution_boundaries_expose_only_explicit_machine_rules() -> None:
    boundaries = ExecutionBoundaries(
        in_scope=("前端", "context:context-1"),
        prohibited_actions=("不要创建新 Context", "create_lane"),
    )

    assert boundaries.scoped_context_ids() == ("context-1",)
    assert boundaries.blocked_action_types() == ("create_lane",)


def test_completion_policy_rejects_undeclared_prose_and_wrong_evidence_kind() -> None:
    checks = ({"check_id": "tests", "claim": "测试通过", "expected_evidence_kinds": ["test"], "required": True},)
    policy = CompletionCheckPolicy()

    with pytest.raises(ValueError, match="只能评估"):
        policy.validate(
            checks,
            (CriterionVerification(check_id="最终结果文字", status="unknown", explanation="不是声明的检查"),),
        )
    with pytest.raises(ValueError, match="未声明的证据类型"):
        policy.validate(
            checks,
            (CriterionVerification(check_id="tests", status="satisfied", evidence=({"kind": "artifact", "source_id": "artifact-1"},), explanation="错误类型"),),
        )


def test_only_user_can_record_mission_revision() -> None:
    service = MissionRevisionService()
    try:
        service._require_user_author("patrol")
    except PermissionError as exc:
        assert "必须由用户显式确认" in str(exc)
    else:
        raise AssertionError("Patrol 不得创建 Mission revision")
