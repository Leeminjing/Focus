"""
本文件对外提供 `run_agent` 异步函数，作为统一 agent 执行层的主入口。

对外提供:
    run_agent — 在后台 asyncio task 中运行 agent graph，将 stream chunk 通过 StreamBridge 发布为统一信封 SSE 事件

输入:
    record: RunRecord — 当前 run 的运行时档案，含 run_id、model_name、abort_event 等
    bridge: StreamBridge — 进程内事件总线，worker 通过它发布事件
    run_manager: RunManager — run 状态管理注册表，用于更新状态
    app_config: AppConfig — 组合根配置
    graph_input: dict — agent graph 的输入（含 messages 等）
    runnable_config: RunnableConfig — LangGraph 执行配置
    stream_modes: list[str] | str | None — 前端传入的 stream mode（messages-tuple → tokens 事件，values → events 事件）
    agent_name: str | None — agent 名称（缺省装配路径使用）
    tool_groups: list[str] | None — 工具分组过滤（缺省装配路径使用）
    langgraph_context: dict | None — LangGraph context，传给 agent.astream(context=...)；
        其中 user_id 传给 make_lead_agent，workspace_id/agent_id 用于组装事件信封
    agent_factory: Callable | None — 自定义装配函数（await 后返回 CompiledStateGraph），
        缺省使用 make_lead_agent；桌面经此注入模型、工具、prompt 与 checkpoint 包装
    checkpointer: BaseCheckpointSaver | None — checkpoint 持久化器，None 时不启用
    store: BaseStore | None — 跨 thread 长期记忆存储，None 时不启用

输出:
    统一信封 SSE 事件流 → bridge → 前端；最终状态 → run_manager

具体工作流:
    (1) 设置 run 状态为 running，发布信封 metadata 事件
    (2) 读取当前 thread 的旧 checkpoint 保存为 rollback 快照（checkpointer 可用时）
    (3) 通过 agent_factory（缺省 make_lead_agent）创建 agent
    (3.5) 将 checkpointer 挂载到 agent.checkpointer，将 store 挂载到 agent.store
    (4) 翻译 stream_modes（前端名称 → LangGraph 内部名称），统一为列表模式
    (5) 调用 agent.astream()，每轮检查 abort_event
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
"""

import logging
from typing import Any, Awaitable, Callable

from langchain_core.runnables import RunnableConfig

from langgraph.checkpoint.base import BaseCheckpointSaver
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
from focus.runtime.stream_bridge.base import StreamBridge
from focus.runtime.stream_bridge.schemas import StreamEvent

logger = logging.getLogger(__name__)

# stream_mode 名称映射表：前端/SSE → LangGraph 内部名称
_STREAM_MODE_MAP: dict[str, str] = {
    "messages-tuple": "messages",
    "values": "values",
    "updates": "updates",
    "custom": "custom",
}

# 与 values 不兼容的 mode，需要跳过
_SKIP_MODES: frozenset = frozenset({"events"})


def _map_stream_modes(stream_modes: list[str] | str | None) -> list[str] | str | None:
    """将前端/SSE 名称翻译为 LangGraph 内部 stream_mode 名称。

    输入:
        stream_modes: list[str] | str | None — 前端传入的 stream mode

    输出:
        list[str] | str | None — 翻译后的 LangGraph stream_mode

    工作流:
        (1) 若为 None 或空，返回 ["values"] 作为默认
        (2) 若为单个字符串，按映射表翻译
        (3) 若为列表，逐项翻译，跳过不兼容的 mode（如 events）
        (4) 翻译后若为空列表，回退为 ["values"]
    """
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


