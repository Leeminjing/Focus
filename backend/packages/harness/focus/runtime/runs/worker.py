"""
本文件对外提供 `run_agent` 异步函数，作为统一 agent 执行层的主入口。

对外提供:
    run_agent — 在后台 asyncio task 中运行 agent graph，将 stream chunk 通过 StreamBridge 发布为统一信封 SSE 事件

输入为:
    record: RunRecord — 当前 run 的运行时档案，含 run_id、model_name、abort_event 等
    bridge: StreamBridge — 进程内事件总线，worker 通过它发布事件
    run_manager: RunManager — run 状态管理注册表，用于更新状态
    app_config: AppConfig — 组合根配置
    graph_input: dict — agent graph 的输入（含 messages 等）
    runnable_config: RunnableConfig — LangGraph 执行配置
    stream_modes: list[str] | str | None — 前端传入的 stream mode（messages-tuple → tokens 事件，values → events 事件）
    agent_name: str | None — agent 名称（缺省装配路径使用）
    tool_groups: list[str] | None — 工具分组过滤（缺省装配路径使用）
    langgraph_context: dict | None — 运行上下文，传给 agent.astream(context=...)；
        必须携带服务端派生的安全上下文（其中 owner 传给 make_lead_agent，路由身份用于组装
        事件信封）；缺失合法安全上下文即拒绝执行
    agent_factory: Callable | None — 自定义装配函数（await 后返回 CompiledStateGraph），
        缺省使用 make_lead_agent；桌面经此注入模型、工具、prompt 与 checkpoint 包装
    checkpointer: BaseCheckpointSaver | None — checkpoint 持久化器，None 时不启用
    store: BaseStore | None — 跨 thread 长期记忆存储，None 时不启用

输出为:
    统一信封 SSE 事件流 → bridge → 前端；最终状态 → run_manager

具体工作流为:
    (0) 强制边界：校验运行上下文携带合法安全上下文，缺失即拒绝执行并落到 error 终态
    (1) 设置 run 状态为 running，发布信封 metadata 事件
    (2) 读取当前 thread 的旧 checkpoint 保存为 rollback 快照（checkpointer 可用时）
    (3) 通过 agent_factory（缺省 make_lead_agent）创建 agent
    (3.5) 将 checkpointer 挂载到 agent.checkpointer，将 store 挂载到 agent.store
    (4) 翻译 stream_modes（前端名称 → LangGraph 内部名称），统一为列表模式
    (5) 把当前 Run 的取消信号绑定到受治理执行上下文，调用 agent.astream()，每轮检查 abort_event
        使用 durability=sync，prepared Items/snapshot/manifest checkpoint 落盘后才执行下一个 sampling 节点
    (6) 每个 chunk 经 focus.runtime.runs.events 转换为统一信封事件 publish 到 bridge
        （messages 模式 → tokens；values 模式 → events）
    (7) 终态处理：success / interrupted（rollback 时用旧 checkpoint 恢复 thread 状态）/ error
    (8) finally: publish_end 关流 + 延迟缓存清理

示例:
    task = asyncio.create_task(run_agent(
        record=record,
        bridge=bridge,
        run_manager=run_manager,
        app_config=app_config,
        graph_input={"messages": [HumanMessage(content="你好")]},
        runnable_config={"configurable": {"thread_id": "th-001"}},
        stream_modes=["values"],
        langgraph_context={"model_name": "deepseek-v4-flash", "app_config": app_config, "user_id": "uuid-xxx"},
    ))
精确 checkpoint 的普通新输入通过 update_state 创建分叉；Command(resume) 直接恢复原中断任务，不经过 START 分叉，保留原 Run 的工具 ledger。
"""

import logging
from dataclasses import replace
from typing import Any, Awaitable, Callable

from langchain_core.runnables import RunnableConfig

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.types import Command

from focus.agents.lead import make_lead_agent
from focus.config.app_config import AppConfig
from focus.runtime.runs.events import (
    build_envelope,
    chunk_to_events,
    extract_interrupts,
)
from focus.runtime.runs.manager import RunManager, RunRecord
from focus.runtime.runs.schemas import RunStatus
from focus.runtime.runs.usage import StreamUsageAccumulator
from focus.runtime.stream_bridge.base import StreamBridge
from focus.runtime.stream_bridge.schemas import StreamEvent
from focus.security.context import (
    SecurityContext,
    has_security_context,
    security_context_of,
)

