"""
本文件对外提供 PluginBridgeMiddleware 插件桥接中间件，作为插件实现进入 lead agent
装配链的唯一入口。

对外提供:
    PluginBridgeMiddleware(registry) — 承载插件工具注入与插件 hook 分发的 AgentMiddleware

输入:
    registry: PluginRegistry — 装配时快照使用的插件注册表

输出:
    tools 属性 → list[BaseTool]（注册表插件工具，经 create_agent 编译时并入工具节点）
    abefore_agent / abefore_model → dict | None（可变 hook 流水线合并 diff）
    aafter_model / aafter_agent → None（只读 hook，返回值丢弃）
    awrap_tool_call → ToolMessage | Command（before_tool → handler → after_tool）

具体工作流:
    (1) hook 分发按注册表稳定顺序逐插件执行；可变接口后一个插件看到前一个插件修改后的
        state（"Original → A changed → B changed → Final"），最终返回合并 diff
    (2) 每次插件调用按接口目录超时 asyncio.wait_for 包裹；同步回调经 asyncio.to_thread；
        超时记录 "Extension Timeout: Plugin=…, Interface=…" 并按失败策略继续（skip）或抛出（abort）
    (3) 失败归属插件实现（trace 记录插件名 + 接口名 + 错误文本），系统不做技术栈特殊处理
    (4) 本中间件仅实现异步钩子：系统运行链路全程 agent.astream（ponytail: 出现同步
        invoke 调用路径时再补 sync 变体）

示例:
    middlewares = [PluginBridgeMiddleware(get_plugin_registry())]
    graph = create_agent(model, tools, middleware=middlewares, system_prompt=...)
"""

import asyncio
import inspect
import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware

from focus.plugins.registry import HookImpl, PluginRegistry

logger = logging.getLogger(__name__)


class PluginBridgeMiddleware(AgentMiddleware):
    def __init__(self, registry: PluginRegistry) -> None:
        super().__init__()
        self._registry = registry

    @property
    def tools(self) -> list[Any]:
        return self._registry.tools()

    async def _invoke(
        self, impl: HookImpl, name: str, *args: Any
    ) -> tuple[Any, str | None]:
        iface = self._registry.catalog.get(name)
        timeout = iface.timeout_seconds if iface is not None else None
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            if inspect.iscoroutinefunction(impl.fn):
                call = impl.fn(*args)
            else:
                call = asyncio.to_thread(impl.fn, *args)
            result = await asyncio.wait_for(call, timeout) if timeout else await call
            self._registry.trace(name, impl.plugin, "success", (loop.time() - started) * 1000)
            return result, None
        except TimeoutError:
            error = f"Extension Timeout: Plugin={impl.plugin}, Interface={name}"
            self._registry.trace(
                name, impl.plugin, "timeout", (loop.time() - started) * 1000, error,
            )
            return None, error
        except Exception as exc:
            self._registry.trace(
                name, impl.plugin, "failed", (loop.time() - started) * 1000, str(exc),
            )
            return None, f"插件实现失败: Plugin={impl.plugin}, Interface={name}, error={exc}"

    def _abort(self, name: str, error: str) -> None:
        iface = self._registry.catalog.get(name)
        if iface is not None and iface.failure_policy == "abort":
            raise RuntimeError(error)

    async def _dispatch_mutable(
        self, name: str, state: dict, runtime: Any
    ) -> dict | None:
        updates: dict = {}
        current: dict = dict(state)
        for impl in self._registry.hooks(name):
            result, error = await self._invoke(impl, name, current, runtime)
            if error is not None:
                self._abort(name, error)
                continue
            if isinstance(result, dict):
                updates.update(result)
                current.update(result)
        return updates or None

    async def _dispatch_readonly(self, name: str, state: dict, runtime: Any) -> None:
        for impl in self._registry.hooks(name):
            result, error = await self._invoke(impl, name, state, runtime)
            if error is not None:
                self._abort(name, error)
            # 只读接口：返回值一律丢弃，不改变原始结果

    async def abefore_agent(self, state: dict, runtime: Any) -> dict | None:
        return await self._dispatch_mutable("hook.before_agent", state, runtime)

    async def abefore_model(self, state: dict, runtime: Any) -> dict | None:
        return await self._dispatch_mutable("hook.before_model", state, runtime)

    async def aafter_model(self, state: dict, runtime: Any) -> None:
        await self._dispatch_readonly("hook.after_model", state, runtime)

    async def aafter_agent(self, state: dict, runtime: Any) -> None:
        await self._dispatch_readonly("hook.after_agent", state, runtime)

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        req = request
        for impl in self._registry.hooks("hook.before_tool"):
            result, error = await self._invoke(impl, "hook.before_tool", req)
            if error is not None:
                self._abort("hook.before_tool", error)
                continue
            if result is not None:
                req = result
        outcome = await handler(req)
        for impl in self._registry.hooks("hook.after_tool"):
            _, error = await self._invoke(impl, "hook.after_tool", req, outcome)
            if error is not None:
                self._abort("hook.after_tool", error)
        return outcome
