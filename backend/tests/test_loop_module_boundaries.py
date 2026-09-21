r"""本文件对外提供 Loop 韧性、策展所有权、终态生命周期与桌面响应解码模块的静态边界测试。

输入为新增领域、Run 编排、迁移和桌面源文件；输出为模块存在、声明式五要素文件头、单向依赖与纯解码器无 DOM 耦合的断言。
具体工作流为读取源码并检查职责词、终态写入点和 import 方向，不启动应用。示例：`pytest backend/tests/test_loop_module_boundaries.py`。
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILES = (
    "backend/app/desktop/agent_loop/persistence_safety.py",
    "backend/app/desktop/agent_loop/wait_models.py",
    "backend/app/desktop/agent_loop/wait_requests.py",
    "backend/app/desktop/agent_loop/activation_eligibility.py",
    "backend/app/desktop/agent_loop/projection_recovery.py",
    "backend/app/desktop/agent_loop/curation_ownership.py",
    "backend/app/desktop/agent_loop/terminal_lifecycle.py",
    "backend/app/desktop/agent_loop/runtime_convergence.py",
    "backend/packages/harness/focus/persistence/migrations/versions/2c3d4e5f6a7b_retire_terminal_loop_curation_ownership.py",
    "backend/app/desktop/run_orchestration/admission.py",
    "backend/app/desktop/run_orchestration/assembler.py",
    "backend/app/desktop/run_orchestration/dispatch.py",
    "desktop/task-run-operations.js",
    "desktop/loop-wait-request-view.js",
    "desktop/http-response.js",
    "desktop/loop-api.js",
    "desktop/app.js",
    "desktop/test-helper.cjs",
)


@pytest.mark.parametrize("relative_path", SOURCE_FILES)
def test_resilience_modules_exist_with_declarative_header(relative_path: str) -> None:
    source = (ROOT / relative_path).read_text(encoding="utf-8")
    header = "\n".join(source.splitlines()[:24])
    for phrase in ("本文件", "输入", "输出", "具体工作流", "示例"):
        assert phrase in header, f"{relative_path} 缺少 {phrase}"


def test_persistence_normalizer_has_no_orm_dependency() -> None:
    source = (ROOT / SOURCE_FILES[0]).read_text(encoding="utf-8")
    assert "sqlalchemy" not in source
    assert "AsyncSession" not in source


def test_frontend_operation_store_has_no_dom_dependency() -> None:
    source = (ROOT / "desktop/task-run-operations.js").read_text(encoding="utf-8")
    assert "document." not in source
    assert "innerHTML" not in source


def test_response_decoder_has_no_dom_or_loop_dependency() -> None:
    source = (ROOT / "desktop/http-response.js").read_text(encoding="utf-8")
    for forbidden in ("document.", "window.", "innerHTML", "FocusLoopApi"):
        assert forbidden not in source


def test_curation_ownership_and_terminal_lifecycle_dependencies_are_acyclic() -> None:
    ownership = (ROOT / "backend/app/desktop/agent_loop/curation_ownership.py").read_text(encoding="utf-8")
    terminal = (ROOT / "backend/app/desktop/agent_loop/terminal_lifecycle.py").read_text(encoding="utf-8")
    runtime = (ROOT / "backend/app/desktop/agent_loop/runtime_convergence.py").read_text(encoding="utf-8")
    assert "fastapi" not in ownership
    assert "terminal_lifecycle" not in ownership
    assert "terminal_lifecycle" not in runtime
    assert "curation_ownership" in terminal
    assert "runtime_convergence" in terminal


def test_ownership_repair_migration_has_no_application_dependency() -> None:
    source = (ROOT / "backend/packages/harness/focus/persistence/migrations/versions/2c3d4e5f6a7b_retire_terminal_loop_curation_ownership.py").read_text(encoding="utf-8")
    assert "backend.app" not in source
    assert "CurationOwnershipRepository" not in source


def test_terminal_loop_status_writes_do_not_bypass_lifecycle_coordinator() -> None:
    root = ROOT / "backend/app/desktop/agent_loop"
    assignment = re.compile(r"loop\.status\s*=\s*[\"'](?:completed|stopped|failed)[\"']")
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "terminal_lifecycle.py":
            continue
        if assignment.search(path.read_text(encoding="utf-8")):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_every_in_scope_persistence_port_uses_the_shared_normalizer() -> None:
    ports = (
        "backend/app/desktop/agent_loop/event_journal.py",
        "backend/app/desktop/agent_loop/fact_lifecycle.py",
        "backend/app/desktop/agent_loop/wait_requests.py",
        "backend/app/desktop/run_orchestration/admission.py",
        "backend/app/desktop/run_orchestration/lifecycle.py",
        "backend/app/desktop/run_orchestration/outbox.py",
    )
    for relative_path in ports:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "PersistencePayloadNormalizer" in source, f"{relative_path} 绕过统一持久化安全边界"
