"""本文件对外提供 build_tool_error_middleware，同步构建桌面 Agent 的工具错误中间件。

输入为 LangChain ToolCallRequest 与下游 handler；输出为原工具结果，或针对 ValueError / FastAPI
4xx HTTPException 的 ToolMessage。具体工作流为：调用下游工具，捕获可恢复业务错误并复用原
tool_call_id 构造错误消息，其他异常继续传播。示例：
`middlewares = [build_tool_error_middleware()]`。
"""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException
from langchain.agents.middleware import AgentMiddleware, wrap_tool_call
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command


def _error_text(error: Exception) -> str:
    if isinstance(error, HTTPException):
        return str(error.detail)
    return str(error)


def build_tool_error_middleware() -> AgentMiddleware:
    """构建仅处理可恢复输入/4xx 业务错误的 LangChain 工具 middleware。"""

    @wrap_tool_call
    async def handle_tool_errors(
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        try:
            return await handler(request)
        except (ValueError, FileNotFoundError, NotADirectoryError, IsADirectoryError) as error:
            return ToolMessage(
                content=f"工具调用失败：{_error_text(error)}",
                tool_call_id=request.tool_call["id"],
            )
        except HTTPException as error:
            if 400 <= error.status_code < 500:
                return ToolMessage(
                    content=f"工具调用失败：{_error_text(error)}",
                    tool_call_id=request.tool_call["id"],
                )
            raise

    return handle_tool_errors
