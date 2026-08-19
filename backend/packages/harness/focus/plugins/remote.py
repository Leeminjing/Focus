"""
本文件对外提供 RemotePlugin 进程外插件宿主与远程工具/hook 包装函数。

对外提供:
    RemotePlugin — 子进程 + stdio JSON Lines 语言中立协议的插件进程宿主
    build_remote_declaration(remote, manifest) — 握手后把能力上报构造成 PluginDeclaration
    close_remote_plugins() — 终止全部已启动的远程插件进程（atexit 注册）

输入:
    RemotePlugin(command, args, plugin_dir):
        command: str — 插件声明的启动命令（系统只 spawn，不准备环境）
        args: list[str] — 命令行参数
        plugin_dir: Path — 进程工作目录（插件目录）

输出:
    RemotePlugin.request(payload, timeout) → dict — 异步请求（工具/hook 调用，id 关联响应）
    RemotePlugin.request_sync(payload, timeout) → dict — 同步请求（加载期握手）
    build_remote_declaration → PluginDeclaration（与 build_plugin 同构，复用统一链路）

具体工作流:
    (1) 协议为逐行 JSON：系统发送 {"type": ..., "id": ...}，插件进程在 stdout 逐行回复
        同 id 响应；hello 上报 tools（name/description/schema）与 hooks（接口名列表）
    (2) 请求响应经后台 reader 线程分发：异步上下文用 asyncio.Future（call_soon_threadsafe），
        加载期握手用同步槽（threading.Event）；进程退出 → 全部 pending 立即失败
    (3) 远程工具：JSON Schema → Pydantic args_schema，包装为 StructuredTool（120s 超时，
        失败 → ToolException 经既有工具错误流闭合）
    (4) 远程 hook：state/runtime 经 serialize_value 序列化往返（与统一 SSE 信封同源）；
        hook_result.updates 语义与进程内 hook 返回 dict 相同

示例:
    remote = RemotePlugin("node", ["plugin.mjs"], Path("plugins/node-tool"))
    declaration = build_remote_declaration(remote, manifest)
"""

import asyncio
import atexit
import itertools
import json
import logging
import subprocess
import threading
from pathlib import Path
from typing import Any

import pydantic
from langchain_core.tools import StructuredTool, ToolException

from focus.plugins.schemas import PluginDeclaration, PluginManifest
from focus.runtime.runs.events import serialize_value

logger = logging.getLogger(__name__)

HELLO_TIMEOUT_SECONDS = 30.0
TOOL_TIMEOUT_SECONDS = 120.0
PROCESS_EXIT_MESSAGE = "插件进程已退出"

_TYPE_MAP: dict[str, Any] = {
    "string": str, "integer": int, "number": float,
    "boolean": bool, "array": list, "object": dict,
}

_remote_handles: list["RemotePlugin"] = []


_counter = itertools.count(1)


def _new_id() -> str:
    return str(next(_counter))


class _SyncSlot:
    """加载期握手用的同步响应槽（reader 线程写入，loader 线程等待）。"""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: dict | None = None


class RemotePlugin:
    def __init__(self, command: str, args: list[str], plugin_dir: Path) -> None:
        self._pending: dict[str, asyncio.Future | _SyncSlot] = {}
        self._lock = threading.Lock()
        try:
            self._proc = subprocess.Popen(
                [command, *args],
                cwd=str(plugin_dir),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"命令不存在: {command}") from exc
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        _remote_handles.append(self)

    def _read_loop(self) -> None:
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                with self._lock:
                    entry = self._pending.pop(str(payload.get("id")), None)
                if entry is None:
                    continue
                if isinstance(entry, asyncio.Future):
                    loop = entry.get_loop()
                    try:
                        loop.call_soon_threadsafe(entry.set_result, payload)
                    except RuntimeError:
                        pass  # 事件循环已关闭（进程退出竞态）
                else:
                    entry.result = payload
                    entry.event.set()
        finally:
            self._fail_pending()

    def _fail_pending(self) -> None:
        with self._lock:
            entries = list(self._pending.items())
            self._pending.clear()
        for _, entry in entries:
            if isinstance(entry, asyncio.Future):
                loop = entry.get_loop()
                try:
                    loop.call_soon_threadsafe(
                        entry.set_exception, RuntimeError(PROCESS_EXIT_MESSAGE),
                    )
                except RuntimeError:
                    pass
            else:
                entry.result = {"type": "error", "error": PROCESS_EXIT_MESSAGE}
                entry.event.set()

    def _alive(self) -> None:
        if self._proc.poll() is not None:
            raise RuntimeError(PROCESS_EXIT_MESSAGE)

    def _write(self, payload: dict) -> None:
        try:
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise RuntimeError(PROCESS_EXIT_MESSAGE) from exc

    def _register(self, rid: str, entry: asyncio.Future | _SyncSlot) -> None:
        with self._lock:
            self._pending[rid] = entry

    def _drop(self, rid: str) -> None:
        with self._lock:
            self._pending.pop(rid, None)

    def request_sync(self, payload: dict, timeout: float) -> dict:
        """同步请求（仅加载期握手使用）。"""
        self._alive()
        rid = _new_id()
        slot = _SyncSlot()
        self._register(rid, slot)
        try:
            self._write({**payload, "id": rid})
            if not slot.event.wait(timeout):
                raise TimeoutError(f"插件响应超时: {payload.get('type')}")
        finally:
            self._drop(rid)
        if slot.result is None:
            raise RuntimeError(PROCESS_EXIT_MESSAGE)
        return _check_result(slot.result)

    async def request(self, payload: dict, timeout: float | None = None) -> dict:
        """异步请求（工具/hook 调用）。"""
        self._alive()
        rid = _new_id()
        future = asyncio.get_running_loop().create_future()
        self._register(rid, future)
        try:
            self._write({**payload, "id": rid})
            result = await asyncio.wait_for(future, timeout) if timeout else await future
        finally:
            if not future.done():
                future.cancel()
            self._drop(rid)
        return _check_result(result)

    def close(self) -> None:
        if self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass
        self._fail_pending()


