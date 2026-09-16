r"""本文件对外提供 RunExecutionResources 与 execute_prepared_run 统一 Agent 执行脊柱。

输入为可信 Run 请求、执行 thread、运行时资源、可选 Agent factory 与测试用 runner seam；输出为已登记并启动的
RunRecord。具体工作流为验证服务端 SecurityContext、还原消息或 resume Command、绑定 checkpoint
与 namespace、创建 RunRecord，并且只在此处启动 run_agent；若存在 workspace lease，则伴随 Agent task
续约，续约失败立即 fence 并取消 Run。示例：
`record = await execute_prepared_run(body, thread_id, resources, factory)`。
"""

from __future__ import annotations

import asyncio
from contextvars import Context
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import HTTPException
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from focus.config.app_config import AppConfig
from focus.runtime.checkpointer.namespaced import NamespacedCheckpointer
from focus.runtime.runs.events import deserialize_messages
from focus.runtime.runs.limits import DEFAULT_AGENT_RECURSION_LIMIT
from focus.runtime.runs.manager import RunManager, RunRecord
from focus.runtime.runs.schemas import DisconnectMode
from focus.runtime.runs.worker import run_agent
from focus.runtime.stream_bridge.base import StreamBridge
from focus.security.context import has_security_context


@dataclass(frozen=True, slots=True)
class RunExecutionResources:
    bridge: StreamBridge
    run_manager: RunManager
    checkpointer: Any
    store: Any
    app_config: AppConfig


async def execute_prepared_run(
    body: Any,
    thread_id: str,
    resources: RunExecutionResources,
    agent_factory: Callable[[], Awaitable[CompiledStateGraph]] | None = None,
    *,
    runner: Callable[..., Awaitable[Any]] | None = None,
) -> RunRecord:
    context = _context_dict(body)
    _require_governed_context(context)
    record = resources.run_manager.create(
        thread_id=thread_id,
        run_id=context.get("run_id") or None,
        on_disconnect=DisconnectMode.cancel,
        model_name=context.get("model_name"),
    )
    checkpointer = resources.checkpointer
    checkpoint_ns = context.get("checkpoint_ns")
    if checkpoint_ns:
        checkpointer = NamespacedCheckpointer(checkpointer, checkpoint_ns)
    execute = runner or run_agent
    task = asyncio.create_task(
        execute(
            record=record,
            bridge=resources.bridge,
            run_manager=resources.run_manager,
            app_config=resources.app_config,
            graph_input=_graph_input(body),
            runnable_config=_runnable_config(thread_id, record.run_id, context),
            stream_modes=getattr(body, "stream_mode", None) or ["values"],
            langgraph_context={**context, "app_config": context.get("app_config", resources.app_config)},
            agent_factory=agent_factory,
            checkpointer=checkpointer,
            store=resources.store,
        ),
        context=Context(),
    )
    record.task = task
    lease = context.get("workspace_lease")
    renew = context.get("workspace_lease_renew")
    if isinstance(lease, dict) and callable(renew):
        heartbeat = asyncio.create_task(
            _maintain_workspace_lease(record, resources.run_manager, renew, lease)
        )
        setattr(record, "workspace_lease_task", heartbeat)
    return record


async def _maintain_workspace_lease(record, run_manager, renew, lease: dict[str, Any]) -> None:
    ttl_seconds = max(10, int(lease.get("ttl_seconds") or 120))
    interval = max(3, min(30, ttl_seconds // 3))
    try:
        while record.task is not None and not record.task.done():
            await asyncio.sleep(interval)
            if record.task.done():
                return
            await renew(
                str(lease["lease_id"]),
                int(lease["fencing_token"]),
                ttl_seconds,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        run_manager.cancel(record.run_id, action="interrupt")


def _context_dict(body: Any) -> dict[str, Any]:
    context = getattr(body, "context", None)
    if not context:
        return {}
    return context if isinstance(context, dict) else context.model_dump()


def _require_governed_context(context: dict[str, Any]) -> None:
    if not has_security_context(context):
        raise HTTPException(
            422,
            {
                "code": "execution_profile_required",
                "message": "运行上下文必须来自服务端登记的执行身份；调用方不得自行提供受治理字段",
            },
        )


def _graph_input(body: Any) -> dict[str, Any] | Command:
    resume = getattr(body, "resume", None)
    if resume is not None:
        return Command(resume=resume)
    raw_input = getattr(body, "input", None)
    if not raw_input:
        return {}
    if not isinstance(raw_input, dict):
        return raw_input
    messages = raw_input.get("messages", [])
    serialized = [message for message in messages if isinstance(message, dict)]
    if serialized:
        return {"messages": deserialize_messages(serialized)}
    return {"messages": [message for message in messages if isinstance(message, BaseMessage)]}


def _runnable_config(
    thread_id: str, run_id: str, context: dict[str, Any]
) -> RunnableConfig:
    configurable: dict[str, Any] = {"thread_id": thread_id, "run_id": run_id}
    if context.get("checkpoint_id") is not None:
        configurable["checkpoint_id"] = context["checkpoint_id"]
    return {
        "max_concurrency": None,
        "recursion_limit": DEFAULT_AGENT_RECURSION_LIMIT,
        "metadata": {"run_id": run_id},
        "configurable": configurable,
    }