def _envelope_base(record: RunRecord, langgraph_context: dict | None) -> dict[str, Any]:
    """组装统一事件信封的基础字段（workspace_id/thread_id/agent_id/run_id）。

    输入:
        record: RunRecord — 当前 run
        langgraph_context: dict | None — 运行上下文，提供 workspace_id/agent_id

    输出:
        dict — 信封基础字段
    """
    return {
        "workspace_id": langgraph_context.get("workspace_id") if langgraph_context else None,
        "thread_id": record.thread_id,
        "agent_id": langgraph_context.get("agent_id") if langgraph_context else record.run_id,
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
        # (1) 置 running，发信封 metadata 事件
        run_manager.update(record.run_id, status=RunStatus.running)
        logger.info("run '%s' 状态 → running", record.run_id)

        env_base = _envelope_base(record, langgraph_context)
        metadata_event = StreamEvent(
            id="",
            event="metadata",
            data=build_envelope(
                env_base["workspace_id"], env_base["thread_id"], env_base["agent_id"],
                record.run_id, "metadata", {"status": "running"},
            ),
        )
        bridge.publish(record.run_id, metadata_event)

        # (2) 读取旧 checkpoint 保存 rollback 快照
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

        # (3) 通过 agent_factory（缺省 make_lead_agent）创建 agent
        model_name = record.model_name or (app_config.models[0].name if app_config.models else None)
        mapped_stream_modes = _map_stream_modes(stream_modes)
        user_id = langgraph_context.get("user_id") if langgraph_context else None

        if agent_factory is not None:
            agent = await agent_factory()
        else:
            agent = await make_lead_agent(
                model_name=model_name or None,
                agent_name=agent_name,
                tool_groups=tool_groups,
                user_id=user_id,
            )
        agent_ready = True

        # (3.5) 挂载 checkpointer 和 store 到 agent
        if checkpointer is not None:
            agent.checkpointer = checkpointer
        if store is not None:
            agent.store = store

        # 从已验证 checkpoint 恢复时先创建 clean fork，避免 replay 后续坏写入。
        stream_input = graph_input
        checkpoint_id = runnable_config.get("configurable", {}).get("checkpoint_id")
        if checkpoint_id is not None:
            fork_input_config = {
                **runnable_config,
                "configurable": {
                    **runnable_config.get("configurable", {}),
                    "checkpoint_ns": "",
                },
            }
            fork_config = await agent.aupdate_state(fork_input_config, graph_input)
            runnable_config = {
                **runnable_config,
                "configurable": {
                    **runnable_config.get("configurable", {}),
                    **fork_config.get("configurable", {}),
                    "run_id": record.run_id,
                },
            }
            stream_input = None

        # (4) agent.astream 主循环（统一列表模式 → (mode, chunk) 元组）
        stream_modes_list = list(mapped_stream_modes) if isinstance(mapped_stream_modes, list) else [mapped_stream_modes]
        # 承诺层开启时强制追加 values（interrupt 快照与 lead 状态）与 custom（各角色消息轨迹）
        if app_config.commitment.enabled:
            stream_modes_list = list(dict.fromkeys([*stream_modes_list, "values", "custom"]))
        graph_interrupted = False
        async for mode, chunk in agent.astream(
            stream_input,
            config=runnable_config,
            context=langgraph_context,
            stream_mode=stream_modes_list,
        ):
            # (5) abort 中断检查
            if record.abort_event.is_set():
                logger.info("run '%s' 收到 abort 信号，停止执行", record.run_id)
                break

            # interrupt 识别：graph 暂停 → 发布 interrupt 事件，流结束后置 interrupted
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

            # 每个 chunk 经 events 模块转换为统一信封事件 publish 到 bridge
            for chunk_event in chunk_to_events(mode, chunk, env_base):
                bridge.publish(record.run_id, chunk_event)

        # (6) 终态处理
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
        # Resume 在 agent 装配完成前失败时，Command 尚未交给父图消费，原
        # interrupt checkpoint 仍然有效。保留 interrupted 语义，桌面端才能
        # 重新展示同一审批；普通运行或进入图后的异常仍是真正的 error。
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
        error_event = StreamEvent(
            id="",
            event="error",
            data=build_envelope(
                env_base["workspace_id"], env_base["thread_id"], env_base["agent_id"],
                record.run_id, "error", {"error": str(exc)},
            ),
        )
        try:
            bridge.publish(record.run_id, error_event)
        except Exception:
            logger.error("发布 error 事件失败", exc_info=True)

    finally:
        # (7) 关流 + 延迟清理
        bridge.publish_end(record.run_id)
        bridge.cleanup(record.run_id, delay=300)
        logger.info("run '%s' 流资源已关流，延迟清理已安排", record.run_id)
