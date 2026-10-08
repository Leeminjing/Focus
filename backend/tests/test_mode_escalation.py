"""本文件对外提供单次文件模式放宽的审批绑定与租约复查测试。

输入为服务端安全上下文、内置 Shell 或写工具参数以及模拟的人工作答。
输出为合法阶梯、请求标识绑定、批准只作用于当前调用和租约失效拒绝的证据。
具体工作流为先构造受治理 ToolCallRequest，再调用中间件并检查传给处理器的不可变执行绑定。
示例：运行 python -m pytest backend/tests/test_mode_escalation.py。
"""

import asyncio
import json
import os
from pathlib import Path

import pytest
from langchain.tools import ToolRuntime
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from backend.tests.runtime_context_support import runtime_context
import focus.security.middleware as middleware_module
from focus.security.escalation import validate_escalation
from focus.security.execution import bind_call_execution
from focus.security.middleware import AccessPolicyMiddleware, _admit
from focus.security.policy import AccessMode
from focus.sandbox import WindowsAclBackend
from focus.tools.builtins.workspace_tools import powershell, read_file, write_file
from focus.tools.builtins import workspace_tools
from windows_sandbox_fixture import windows_sandbox_roots


def _request(tool, workspace: Path, mode: AccessMode, args: dict, call_id: str = "call-1"):
    runtime = ToolRuntime(
        state={}, context=runtime_context(
            agent_id="agent-1", task_id="task-1", workspace=str(workspace),
            permissions=("read", "write", "host_command"), access_mode=mode,
        ), config={}, stream_writer=None, tool_call_id=call_id, store=None, tools=[],
    )
    return ToolCallRequest(
        tool_call={"name": tool.name, "args": args, "id": call_id},
        tool=tool, state={}, runtime=runtime,
    )


@pytest.mark.parametrize("current,target", [
    (AccessMode.READ_ONLY, "workspace-write"),
    (AccessMode.READ_ONLY, "danger-full-access"),
    (AccessMode.WORKSPACE_WRITE, "danger-full-access"),
])
def test_valid_escalation_steps(current, target):
    assert validate_escalation(current, target, "完成本次操作") is AccessMode(target)


@pytest.mark.parametrize("current,target,reason", [
    (AccessMode.READ_ONLY, "read-only", "same"),
    (AccessMode.WORKSPACE_WRITE, "read-only", "narrow"),
    (AccessMode.WORKSPACE_WRITE, "workspace-write", "same"),
    (AccessMode.READ_ONLY, "bogus", "invalid"),
    (AccessMode.READ_ONLY, "workspace", "legacy spelling"),
    (AccessMode.READ_ONLY, "workspace-write", ""),
])
def test_invalid_escalation_fails_before_execution(current, target, reason):
    with pytest.raises(ValueError):
        validate_escalation(current, target, reason)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("target,reason", [
    ("workspace-write", "write fixture"),
    ("danger-full-access", "same mode"),
    ("bogus", "invalid mode"),
    ("workspace-write", ""),
])
def test_invalid_escalation_is_a_recoverable_tool_result(tmp_path, target, reason, asynchronous):
    request = _request(powershell, tmp_path, AccessMode.DANGER_FULL_ACCESS,
        {"command": "echo ready", "requested_mode": target, "reason": reason})
    seen = []

    def handler(active):
        seen.append(active)
        return ToolMessage(content="executed", tool_call_id="call-1")

    async def async_handler(active):
        return handler(active)

    middleware = AccessPolicyMiddleware()
    result = (asyncio.run(middleware.awrap_tool_call(request, async_handler))
              if asynchronous else middleware.wrap_tool_call(request, handler))
    assert (result.name, result.tool_call_id, result.status) == ("powershell", "call-1", "error")
    assert "requested_mode" in result.content
    assert seen == []


