"""本文件对外提供 build_tool_error_middleware，同步构建桌面 Agent 的工具错误中间件。

输入为 LangChain ToolCallRequest 与下游 handler；输出为原工具结果，或针对可恢复业务错误
（ValueError / 路径形态错误 / 4xx / ToolException）的 ToolMessage。具体工作流为：调用下游
工具，捕获可恢复业务错误并复用原 tool_call_id 构造错误消息，其他异常继续传播。

可恢复集合含 ToolException（LangChain 官方「工具可恢复错误」类型，web_search/web_fetch 等
工具的网络/业务失败即抛此类型）—— 闭合为 ToolMessage 后模型可看到失败原因并生成收尾
AI 消息，而非异常传播导致 run error 无收尾。示例：
`middlewares = [build_tool_error_middleware()]`。
"""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException
from langchain.agents.middleware import AgentMiddleware, wrap_tool_call
from langchain.tools import ToolException
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command


def _error_text(error: Exception) -> str:
    if isinstance(error, HTTPException):
        return str(error.detail)
    return str(error)


def build_tool_error_middleware() -> AgentMiddleware:
    """构建处理可恢复输入/业务/工具错误的 LangChain 工具 middleware。"""

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
        except ToolException as error:
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
