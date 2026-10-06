"""本文件对外提供 build_tool_error_middleware，同步构建桌面 Agent 的工具错误中间件。

输入为 LangChain ToolCallRequest 与下游 handler；输出为带原始 name/call/run 身份的工具结果或错误 ToolMessage。
具体工作流为在 handler 之前保存调用身份，捕获参数、权限、业务和工具执行异常；已知错误清理敏感值，
未知异常仅公开错误类别，形成完整调用配对。取消保持传播，由 Run 结算的协议修复保存中断返回，避免继续执行。
示例：
`middlewares = [build_tool_error_middleware()]`。
"""

from collections.abc import Awaitable, Callable
from typing import Any
import asyncio
import os
import re

from fastapi import HTTPException
from langchain.agents.middleware import AgentMiddleware, wrap_tool_call
from langchain.tools import ToolException
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command


def _error_text(error: Exception) -> str:
    if isinstance(error, HTTPException):
        text = str(error.detail) if error.status_code < 500 else f"服务失败 HTTP {error.status_code}"
    elif isinstance(error, (ValueError, OSError, ToolException)):
        text = str(error)
    else:
        text = f"工具执行异常：{type(error).__name__}"
    for key, value in os.environ.items():
        if any(word in key.upper() for word in ("API_KEY", "SECRET", "TOKEN", "PASSWORD")) and len(value) >= 8:
            text = text.replace(value, "[redacted]")
    text = re.sub(r"(?i)(?:sk-[a-z0-9_-]{8,}|bearer\s+\S+|(?:api[_ -]?key|password|secret)\s*[=:]\s*[^\s,;]+)", "[redacted]", text)
    return text[:2000]


def _error_message(identity: dict[str, Any], error: Exception) -> ToolMessage:
    return ToolMessage(
        content=f"工具调用失败：{_error_text(error)}",
        name=identity["name"],
        tool_call_id=identity["call_id"],
        status="error",
        additional_kwargs={"focus_tool_call": {**identity, "failure_kind": type(error).__name__}},
    )


def build_tool_error_middleware() -> AgentMiddleware:

    @wrap_tool_call
    async def handle_tool_errors(
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        context = request.runtime.context
        identity = {"name": request.tool_call["name"], "call_id": request.tool_call["id"],
                    "run_id": context.get("run_id") if isinstance(context, dict) else None}
        try:
            result = await handler(request)
            if isinstance(result, ToolMessage) and result.tool_call_id == identity["call_id"]:
                update = {"name": identity["name"], "additional_kwargs": {**result.additional_kwargs,
                    "focus_tool_call": {**result.additional_kwargs.get("focus_tool_call", {}), **identity}}}
                if result.status == "error":
                    update["content"] = _error_text(ToolException(str(result.content)))
                result = result.model_copy(update=update)
            return result
        except asyncio.CancelledError as error:
            error.add_note(f"tool={identity['name']} call_id={identity['call_id']} run_id={identity['run_id']}")
            raise
        except Exception as error:
            return _error_message(identity, error)

    return handle_tool_errors
