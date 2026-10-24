"""UTF-8 JSONL connection to the independent Windows browser host.

Pipe threads work with Focus's Windows Selector event loop. All request completion
and application callbacks run on that loop; one slow browser never owns the reader.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import uuid


class BrowserError(RuntimeError):
    def __init__(self, code, message, detail=None):
        self.code, self.detail = code, detail or {}
        super().__init__(f"{code}: {message}")

    def payload(self):
        return {"code": self.code, "message": str(self), "detail": self.detail}


def executable_path():
    override = os.environ.get("FOCUS_DISTINCT_BROWSER_EXE")
    path = Path(override) if override else Path(__file__).resolve().parents[4] / "desktop/resources/distinct-browser/distinct-browser.exe"
    if os.name != "nt":
        raise BrowserError("unsupported_platform", "独立浏览器需要 Windows 11 和 WebView2 Runtime")
    if not path.is_file():
        raise BrowserError("executable_missing", "未找到 distinct-browser.exe；请运行 npm --prefix desktop run build:browser，或设置 FOCUS_DISTINCT_BROWSER_EXE")
    return path.resolve()


class BrowserTransport:
    def __init__(self, executable=None, on_event=lambda event: None, timeout=55):
        self.executable = executable
        self.on_event, self.timeout = on_event, timeout
        self.process = None
        self.failure = None
        self.pending = {}
        self.host_id = uuid.uuid4().hex
        self.pid = None
        self._start_lock = asyncio.Lock()
        self._outgoing = queue.Queue(maxsize=128)
        self._stderr = bytearray()

    async def start(self):
        async with self._start_lock:
            self.check()
            if self.process is not None:
                return
            self._loop = asyncio.get_running_loop()
            self._ready = self._loop.create_future()
            try:
                command = self.executable if isinstance(self.executable, list) else [str(self.executable or executable_path())]
                self.process = subprocess.Popen(
                    command, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    bufsize=0,
                )
                for target in (self._read, self._write, self._read_stderr):
                    threading.Thread(target=target, daemon=True).start()
                await asyncio.wait_for(asyncio.shield(self._ready), 10)
            except BaseException as error:
                self._disconnect(BrowserError("connection_lost", f"浏览器宿主启动失败：{error}"))
                raise

    def check(self):
        if self.failure:
            raise self.failure

    def _post(self, callback, *args):
        try:
            self._loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:
            pass  # Application loop has already shut down.

    def _read(self):
        try:
            with io.BufferedReader(self.process.stdout, buffer_size=65536) as pipe:
                while True:
                    line = pipe.readline(36 * 1024 * 1024 + 1)
                    if not line:
                        break
                    if len(line) > 36 * 1024 * 1024 or not line.endswith(b"\n"):
                        raise ValueError("invalid host output frame")
                    self._post(self._receive, json.loads(line.decode("utf-8")))
        except Exception as error:
            self._post(self._disconnect, BrowserError("connection_lost", str(error)))
        finally:
            self._post(self._disconnect, BrowserError("connection_lost", "浏览器宿主输出连接已关闭"))

    def _write(self):
        try:
            with self.process.stdin as pipe:
                while True:
                    data = self._outgoing.get()
                    if data is None:
                        return
                    remaining = memoryview(data)
                    while remaining:
                        written = pipe.write(remaining)
                        if not written:
                            raise BrokenPipeError("browser stdin closed")
                        remaining = remaining[written:]
                    pipe.flush()
        except Exception as error:
            self._post(self._disconnect, BrowserError("connection_lost", str(error)))

    def _read_stderr(self):
        with self.process.stderr as pipe:
            while data := pipe.read(1024):
                self._stderr.extend(data)
                del self._stderr[:-8192]

    def _receive(self, message):
        if self.failure:
            return
        if not isinstance(message, dict):
            self._disconnect(BrowserError("protocol_error", "浏览器宿主输出不是 JSON 对象"))
            return
        if "event" in message:
            if message["event"] == "ready":
                if message.get("protocol_version") != 1:
                    self._disconnect(BrowserError("protocol_mismatch", "需要 JSONL 协议 v1"))
                    return
                self.pid = message["pid"]
                if not self._ready.done():
                    self._ready.set_result(None)
            self.on_event(message)
            return
        future = self.pending.pop(message.get("id"), None)
        if future is None or future.done():
            return
        if message.get("ok") is True:
            future.set_result(message["result"])
        else:
            error = message.get("error", {})
            future.set_exception(BrowserError(error.get("code", "protocol_error"), error.get("message", "请求失败"), error.get("detail")))

    def _disconnect(self, error):
        if self.failure:
            return
        self.failure = error
        for future in [*self.pending.values(), *([self._ready] if hasattr(self, "_ready") else [])]:
            if not future.done():
                future.set_exception(error)
                future.exception()  # Also consumed when startup caller was cancelled.
        self.pending.clear()
        # Stop unsent work. EOF revokes control and preserves initialized windows.
        while True:
            try:
                self._outgoing.get_nowait()
            except queue.Empty:
                break
        self._outgoing.put_nowait(None)
        self.on_event({"event": "disconnected", "state": "disconnected", "detail": error.payload()})

    async def call(self, method, **params):
        await self.start()
        self.check()
        request_id = uuid.uuid4().hex
        data = (json.dumps({"id": request_id, "method": method, "params": params}, ensure_ascii=False) + "\n").encode("utf-8")
        if len(data) - 1 > 1024 * 1024:
            raise BrowserError("invalid_request", "请求超过 1 MiB")
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            self._outgoing.put_nowait(data)
        except queue.Full:
            self.pending.pop(request_id, None)
            raise BrowserError("busy", "Focus 宿主请求队列已满，请求未接受")
        try:
            return await asyncio.wait_for(asyncio.shield(future), self.timeout)
        except TimeoutError:
            error = BrowserError("connection_lost", "宿主响应超时，结果未知，禁止重放", {"result_unknown": True})
            self._disconnect(error)
            raise error
        finally:
            if future.done():
                self.pending.pop(request_id, None)
            else:
                future.add_done_callback(lambda result: result.exception() if not result.cancelled() else None)

    async def disconnect(self):
        self._disconnect(BrowserError("connection_lost", "Focus 已断开浏览器连接"))