def _check_result(payload: dict) -> dict:
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    return payload


def _args_model(name: str, schema: dict) -> type | None:
    """JSON Schema → Pydantic args 模型（v1 只做一层类型映射）。"""
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    fields: dict[str, Any] = {}
    for key, prop in properties.items():
        if not isinstance(prop, dict) or not key.isidentifier():
            continue
        py_type = _TYPE_MAP.get(prop.get("type"), str)
        fields[key] = (py_type, ...) if key in required else (py_type, None)
    if not fields:
        return None
    model_name = "".join(chunk for chunk in name.title().split("-") if chunk) + "Args"
    return pydantic.create_model(model_name, **fields)


def build_remote_tool(remote: RemotePlugin, spec: dict) -> StructuredTool:
    async def call(**kwargs: Any) -> str:
        try:
            result = await remote.request(
                {"type": "tool_call", "name": spec["name"], "args": kwargs},
                timeout=TOOL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            raise ToolException(str(exc)) from exc
        content = result.get("content")
        return content if isinstance(content, str) else str(content)

    return StructuredTool.from_function(
        coroutine=call,
        name=str(spec["name"]),
        description=str(spec.get("description") or spec["name"]),
        args_schema=_args_model(str(spec["name"]), spec.get("schema") or {}),
    )


def build_remote_hook(remote: RemotePlugin, interface: str) -> Any:
    if interface in ("hook.before_tool", "hook.after_tool"):
        return _build_remote_tool_hook(remote, interface)
    return _build_remote_agent_hook(remote, interface)


def _build_remote_agent_hook(remote: RemotePlugin, interface: str) -> Any:
    async def hook(state: dict, runtime: Any) -> dict | None:
        result = await remote.request({
            "type": "hook",
            "interface": interface,
            "payload": {
                "state": serialize_value(state),
                "runtime": serialize_value(runtime),
            },
        })
        updates = result.get("updates")
        return updates if isinstance(updates, dict) else None

    return hook


def _build_remote_tool_hook(remote: RemotePlugin, interface: str) -> Any:
    if interface == "hook.before_tool":
        async def hook(request: Any) -> Any:
            result = await remote.request({
                "type": "hook",
                "interface": interface,
                "payload": {"tool_call": serialize_value(request.tool_call)},
            })
            updates = result.get("updates") or {}
            new_call = updates.get("tool_call")
            if isinstance(new_call, dict) and hasattr(request, "model_copy"):
                return request.model_copy(update={"tool_call": new_call})
            return None

        return hook

    async def hook(request: Any, result_msg: Any) -> None:
        await remote.request({
            "type": "hook",
            "interface": interface,
            "payload": {
                "tool_call": serialize_value(request.tool_call),
                "result": {
                    "content": serialize_value(getattr(result_msg, "content", "")),
                    "tool_call_id": getattr(result_msg, "tool_call_id", ""),
                    "name": getattr(result_msg, "name", ""),
                },
            },
        })

    return hook


def build_remote_declaration(
    remote: RemotePlugin, manifest: PluginManifest,
) -> PluginDeclaration:
    """握手并构造与 build_plugin 同构的声明（复用统一校验/注入链路）。"""
    hello = remote.request_sync({"type": "hello"}, timeout=HELLO_TIMEOUT_SECONDS)
    tools = [build_remote_tool(remote, spec) for spec in hello.get("tools", []) if isinstance(spec, dict)]
    hooks: dict[str, list[Any]] = {}
    for name in hello.get("hooks", []) if isinstance(hello.get("hooks"), list) else []:
        if isinstance(name, str):
            hooks.setdefault(name, []).append(build_remote_hook(remote, name))
    logger.info(
        "插件 %s 远程能力上报: tools=%d hooks=%s",
        manifest.name, len(tools), sorted(hooks),
    )
    return PluginDeclaration(tools=tools, hooks=hooks)


def close_remote_plugins() -> None:
    for remote in _remote_handles:
        remote.close()
    _remote_handles.clear()


atexit.register(close_remote_plugins)
