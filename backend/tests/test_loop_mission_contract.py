r"""本文件对外提供 Loop Mission contract 的迁移、领域验证、机器边界、类型化完成证据、旧契约转换与修订服务回归测试。

输入为旧 Goal revision、新 Mission contract 请求及隔离 PostgreSQL；输出为 schema 往返、无损 legacy
边界、稳定完成检查标识与用户修订约束的断言。具体工作流为先验证 additive 迁移，再覆盖纯领域转换与
Repository/Service 事务行为。示例：`pytest backend/tests/test_loop_mission_contract.py`。
"""

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from pydantic import ValidationError
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url

from backend.app.desktop.agent_loop.mission_contract import (
    CompletionCheckDefinition,
    ExecutionBoundaries,
    LegacyMissionAdapter,
    LoopMissionContract,
)
from backend.app.desktop.agent_loop.completion_policy import CompletionCheckPolicy
from backend.app.desktop.agent_loop.mission_service import MissionRevisionService
from backend.app.desktop.agent_loop.schemas import CriterionVerification, LoopCreateRequest


def test_mission_contract_migration_is_additive_and_reversible(isolated_postgres_database) -> None:
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
    try:
        command.downgrade(config, "f3a4b5c6d7e8")
        schema = inspect(engine)
        assert "loop_mission_revisions" not in schema.get_table_names()
        assert "loop_goal_revisions" in schema.get_table_names()

        command.upgrade(config, "head")
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
        } == columns
    finally:
        command.upgrade(config, "head")
        engine.dispose()


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


def test_create_request_accepts_structured_mission_and_legacy_payloads() -> None:
    common = {
        "loop_id": "loop-1",
        "workspace_id": "workspace-1",
        "initial_context_id": "context-1",
        "initial_run_id": "run-1",
        "holder_id": "patrol-1",
        "capabilities": ("request_completion",),
        "context_scope": ("context-1",),
        "permission_scope": ("read",),
    }
    mission = LoopMissionContract(
        outcome="交付 Live Loop",
        boundaries=ExecutionBoundaries(required_invariants=("保留 Kernel",)),
        completion_checks=(
            CompletionCheckDefinition(
                check_id="tests",
                claim="测试通过",
                expected_evidence_kinds=("test",),
            ),
        ),
    )
    structured = LoopCreateRequest(**common, mission=mission)
    legacy = LoopCreateRequest(
        **common,
        goal="交付 Live Loop",
        task_contract="保留 Kernel",
        acceptance_criteria=({"criterion_id": "tests", "text": "测试通过"},),
    )

    assert structured.resolved_mission() == mission
    assert legacy.resolved_mission().boundaries.legacy_text == "保留 Kernel"


def test_create_request_rejects_mixed_mission_shapes() -> None:
    mission = LoopMissionContract(
        outcome="交付 Live Loop",
        completion_checks=(
            CompletionCheckDefinition(
                check_id="tests",
                claim="测试通过",
                expected_evidence_kinds=("test",),
            ),
        ),
    )
    try:
        LoopCreateRequest(
            loop_id="loop-1",
            workspace_id="workspace-1",
            initial_context_id="context-1",
            initial_run_id="run-1",
            holder_id="patrol-1",
            mission=mission,
            goal="重复目标",
            capabilities=("request_completion",),
            context_scope=("context-1",),
            permission_scope=("read",),
        )
    except ValidationError as exc:
        assert "不能同时提交" in str(exc)
    else:
        raise AssertionError("结构化 Mission 与旧字段不得混用")