logger = logging.getLogger(__name__)


_STREAM_MODE_MAP: dict[str, str] = {
    "messages-tuple": "messages",
    "values": "values",
    "updates": "updates",
    "custom": "custom",
}


_SKIP_MODES: frozenset = frozenset({"events"})


def _map_stream_modes(stream_modes: list[str] | str | None) -> list[str] | str | None:


    if not stream_modes:
        return ["values"]

    if isinstance(stream_modes, str):
        return _STREAM_MODE_MAP.get(stream_modes, stream_modes)

    mapped: list[str] = []
    for mode in stream_modes:
        if mode in _SKIP_MODES:
            logger.info("stream_mode '%s' 与 values 不兼容，已跳过", mode)
            continue
        mapped.append(_STREAM_MODE_MAP.get(mode, mode))

    if not mapped:
        return ["values"]

    return mapped


def _envelope_base(record: RunRecord, security: SecurityContext) -> dict[str, Any]:


    return {
        "workspace_id": security.routing.workspace_id,
        "thread_id": security.routing.thread_id,
        "agent_id": security.routing.agent_id,
        "run_id": record.run_id,
    }


def _error_envelope_base(record: RunRecord, langgraph_context: dict | None) -> dict[str, Any]:


    if has_security_context(langgraph_context):
        return _envelope_base(record, security_context_of(langgraph_context))
    return {
        "workspace_id": None,
        "thread_id": record.thread_id,
        "agent_id": record.run_id,
        "run_id": record.run_id,
    }


