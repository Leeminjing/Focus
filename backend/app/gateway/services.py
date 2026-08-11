"""
本文件对外提供 `start_run` 异步函数，作为统一 agent 链路的编排层核心。

对外提供:
    start_run — 编排实现层需要的参数与对象，最后创建 asyncio task 调用实现层

输入:
    body: Any — RunCreateRequest 解析后的请求体（input.messages / context / stream_mode）
    thread_id: str — 请求查询参数中的 thread_id
    request: Request — FastAPI Request 对象，用于获取 app.state 中的资源
    agent_factory: Callable | None — 自定义装配函数，None 时 worker 使用 make_lead_agent

输出:
    RunRecord — 新创建的 run 运行时档案

具体工作流:
    (1) 从 request.app.state 获取 StreamBridge、RunManager、Checkpointer、Store
    (2) 通过 get_app_config("config.yaml") 获取 AppConfig
    (3) 从 body.context 提取运行参数（model_name/workspace_id/agent_id/permissions/skills/checkpoint_ns/workspace 等）
    (4) 创建 RunRecord（初始状态 pending）
    (5) 组装参数：input.messages → BaseMessage（deserialize_messages 全角色还原）、
        RunnableConfig、context（透传 + user_id）、stream_modes
    (6) context.checkpoint_ns 非空时以 NamespacedCheckpointer 包装 checkpointer
    (7) asyncio.create_task(run_agent(...)) 启动 worker
    (8) record.task = task，返回 RunRecord

示例:
    @router.post("/{thread_id}/runs/stream")
    async def stream_run(thread_id: str, body: RunCreateRequest, request: Request):
        record = await start_run(body, thread_id, request)
        return StreamingResponse(sse_consumer(...))
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable

from fastapi import Request
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from focus.config.app_config import get_app_config
from focus.runtime.checkpointer.namespaced import NamespacedCheckpointer
from focus.runtime.runs.events import deserialize_messages
from focus.runtime.runs.manager import RunManager, RunRecord
from focus.runtime.runs.schemas import DisconnectMode
from focus.runtime.runs.worker import run_agent
from focus.runtime.stream_bridge.base import StreamBridge

logger = logging.getLogger(__name__)

# Focus 主运行会在承诺交接后执行完整的多轮 ReAct 文件任务；LangGraph
# 默认 25 步会在最后一次工具返回后提前终止，无法生成最终交付消息。
_DEFAULT_RECURSION_LIMIT = 100


def _context_dict(body: Any) -> dict[str, Any]:
    """从请求体提取 context 字典。"""
    if hasattr(body, "context") and body.context:
        return body.context if isinstance(body.context, dict) else body.context.model_dump()
    return {}


async def start_run(
    body: Any,
    thread_id: str,
    request: Request,
    agent_factory: Callable[[], Awaitable[CompiledStateGraph]] | None = None,
) -> RunRecord:
    # (1) 从 request.app.state 获取资源
    bridge: StreamBridge = request.app.state.stream_bridge
    run_manager: RunManager = request.app.state.run_manager
    checkpointer = request.app.state.checkpointer
    store = request.app.state.store

    # (2) 获取 AppConfig
    app_config = get_app_config("config.yaml")

    # (3) 从 context 提取运行参数
    context = _context_dict(body)
    model_name = context.get("model_name")
    checkpoint_ns = context.get("checkpoint_ns")

    record = run_manager.create(
        thread_id=thread_id,
        run_id=context.get("run_id") or None,
        on_disconnect=DisconnectMode.cancel,
        model_name=model_name,
    )
    logger.info("RunRecord 已创建: run_id='%s', thread_id='%s'", record.run_id, thread_id)

    # (4) 组装参数

    # resume → Command(resume=...)（承诺层人工确认恢复同一 thread 的 interrupt）；
    # 否则 input.messages → BaseMessage（全角色还原，支持冻结消息重放）
    graph_input: dict | Command = {}
    if hasattr(body, "resume") and body.resume is not None:
        graph_input = Command(resume=body.resume)
    elif hasattr(body, "input") and body.input:
        raw_input = body.input
        if isinstance(raw_input, dict):
            msgs = raw_input.get("messages", [])
            from langchain_core.messages import BaseMessage

            plain = [m for m in msgs if isinstance(m, dict)]
            if plain:
                graph_input["messages"] = deserialize_messages(plain)
            else:
                graph_input["messages"] = [m for m in msgs if isinstance(m, BaseMessage)]
        else:
            graph_input = raw_input

    # RunnableConfig
    runnable_config: RunnableConfig = {
        "max_concurrency": None,
        "recursion_limit": _DEFAULT_RECURSION_LIMIT,
        "configurable": {
            "thread_id": thread_id,
            "run_id": record.run_id,
        },
    }

    # LangGraph context（透传桌面参数 + user_id）
    langgraph_context: dict = {**context}
    langgraph_context.setdefault("model_name", model_name)
    langgraph_context.setdefault("app_config", app_config)
    current_user = getattr(request.state, "current_user", None)
    langgraph_context.setdefault("user_id", str(current_user.id) if current_user is not None else None)

    # (5) checkpoint_ns 非空时包装 checkpointer（小兵命名空间隔离）
    if checkpoint_ns:
        checkpointer = NamespacedCheckpointer(checkpointer, checkpoint_ns)

    # stream_modes
    stream_modes: list[str] | str | None = None
    if hasattr(body, "stream_mode") and body.stream_mode:
        stream_modes = body.stream_mode
    if stream_modes is None:
        stream_modes = ["values"]

    # (6) asyncio.create_task 启动 worker
    task = asyncio.create_task(
        run_agent(
            record=record,
            bridge=bridge,
            run_manager=run_manager,
            app_config=app_config,
            graph_input=graph_input,
            runnable_config=runnable_config,
            stream_modes=stream_modes,
            langgraph_context=langgraph_context,
            agent_factory=agent_factory,
            checkpointer=checkpointer,
            store=store,
        )
    )

    # (7) 挂载 task 并返回
    record.task = task
    logger.info("worker 已启动: run_id='%s'", record.run_id)
    return record
