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
import sys
import time

from .errors import BrowserError
from .host_artifact import recovery_enabled, stage


def executable_path():
    override = os.environ.get("FOCUS_DISTINCT_BROWSER_EXE")
    path = Path(override) if override else Path(__file__).resolve().parents[4] / "desktop/resources/distinct-browser/distinct-browser.exe"
    if os.name != "nt":
        raise BrowserError("unsupported_platform", "独立浏览器需要 Windows 11 和 WebView2 Runtime")
    if not path.is_file():
        raise BrowserError("executable_missing", "未找到 distinct-browser.exe；请运行 npm --prefix desktop run build:browser，或设置 FOCUS_DISTINCT_BROWSER_EXE")
    return path.resolve()


class BrowserTransport:
    def __init__(self, executable=None, on_event=lambda event: None, timeout=55, *, recovery=None,
                 endpoint=None, expected_host_id=None, artifact_sha256=None, cache_root=None):
        self.executable = executable
        self.on_event, self.timeout = on_event, timeout
        self.process = None
        self.failure = None
        self.pending = {}
        self.host_id = expected_host_id or uuid.uuid4().hex
        self.recovery = recovery_enabled() if recovery is None else recovery
        self.endpoint, self.artifact_sha256, self.cache_root = endpoint, artifact_sha256, cache_root
        self.expected_host_id = expected_host_id
        self.connection_id = None
        self.recovery_snapshot = None
        self.restoring = endpoint is not None
        self._native = None
        self._native_close = None
        self._revision = -1
        self.pid = None
        self._start_lock = asyncio.Lock()
        self._outgoing = queue.Queue(maxsize=128)
        self._stderr = bytearray()

    async def start(self):
        async with self._start_lock:
            self.check()
            if self.process is not None or self._native is not None:
                return
            self._loop = asyncio.get_running_loop()
            self._ready = self._loop.create_future()
            try:
                if self.recovery:
                    await self._start_named()
                    return
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
                if isinstance(error, asyncio.CancelledError):
                    self._disconnect(BrowserError('connection_lost', '浏览器连接初始化已取消'))
                    raise
                failure = error if isinstance(error, BrowserError) else BrowserError(
                    'connection_busy' if getattr(error, 'winerror', None) == 231 else 'host_unrecoverable' if self.restoring else 'connection_lost',
                    '原浏览器宿主正被另一个客户端使用' if getattr(error, 'winerror', None) == 231 else '浏览器连接失败；没有重建原页面')
                self._disconnect(failure)
                raise failure from error

    async def _start_named(self):
        if os.name != 'nt':
            raise BrowserError('unsupported_platform', '会话恢复仅支持 Windows')
        if self.restoring and (not self.expected_host_id or not self.artifact_sha256):
            raise BrowserError('host_unrecoverable', '原绑定缺少受信任的宿主身份')
        binary, self.artifact_sha256 = await asyncio.to_thread(stage,
            None if self.restoring else self.executable or executable_path(), self.cache_root, self.artifact_sha256 if self.restoring else None)
        if not self.restoring:
            def launch():
                result = subprocess.run([sys.executable, str(Path(__file__).with_name('launch_host.py')), str(binary)],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode:
                    raise BrowserError('host_launch_failed', '无法启动可独立存活的浏览器宿主；请检查 v0.6 产物和父进程 Job 限制')
                return json.loads(result.stdout)
            bootstrap = await asyncio.to_thread(launch)
            self.endpoint, self.expected_host_id = bootstrap['endpoint'], bootstrap['host_id']
        from .win32_pipe import ReconnectClient
        def connect():
            deadline = time.monotonic() + 1
            while True:
                try:
                    return ReconnectClient(self.endpoint, binary, expected_sha256=self.artifact_sha256,
                                           expected_host_id=self.expected_host_id)
                except OSError as error:
                    # Only retry opening a busy pipe while the old STA writer
                    # detaches. Never repeat a handshake or a browser command.
                    if error.winerror != 231 or time.monotonic() >= deadline: raise
                    time.sleep(.025)
        connecting = asyncio.create_task(asyncio.to_thread(connect))
        try:
            self._native = await asyncio.shield(connecting)
        except asyncio.CancelledError:
            # A cancelled await cannot stop the Windows handshake thread.
            # Close its eventual client rather than leaving an unseen owner.
            def abandon(completed):
                if not completed.cancelled() and completed.exception() is None:
                    asyncio.create_task(asyncio.to_thread(completed.result().disconnect))
            connecting.add_done_callback(abandon)
            raise
        self.check()
        self.host_id, self.pid, self.connection_id = self._native.host_id, self._native.pid, self._native.connection_id
        # The codec already buffers events. Install the authoritative snapshot
        # on the app loop BEFORE starting the event relay, then drop <= baseline.
        snapshot_call = await asyncio.to_thread(self._native.submit, 'snapshot')
        self.recovery_snapshot = await asyncio.wait_for(asyncio.wrap_future(snapshot_call), self.timeout)
        self.check()
        if (self.recovery_snapshot.get('host_id') != self.host_id or self.recovery_snapshot.get('connection_id') != self.connection_id
                or not isinstance(self.recovery_snapshot.get('revision'), int) or not isinstance(self.recovery_snapshot.get('browsers'), list)):
            raise BrowserError('protocol_mismatch', '宿主快照的身份、代次或修订号无效')
        self._revision = self.recovery_snapshot['revision']
        self.on_event({'event': 'host_snapshot', 'host_id': self.host_id, 'connection_id': self.connection_id,
                       'detail': self.recovery_snapshot, 'restored': self.restoring})
        self._ready.set_result(None)
        self._native.subscribe(lambda event: self._post(self._named_event, event),
            lambda: self._post(self._disconnect, BrowserError('connection_lost', '浏览器连接结束；已发动作结果可能未知，禁止重放')))

    def _named_event(self, event):
        if self.failure: return
        if event.get('host_id') != self.host_id or event.get('connection_id') != self.connection_id:
            self._disconnect(BrowserError('protocol_mismatch', '浏览器事件的宿主或连接代次不匹配')); return
        if not isinstance(event.get('revision'), int) or event['revision'] < 0:
            self._disconnect(BrowserError('protocol_mismatch', '浏览器事件修订号无效')); return
        if event['revision'] <= self._revision: return
        self._revision = event['revision']
        self.on_event(event)

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
        if self._native and self._native_close is None:
            self._native_close = asyncio.create_task(asyncio.to_thread(self._native.disconnect))
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
        self.on_event({"event": "disconnected", "host_id": self.host_id, "state": "disconnected", "detail": error.payload()})

    async def call(self, method, **params):
        await self.start()
        self.check()
        if self.recovery:
            future = None
            try:
                submitted = await asyncio.to_thread(self._native.submit, method, **params)
                future = asyncio.wrap_future(submitted)
                return await asyncio.wait_for(asyncio.shield(future), self.timeout)
            except BrowserError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failure = BrowserError('connection_lost', '浏览器响应丢失，结果未知，禁止重放', {'result_unknown': True})
                self._disconnect(failure)
                raise failure from error
            finally:
                if future and not future.done():
                    future.add_done_callback(lambda result: result.exception() if not result.cancelled() else None)
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
        if self._native_close:
            await self._native_close
