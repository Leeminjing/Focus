"""本文件对外提供本机调用策略绑定的测试用例。

输入为服务端执行档案、不同工作区与并行 Run 身份；输出为不可变且互不串扰的调用绑定。
具体工作流为签发 SecurityContext，再用工具调用 ID 绑定真实工作区，核对身份缺失拒绝
及模式修改后既有绑定仍保留原值。
示例：运行 python -m pytest backend/tests/test_call_execution_binding.py。
"""

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from focus.security.context import (
    AuthorizationIdentity,
    ExecutionProfile,
    RoutingIdentity,
    derive_security_context,
)
from focus.security.execution import bind_call_execution
from focus.security.policy import AccessMode


def _context(workspace: Path, run_id: str, mode: AccessMode) -> dict:
    profile = ExecutionProfile(
        authorization=AuthorizationIdentity(
            workspace=workspace, roots=(workspace,), permissions=("host_command",),
            access_mode=mode, agent_role="main",
        ),
        routing=RoutingIdentity(
            thread_id="session-1", workspace_id="workspace-1", agent_id="agent-1",
            task_id="task-1", checkpoint_ns="", run_id=run_id,
        ),
    )
    return derive_security_context(profile).to_runtime_context()


def test_bindings_keep_run_mode_and_real_workspace_separate(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = bind_call_execution(_context(first_root, "run-a", AccessMode.READ_ONLY), "call-a")
    second = bind_call_execution(_context(second_root, "run-b", AccessMode.WORKSPACE_WRITE), "call-b")
    assert (first.run_id, first.mode, first.workspace) == ("run-a", AccessMode.READ_ONLY, first_root.resolve())
    assert (second.run_id, second.mode, second.workspace) == (
        "run-b", AccessMode.WORKSPACE_WRITE, second_root.resolve()
    )
    with pytest.raises(FrozenInstanceError):
        first.mode = AccessMode.DANGER_FULL_ACCESS


def test_flat_model_claim_does_not_change_signed_mode(tmp_path):
    context = _context(tmp_path, "run-a", AccessMode.READ_ONLY)
    context["access_mode"] = "danger-full-access"
    context["workspace"] = str(tmp_path.parent)
    binding = bind_call_execution(context, "call-a")
    assert binding.mode is AccessMode.READ_ONLY
    assert binding.workspace == tmp_path.resolve()


def test_missing_call_or_run_identity_is_rejected(tmp_path):
    context = _context(tmp_path, "", AccessMode.WORKSPACE_WRITE)
    with pytest.raises(RuntimeError, match="身份"):
        bind_call_execution(context, "call-a")
    context = _context(tmp_path, "run-a", AccessMode.WORKSPACE_WRITE)
    with pytest.raises(RuntimeError, match="身份"):
        bind_call_execution(context, "")


def test_workspace_must_exist_before_binding(tmp_path):
    context = _context(tmp_path / "missing", "run-a", AccessMode.WORKSPACE_WRITE)
    with pytest.raises(FileNotFoundError):
        bind_call_execution(context, "call-a")


def test_mode_change_creates_new_binding_without_rewriting_old_one(tmp_path):
    first_context = _context(tmp_path, "run-a", AccessMode.WORKSPACE_WRITE)
    first = bind_call_execution(first_context, "call-a")
    security = first_context["security_context"]
    narrowed = replace(security, authorization=replace(security.authorization, access_mode=AccessMode.READ_ONLY))
    second = bind_call_execution(narrowed.to_runtime_context(), "call-b")
    assert first.mode is AccessMode.WORKSPACE_WRITE
    assert second.mode is AccessMode.READ_ONLY