def test_agent_corrects_invalid_escalation_without_losing_the_run(tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage
    from backend.app.desktop.tool_error_provider import build_tool_error_middleware
    from backend.tests.config_helpers import app_config_for, ToolCapableFakeChatModel
    from backend.tests.tool_catalog_support import registry_for
    import focus.agents.lead.agent as lead

    async def run():
        model = ToolCapableFakeChatModel(scripted=[
            AIMessage(content="", tool_calls=[{"name": "write_file", "id": "bad", "args": {
                "path": "rejected.txt", "content": "bad", "requested_mode": "workspace-write", "reason": "write fixture"}}]),
            AIMessage(content="", tool_calls=[{"name": "write_file", "id": "corrected", "args": {
                "path": "accepted.txt", "content": "corrected"}}]),
            AIMessage(content="done"),
        ])
        monkeypatch.setattr(lead, "create_chat_model", lambda **kwargs: model)
        monkeypatch.setattr(lead, "get_plugin_registry", lambda: registry_for([]))
        graph = await lead.make_lead_agent(model_name="fixture", tools=[write_file], middlewares=[],
            additional_middlewares=[build_tool_error_middleware()], app_config=app_config_for("fixture", None))
        result = await graph.ainvoke({"messages": [HumanMessage(content="write fixture")]},
            context=runtime_context(agent_id="main", task_id="fixture", workspace=str(tmp_path),
                permissions=("read", "write"), access_mode=AccessMode.DANGER_FULL_ACCESS))
        messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert [(m.tool_call_id, m.status) for m in messages] == [("bad", "error"), ("corrected", "success")]
        assert not (tmp_path / "rejected.txt").exists()
        assert (tmp_path / "accepted.txt").read_text() == "corrected"
        assert result["messages"][-1].content == "done"

    asyncio.run(run())


def test_read_only_tool_cannot_request_escalation(tmp_path):
    request = _request(read_file, tmp_path, AccessMode.READ_ONLY,
        {"path": "fixture.txt", "requested_mode": "workspace-write", "reason": "read fixture"})
    seen = []
    result = AccessPolicyMiddleware().wrap_tool_call(request, lambda active: seen.append(active))
    assert (result.name, result.status) == ("read_file", "error")
    assert seen == []


def test_rejected_escalation_does_not_echo_untrusted_arguments(tmp_path):
    untrusted = "not-a-mode:dummy-sensitive-value"
    request = _request(powershell, tmp_path, AccessMode.READ_ONLY,
        {"command": "echo ready", "requested_mode": untrusted, "reason": "write fixture"})
    result = AccessPolicyMiddleware().wrap_tool_call(request, lambda active: pytest.fail("must not execute"))
    assert result.status == "error"
    assert untrusted not in result.content


def test_approval_payload_binds_subject_workspace_command_and_reason(tmp_path):
    request = _request(
        powershell, tmp_path, AccessMode.READ_ONLY,
        {"command": "echo ready", "requested_mode": "workspace-write", "reason": "生成输出"},
    )
    admission = _admit(request)
    payload = admission.request.payload()
    assert admission.asked is True
    assert payload["run_id"] == "run-1"
    assert payload["call_id"] == "call-1"
    assert payload["agent_id"] == "agent-1"
    assert payload["cwd"] == str(tmp_path.resolve())
    assert payload["access_mode"] == "read-only"
    assert payload["requested_mode"] == "workspace-write"
    assert payload["reason"] == "生成输出"
    assert payload["command"] == "echo ready"
    changed = _request(
        powershell, tmp_path, AccessMode.READ_ONLY,
        {"command": "echo changed", "requested_mode": "workspace-write", "reason": "生成输出"},
    )
    assert _admit(changed).request.request_id != payload["request_id"]


def test_approved_mode_applies_only_to_bound_call(tmp_path, monkeypatch):
    request = _request(
        powershell, tmp_path, AccessMode.READ_ONLY,
        {"command": "echo ready", "requested_mode": "workspace-write", "reason": "生成输出"},
    )
    monkeypatch.setattr(
        middleware_module, "interrupt",
        lambda payload: {"decision": "approve", "request_id": payload["request_id"]},
    )
    seen = []

    def handler(active):
        seen.append(bind_call_execution(active.runtime.context, "call-1"))
        return ToolMessage(content="executed", tool_call_id="call-1")

    result = AccessPolicyMiddleware().wrap_tool_call(request, handler)
    assert result.content == "executed"
    assert seen[0].mode is AccessMode.WORKSPACE_WRITE
    assert seen[0].mode_source == "single-approval"
    assert seen[0].approval_id == _admit(request).request.request_id
    assert bind_call_execution(request.runtime.context, "call-1").mode is AccessMode.READ_ONLY


def test_changed_request_id_cannot_reuse_approval(tmp_path, monkeypatch):
    request = _request(
        powershell, tmp_path, AccessMode.READ_ONLY,
        {"command": "echo ready", "requested_mode": "workspace-write", "reason": "生成输出"},
    )
    monkeypatch.setattr(
        middleware_module, "interrupt",
        lambda payload: {"decision": "approve", "request_id": "other-call"},
    )
    seen = []
    result = AccessPolicyMiddleware().wrap_tool_call(
        request, lambda active: seen.append(active) or ToolMessage(content="executed", tool_call_id="call-1"),
    )
    assert result.status == "error"
    assert seen == []


@pytest.mark.parametrize("answer,expected", [
    ({"decision": "reject"}, "未批准"),
    ({"decision": "cancel"}, "取消"),
    (None, "APPROVAL_UNAVAILABLE"),
])
def test_nonapproval_outcomes_never_execute(tmp_path, monkeypatch, answer, expected):
    request = _request(
        powershell, tmp_path, AccessMode.READ_ONLY,
        {"command": "echo ready", "requested_mode": "workspace-write", "reason": "生成输出"},
    )
    monkeypatch.setattr(middleware_module, "interrupt", lambda payload: answer)
    seen = []
    result = AccessPolicyMiddleware().wrap_tool_call(
        request, lambda active: seen.append(active) or ToolMessage(content="executed", tool_call_id="call-1"),
    )
    assert expected in result.content
    assert seen == []


def test_approval_channel_error_never_executes(tmp_path, monkeypatch):
    request = _request(
        powershell, tmp_path, AccessMode.READ_ONLY,
        {"command": "echo ready", "requested_mode": "workspace-write", "reason": "生成输出"},
    )

    def unavailable(payload):
        raise RuntimeError("channel offline")

    monkeypatch.setattr(middleware_module, "interrupt", unavailable)
    seen = []
    result = AccessPolicyMiddleware().wrap_tool_call(
        request, lambda active: seen.append(active) or ToolMessage(content="executed", tool_call_id="call-1"),
    )
    assert "APPROVAL_UNAVAILABLE" in result.content
    assert seen == []


def test_insufficient_target_mode_is_denied_before_approval(tmp_path):
    outside = Path.home() / "focus-outside-escalation-test.txt"
    request = _request(
        write_file, tmp_path, AccessMode.READ_ONLY,
        {"path": str(outside), "content": "x", "requested_mode": "workspace-write", "reason": "输出"},
    )
    admission = _admit(request)
    assert admission.asked is False
    assert admission.denied == (outside.resolve(),)


def test_lease_is_rechecked_after_approval(tmp_path, monkeypatch):
    request = _request(
        powershell, tmp_path, AccessMode.READ_ONLY,
        {"command": "echo ready", "requested_mode": "workspace-write", "reason": "生成输出"},
    )
    active = {"valid": True}

    async def guard(lease_id, fencing_token):
        assert lease_id == "lease-1"
        assert fencing_token == 1
        if not active["valid"]:
            raise RuntimeError("租约已失效")

    request.runtime.context["workspace_lease"] = {"lease_id": "lease-1", "fencing_token": 1}
    request.runtime.context["workspace_lease_guard"] = guard

    def approve(payload):
        active["valid"] = False
        return {"decision": "approve", "request_id": payload["request_id"]}

    monkeypatch.setattr(middleware_module, "interrupt", approve)
    seen = []

    async def handler(active_request):
        seen.append(active_request)
        return ToolMessage(content="executed", tool_call_id="call-1")

    with pytest.raises(RuntimeError, match="租约已失效"):
        asyncio.run(AccessPolicyMiddleware().awrap_tool_call(request, handler))
    assert seen == []


@pytest.mark.skipif(os.name != "nt", reason="Windows 单次升权真实文件效果验收")
def test_one_call_full_access_does_not_widen_next_call(windows_sandbox_roots, monkeypatch):
    roots = windows_sandbox_roots
    backend = WindowsAclBackend(prepared_registry_path=roots.workspace_a.parent / "prepared.json")
    monkeypatch.setattr(workspace_tools, "_SHELL_BACKEND", backend)
    command = f"[IO.File]::WriteAllText('{roots.outside_file}', 'approved')"
    monkeypatch.setattr(
        middleware_module, "interrupt",
        lambda payload: {"decision": "approve", "request_id": payload["request_id"]},
    )

    def handler(active):
        output = powershell.func(**active.tool_call["args"], runtime=active.runtime)
        return ToolMessage(content=output, tool_call_id=active.tool_call["id"])

    try:
        granted = _request(
            powershell, roots.workspace_a, AccessMode.WORKSPACE_WRITE,
            {"command": command, "requested_mode": "danger-full-access", "reason": "改写外部目标"},
        )
        first = json.loads(AccessPolicyMiddleware().wrap_tool_call(granted, handler).content)
        assert first["mode"] == "danger-full-access"
        assert first["mode_source"] == "single-approval"
        assert first["approval_id"] == _admit(granted).request.request_id
        assert first["backend_applied"] is False
        assert first["exit_code"] == 0
        assert roots.outside_file.read_text() == "approved"
        ordinary = _request(
            powershell, roots.workspace_a, AccessMode.WORKSPACE_WRITE,
            {"command": f"[IO.File]::WriteAllText('{roots.outside_file}', 'escaped')"},
            call_id="call-2",
        )
        second = json.loads(AccessPolicyMiddleware().wrap_tool_call(ordinary, handler).content)
        assert second["mode"] == "workspace-write"
        assert second["backend_applied"] is True
        assert second["exit_code"] != 0
        assert roots.outside_file.read_text() == "approved"
    finally:
        assert backend.close() == ()
