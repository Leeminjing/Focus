"""桌面领域共享的值对象、约束或纯函数。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from langgraph.graph.state import CompiledStateGraph

from backend.app.gateway.routers.thread_runs import RunCreateRequest


@dataclass
class PreparedRun:
    """一次运行发起所需的编排输入：统一接口 body + agent_factory 闭包 + 响应载荷。

    agent_factory 为 None 表示幂等命中已有 run，无需发起（routes._launch 据此跳过）。
    """

    body: RunCreateRequest
    thread_id: str
    agent_factory: Callable[[], Awaitable[CompiledStateGraph]] | None
    payload: dict[str, Any]
