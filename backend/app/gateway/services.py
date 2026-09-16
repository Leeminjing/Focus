r"""本文件对外提供 start_run，作为 FastAPI 资源到统一 Run 执行脊柱的适配器。

输入为 Run 请求、thread_id、FastAPI Request 与可选 Agent factory；输出为由唯一
execute_prepared_run 脊柱启动的 RunRecord。具体工作流为从 app.state 读取共享运行时资源，装配
RunExecutionResources 后委托底层执行器；本文件不复制消息、checkpoint 或 Agent loop 逻辑。
示例：`record = await start_run(body, thread_id, request, factory)`。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import Request
from langgraph.graph.state import CompiledStateGraph

from backend.app.desktop.run_orchestration import (
    RunExecutionResources,
    execute_prepared_run,
)
from focus.config.app_config import get_app_config
from focus.runtime.runs.manager import RunRecord
from focus.runtime.runs.worker import run_agent


async def start_run(
    body: Any,
    thread_id: str,
    request: Request,
    agent_factory: Callable[[], Awaitable[CompiledStateGraph]] | None = None,
) -> RunRecord:
    resources = RunExecutionResources(
        bridge=request.app.state.stream_bridge,
        run_manager=request.app.state.run_manager,
        checkpointer=request.app.state.checkpointer,
        store=request.app.state.store,
        app_config=get_app_config("config.yaml"),
    )
    return await execute_prepared_run(
        body,
        thread_id,
        resources,
        agent_factory,
        runner=run_agent,
    )
