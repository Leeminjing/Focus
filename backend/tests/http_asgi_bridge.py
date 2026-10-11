"""本文件对外提供 ASGIHTTPBridge，将隔离 TestClient 的真实 ASGI 服务暴露为浏览器 HTTP。

输入为已经启动 lifespan 的 TestClient、可选默认请求头和请求记录列表；输出为可用于 Electron 的 url、游标请求记录和 active_paths 活动连接路径。
具体工作流为在原 portal 事件循环执行 ASGI，将 response.start/body 逐帧转发到真实套接字，客户端离开时取消订阅。
不缓存无限 SSE，不替换路由、认证、数据库或 StreamBridge；disconnect(path) 只断开测试套接字以验收 EventSource 重连，上下文退出会关闭测试服务器。
示例：with ASGIHTTPBridge(http, headers=SESSION) as bridge: browser.loadURL(bridge.url + '/desktop/')。
"""

import asyncio
from concurrent.futures import TimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, Queue
import select
import socket
import threading
from urllib.parse import unquote, urlsplit


class ASGIHTTPBridge:
    def __init__(self, client, *, headers=None, traffic=None):
        self.client = client
        self.headers = headers or {}
        self.traffic = traffic if traffic is not None else []
        self.requests = []
        self._connections = {}
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.end_headers()

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "null"))
                self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,PATCH,OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "content-type,x-focus-session,idempotency-key,last-event-id")

            def _forward(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                url = urlsplit(self.path)
                request_headers = {**owner.headers, **dict(self.headers)}
                scope = {
                    "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                    "method": self.command, "scheme": "http", "path": unquote(url.path),
                    "raw_path": url.path.encode(), "query_string": url.query.encode(), "root_path": "",
                    "headers": [(key.lower().encode(), value.encode()) for key, value in request_headers.items()],
                    "client": self.client_address, "server": self.server.server_address,
                    "state": dict(owner.client.app_state),
                }
                frames = Queue()
                disconnected = threading.Event()
                owner.traffic.append((self.command, url.path, "pending"))
                owner.requests.append({"method": self.command, "path": url.path, "last_event_id": self.headers.get("Last-Event-ID")})
                with owner._lock:
                    owner._connections[self.connection] = url.path

                async def serve():
                    received = False

                    async def receive():
                        nonlocal received
                        if not received:
                            received = True
                            return {"type": "http.request", "body": body, "more_body": False}
                        while not disconnected.is_set():
                            await asyncio.sleep(0.05)
                        return {"type": "http.disconnect"}

                    async def send(message):
                        frames.put(message)

                    try:
                        await owner.client.app(scope, receive, send)
                    finally:
                        frames.put(None)

                future = owner.client.portal.start_task_soon(serve)
                try:
                    while True:
                        try:
                            frame = frames.get(timeout=0.25)
                        except Empty:
                            if select.select([self.connection], [], [], 0)[0] and not self.connection.recv(1, socket.MSG_PEEK):
                                break
                            continue
                        if frame is None:
                            future.result()
                            break
                        if frame["type"] == "http.response.start":
                            owner.traffic.append((self.command, url.path, frame["status"]))
                            self.send_response(frame["status"])
                            for key, value in frame.get("headers", []):
                                if key.lower() not in {b"connection", b"transfer-encoding"}:
                                    self.send_header(key.decode("latin-1"), value.decode("latin-1"))
                            self._cors()
                            self.end_headers()
                        elif frame["type"] == "http.response.body":
                            self.wfile.write(frame.get("body", b""))
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                finally:
                    disconnected.set()
                    try:
                        future.result(timeout=1)
                    except TimeoutError:
                        future.cancel()
                    with owner._lock:
                        owner._connections.pop(self.connection, None)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _forward

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def disconnect(self, path):
        with self._lock:
            connections = [connection for connection, current in self._connections.items() if current == path]
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return len(connections)

    def active_paths(self):
        with self._lock:
            return list(self._connections.values())

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