async def run_agent(
    *,
    record: RunRecord,
    bridge: StreamBridge,
    run_manager: RunManager,
    app_config: AppConfig,
    graph_input: dict | Command,
    runnable_config: RunnableConfig,
    stream_modes: list[str] | str | None = None,
    agent_name: str | None = None,
    tool_groups: list[str] | None = None,
    langgraph_context: dict | None = None,
    agent_factory: Callable[[], Awaitable[CompiledStateGraph]] | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore | None = None,
) -> None:
    agent_ready = False
    try:


        security = security_context_of(langgraph_context)
        security = replace(
            security,
            extras={**security.extras, "sandbox_cancel_event": record.abort_event},
        )
        langgraph_context = {**langgraph_context, **security.to_runtime_context()}


        run_manager.update(record.run_id, status=RunStatus.running)
        logger.info("run '%s' 状态 → running", record.run_id)

        env_base = _envelope_base(record, security)
        metadata_event = StreamEvent(
            id="",
            event="metadata",
            data=build_envelope(
                env_base["workspace_id"], env_base["thread_id"], env_base["agent_id"],
                record.run_id, "metadata", {"status": "running"},
            ),
        )
        bridge.publish(record.run_id, metadata_event)


        rollback_snapshot = None
        if checkpointer is not None:
            try:
                config_for_latest = {"configurable": {"thread_id": record.thread_id}}
                latest = await checkpointer.aget_tuple(config_for_latest)
                if latest is not None:
                    rollback_snapshot = latest
                    logger.info("run '%s' 已保存 rollback 快照 (checkpoint_id=%s)", record.run_id, latest.checkpoint.get("id", "?"))
                else:
                    logger.info("run '%s' thread 无旧 checkpoint，rollback 快照为空", record.run_id)
            except Exception:
                logger.warning("run '%s' 读取旧 checkpoint 失败，rollback 不可用", record.run_id, exc_info=True)


        model_name = record.model_name
        mapped_stream_modes = _map_stream_modes(stream_modes)
        user_id = security.owner

        if agent_factory is not None:
            agent = await agent_factory()
        else:

            agent = await make_lead_agent(
                model_name=model_name or app_config.resolve_default_model_name(),
                agent_name=agent_name,
                tool_groups=tool_groups,
                user_id=user_id,
            )
        agent_ready = True


        if checkpointer is not None:
            agent.checkpointer = checkpointer
        if store is not None:
            agent.store = store


        stream_input = graph_input
        checkpoint_id = runnable_config.get("configurable", {}).get("checkpoint_id")
        if checkpoint_id is not None and not isinstance(graph_input, Command):
            fork_input_config = {
                **runnable_config,
                "configurable": {
                    **runnable_config.get("configurable", {}),
                    "checkpoint_ns": "",
                },
            }
            fork_config = await agent.aupdate_state(
                fork_input_config,
                graph_input,
                as_node=START,
            )
            runnable_config = {
                **runnable_config,
                "configurable": {
                    **runnable_config.get("configurable", {}),
                    **fork_config.get("configurable", {}),
                    "run_id": record.run_id,
                },
            }
            stream_input = None


        requested_stream_modes = list(mapped_stream_modes) if isinstance(mapped_stream_modes, list) else [mapped_stream_modes]


        stream_modes_list = list(dict.fromkeys([*requested_stream_modes, "messages", "values", "custom"]))
        usage = StreamUsageAccumulator()
        graph_interrupted = False
        async for mode, chunk in agent.astream(
            stream_input,
            config=runnable_config,
            context=langgraph_context,
            stream_mode=stream_modes_list,
            durability="sync",
        ):
            usage.observe(mode, chunk)
            current_usage = usage.total()
            record.model_call_count = current_usage.model_calls
            record.prompt_input_tokens = current_usage.input_tokens
            record.prompt_output_tokens = current_usage.output_tokens
            record.prompt_cache_hit_tokens = current_usage.cache_read_input_tokens

            if record.abort_event.is_set():
                logger.info("run '%s' 收到 abort 信号，停止执行", record.run_id)
                break


            interrupts = extract_interrupts(chunk)
            if interrupts:
                graph_interrupted = True
                for interrupt_data in interrupts:
                    bridge.publish(
                        record.run_id,
                        StreamEvent(
                            id="",
                            event="interrupt",
                            data=build_envelope(
                                env_base["workspace_id"], env_base["thread_id"],
                                env_base["agent_id"], record.run_id,
                                "interrupt", interrupt_data,
                            ),
                        ),
                    )
                continue


            if mode not in requested_stream_modes and mode == "messages":
                continue
            for chunk_event in chunk_to_events(mode, chunk, env_base):
                bridge.publish(record.run_id, chunk_event)


        if record.abort_event.is_set():
            action = record.abort_action
            if action == "rollback":
                if rollback_snapshot is not None and checkpointer is not None:
                    try:
                        await checkpointer.aput(
                            rollback_snapshot.config,
                            rollback_snapshot.checkpoint,
                            rollback_snapshot.metadata,
                            {},
                        )
                        logger.info("run '%s' rollback 已恢复旧 checkpoint", record.run_id)
                    except Exception:
                        logger.error("run '%s' rollback 恢复 checkpoint 失败", record.run_id, exc_info=True)
                else:
                    logger.info("run '%s' rollback 无旧快照可恢复", record.run_id)
                run_manager.update(record.run_id, status=RunStatus.interrupted)
            else:
                run_manager.update(record.run_id, status=RunStatus.interrupted)
                logger.info("run '%s' 状态 → interrupted", record.run_id)
        elif graph_interrupted:
            run_manager.update(record.run_id, status=RunStatus.interrupted)
            logger.info("run '%s' 状态 → interrupted (graph interrupt)", record.run_id)
        else:
            run_manager.update(record.run_id, status=RunStatus.success)
            logger.info("run '%s' 状态 → success", record.run_id)

    except Exception as exc:
        logger.error("run '%s' 异常: %s", record.run_id, exc, exc_info=True)


        status = (
            RunStatus.interrupted
            if isinstance(graph_input, Command) and not agent_ready
            else RunStatus.error
        )
        run_manager.update(record.run_id, status=status, error=str(exc))
        if status is RunStatus.interrupted:
            logger.info(
                "run '%s' resume 装配失败，保留 interrupted checkpoint",
                record.run_id,
            )
        error_envelope = _error_envelope_base(record, langgraph_context)
        error_event = StreamEvent(
            id="",
            event="error",
            data=build_envelope(
                error_envelope["workspace_id"], error_envelope["thread_id"],
                error_envelope["agent_id"], record.run_id, "error", {"error": str(exc)},
            ),
        )
        try:
            bridge.publish(record.run_id, error_event)
        except Exception:
            logger.error("发布 error 事件失败", exc_info=True)

    finally:

        bridge.publish_end(record.run_id)
        bridge.cleanup(record.run_id, delay=300)
        logger.info("run '%s' 流资源已关流，延迟清理已安排", record.run_id)
