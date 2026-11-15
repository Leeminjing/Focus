"""Patrol 的只读联网请求：复用内置工具参数和执行，返回带来源的非可信观察。

输入为结构化 web_search/web_fetch 请求，输出为成功正文或可恢复错误。
仅允许这两个无本地副作用工具；不读取工作区、不改变 Mission 或执行授权。
"""

from typing import Annotated, Literal

from focus.tools.builtins.web_tools import web_fetch, web_search
from langchain_core.tools import ToolException
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class PatrolWebSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Literal["web_search"]
    arguments: web_search.args_schema


class PatrolWebFetchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Literal["web_fetch"]
    arguments: web_fetch.args_schema


PatrolWebToolRequest = Annotated[
    PatrolWebSearchRequest | PatrolWebFetchRequest, Field(discriminator="name")
]


async def read_web_evidence(request: PatrolWebToolRequest) -> dict:
    tool = {"web_search": web_search, "web_fetch": web_fetch}[request.name]
    arguments = request.arguments.model_dump()
    try:
        content = await tool.ainvoke(arguments)
    except (ToolException, ValidationError, ValueError) as exc:
        return {"kind": "web_tool", "name": request.name, "arguments": arguments,
                "status": "error", "content": str(exc)}
    return {"kind": "web_tool", "name": request.name, "arguments": arguments,
            "status": "success", "content": content}
