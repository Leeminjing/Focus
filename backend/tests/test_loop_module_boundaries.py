r"""本文件对外提供 Loop 韧性模块的静态职责与文件头边界测试。

输入为新增领域、Run 编排和桌面源文件；输出为模块存在、声明式五要素文件头、禁止反向依赖和禁止重新扩张组合根的断言。
具体工作流为读取源码并检查职责词与 import 方向，不启动应用。示例：`pytest backend/tests/test_loop_module_boundaries.py`。
"""

from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILES = (
    "backend/app/desktop/agent_loop/persistence_safety.py",
    "backend/app/desktop/agent_loop/wait_models.py",
    "backend/app/desktop/agent_loop/wait_requests.py",
    "backend/app/desktop/agent_loop/activation_eligibility.py",
    "backend/app/desktop/agent_loop/projection_recovery.py",
    "backend/app/desktop/run_orchestration/admission.py",
    "backend/app/desktop/run_orchestration/assembler.py",
    "backend/app/desktop/run_orchestration/dispatch.py",
    "desktop/task-run-operations.js",
    "desktop/loop-wait-request-view.js",
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
