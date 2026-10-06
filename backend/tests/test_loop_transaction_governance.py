"""本文件对外提供 Loop 事务治理的入口合同回归测试。

输入为真实工具目录、工具请求及其失败 handler；输出为公开约束与耐久错误身份断言。
具体工作流为读取模型实际接收的 schema，验证非法参数在副作用前拒绝，并检查失败回包来源。
示例：python -m pytest backend/tests/test_loop_transaction_governance.py -q。
"""

import asyncio

import pytest
from langchain.tools import ToolRuntime
from langchain.tools.tool_node import ToolCallRequest
from pydantic import ValidationError
from langchain.tools import ToolException
from langchain_core.messages import ToolMessage

from backend.app.desktop.collab import AgentCollab
from backend.app.desktop.service import DesktopService
from backend.app.desktop.tool_error_provider import build_tool_error_middleware
from backend.app.desktop.equipment_policy import activation_equipment, effective_equipment
from backend.tests.runtime_context_support import runtime_context


def test_activation_inherits_server_equipment_without_ui_cache():
    source = {"model_name": "model-a", "skills": ["vault"], "skill_snapshots": [{"id": "v1"}],
              "permissions": ["read", "write", "host_command"], "access_mode": "danger-full-access",
              "_durable_dispatch_execution": {"agent_role": "main"}}
    result = activation_equipment(source, {}, (), inherit=True)
    assert result["permissions"] == source["permissions"]
    assert result["access_mode"] == "danger-full-access"
    assert result["skills"] == ["vault"] and result["skill_snapshots"] == [{"id": "v1"}]
    assert result["patrol_model_name"] == "model-a"
    assert "_durable_dispatch_execution" not in result
    result["skills"].append("another")
    assert source["skills"] == ["vault"]
    narrowed = effective_equipment(source, permissions=["read"], read_only=True)
    assert narrowed["permissions"] == ["read"] and narrowed["access_mode"] == "read-only"
    with pytest.raises(ValueError, match="超出"):
        effective_equipment({"permissions": ["read"]}, permissions=["write"])
    with pytest.raises(ValueError, match="缺少"):
        activation_equipment({}, {}, (), inherit=True)


def test_wait_schema_and_validation_share_bounds():
    service = DesktopService.__new__(DesktopService)
    tool = next(item for item in service._build_swarm_tools() if item.name == "wait_for_swarm")
    schema = tool.tool_call_schema.model_json_schema()
    assert schema["properties"]["timeout_seconds"]["minimum"] == 1
    assert schema["properties"]["timeout_seconds"]["maximum"] == 30
    for seconds in (0, 31, 240):
        with pytest.raises(ValidationError):
            tool.tool_call_schema.model_validate({"agent_ids": ["agent"], "timeout_seconds": seconds})
    for seconds in (1, 30):
        tool.tool_call_schema.model_validate({"agent_ids": ["agent"], "timeout_seconds": seconds})


def test_message_schema_rejects_unsupported_kind():
    service = AgentCollab.__new__(AgentCollab)
    tool = service.build_send_message_tool()
    schema = tool.tool_call_schema.model_json_schema()
    assert "report" not in schema["properties"]["kind"]["enum"]
    assert "message" in schema["properties"]["kind"]["enum"]
    with pytest.raises(ValidationError):
        tool.tool_call_schema.model_validate({"to_agent": "main", "content": "done", "kind": "report"})


@pytest.mark.parametrize("error", [ValueError("invalid kind"), FileNotFoundError("package.json")])
def test_preexecution_errors_keep_tool_identity(error):
    async def run():
        runtime = ToolRuntime(state={}, context={}, config={}, stream_writer=None, tool_call_id=None, store=None, tools=[])
        request = ToolCallRequest(tool_call={"name": "send_message", "id": "call-1", "args": {}, "type": "tool_call"}, tool=None, state={}, runtime=runtime)

        async def fail(_request):
            raise error

        result = await build_tool_error_middleware().awrap_tool_call(request, fail)
        assert result.name == "send_message"
        assert result.tool_call_id == "call-1"
        assert result.status == "error"

    asyncio.run(run())


@pytest.mark.parametrize("failure", [ValueError, PermissionError, ToolException, RuntimeError])
def test_failure_preserves_frozen_identity_and_redacts_secrets(failure, monkeypatch):
    async def run():
        secret = "test-secret-123456789"
        monkeypatch.setenv("FOCUS_TEST_API_KEY", secret)
        runtime = ToolRuntime(state={}, context=runtime_context(agent_id="main:test", task_id="test", run_id="run-original"), config={}, stream_writer=None,
            tool_call_id="call-original", store=None, tools=[])
        request = ToolCallRequest(tool_call={"name": "write_file", "id": "call-original", "args": {}, "type": "tool_call"},
            tool=None, state={}, runtime=runtime)
        async def fail(current):
            current.tool_call["name"] = "changed-after-validation"
            current.runtime.context["run_id"] = "changed-run"
            raise failure(f"failed {secret}")
        result = await build_tool_error_middleware().awrap_tool_call(request, fail)
        assert (result.name, result.tool_call_id, result.status) == ("write_file", "call-original", "error")
        assert result.additional_kwargs["focus_tool_call"] == {"run_id": "run-original", "name": "write_file",
            "call_id": "call-original", "failure_kind": failure.__name__}
        assert secret not in result.content
    asyncio.run(run())


def test_business_failure_keeps_call_pairing_and_cancel_propagates(monkeypatch):
    async def run():
        secret = "test-secret-business-123456789"
        monkeypatch.setenv("FOCUS_TEST_SECRET", secret)
        runtime = ToolRuntime(state={}, context=runtime_context(agent_id="main:test", task_id="test", run_id="run-1"), config={}, stream_writer=None,
            tool_call_id="call-1", store=None, tools=[])
        request = ToolCallRequest(tool_call={"name": "read_file", "id": "call-1", "args": {}, "type": "tool_call"},
            tool=None, state={}, runtime=runtime)
        async def business(_request):
            return ToolMessage(content=f"failed {secret}", tool_call_id="call-1", status="error")
        middleware = build_tool_error_middleware()
        result = await middleware.awrap_tool_call(request, business)
        assert result.name == "read_file" and result.tool_call_id == "call-1" and secret not in result.content
        assert result.additional_kwargs["focus_tool_call"]["run_id"] == "run-1"
        async def cancel(_request):
            raise asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError) as raised:
            await middleware.awrap_tool_call(request, cancel)
        assert raised.value.__notes__ == ["tool=read_file call_id=call-1 run_id=run-1"]
    asyncio.run(run())
