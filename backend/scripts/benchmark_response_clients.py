"""本地 HTTP/1.1 小请求基准：SDK/TCP 创建数、结构化完成耗时与流式首字延迟。

不读取应用配置或真实密钥，不访问外网。耗时包含模型构造，结构化阶段还包含请求估算。
示例：python backend/scripts/benchmark_response_clients.py --requests 20 --report report.json。
"""

import argparse
import asyncio
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import socket
import statistics
import sys
import threading
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend/packages/harness"))

import openai
from pydantic import BaseModel, ConfigDict

from backend.app.desktop.patrol.agent_loop.workers.structured_worker import StructuredWorkerModel
from focus.config.app_config import AppConfig
from focus.models import response_clients
from focus.models.factory import create_chat_model
from focus.models.response_output import response_text


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.connections = 0
        self.requests = 0

    def get_request(self):
        connection, address = super().get_request()
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.connections += 1
        return connection, address


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests += 1
        message = {"type": "message", "id": "msg", "role": "assistant", "status": "completed",
                   "content": [{"type": "output_text", "text": '{"value":1}', "annotations": []}]}
        response = {"object": "response", "id": "response", "created_at": 0, "model": "fixture",
                    "status": "completed", "output": [message],
                    "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}}
        if payload.get("stream"):
            events = [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {**message, "status": "in_progress", "content": []}},
                {"type": "response.output_text.delta", "output_index": 0, "item_id": "msg",
                 "content_index": 0, "delta": '{"value":1}'},
                {"type": "response.output_item.done", "output_index": 0, "item": message},
                {"type": "response.completed", "response": response},
            ]
            body = "".join("event: " + event["type"] + "\ndata: " + json.dumps(
                {**event, "sequence_number": index}) + "\n\n" for index, event in enumerate(events)).encode()
            content_type = "text/event-stream"
        else:
            body = json.dumps(response).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


def _timings(samples):
    warm = sorted(samples[1:])
    return {"cold_ms": round(samples[0], 3), "warm_p50_ms": round(statistics.median(warm), 3),
            "warm_p95_ms": round(warm[min(len(warm) - 1, int(len(warm) * 0.95))], 3),
            "samples_ms": [round(sample, 3) for sample in samples]}


async def measure(server, *, requests, pooled, streaming):
    app = AppConfig.model_validate({"models": [{
        "name": "fixture", "display_name": "Fixture", "model": "fixture",
        "use": "focus.models.responses:FocusResponsesChatModel", "provider": "openai",
        "protocol": "responses", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
        "api_key": "local-benchmark", "curation_default": True, "curation_output_method": "json_schema",
    }]})
    sdk_clients, samples = [], []
    connections_before, requests_before = server.connections, server.requests
    sdk_factory = response_clients.AsyncOpenAI

    def create_sdk(**options):
        client = sdk_factory(**options)
        sdk_clients.append(client)
        return client

    with patch.object(response_clients, "AsyncOpenAI", create_sdk):
        async with response_clients.response_client_session(app) if pooled else nullcontext():
            try:
                for index in range(requests):
                    started = perf_counter()
                    if streaming:
                        model = create_chat_model("fixture", app_config=app)
                        first_text = None
                        async for chunk in model.astream(f"ping {index}"):
                            if first_text is None and response_text(chunk):
                                first_text = (perf_counter() - started) * 1000
                        if first_text is None:
                            raise AssertionError("stream produced no text")
                        samples.append(first_text)
                    else:
                        worker = StructuredWorkerModel(app)
                        worker.estimate_input_tokens(Result, "Return JSON", {"value": index})
                        result = await worker.invoke(Result, "Return JSON", {"value": index})
                        samples.append((perf_counter() - started) * 1000)
                        assert result.value == 1 and worker.usage.model_calls == 1
                        assert worker.last_usage_reported and worker.usage.input_tokens == 10
                    if not pooled:
                        await sdk_clients[-1].close()
            finally:
                if not pooled:
                    for client in sdk_clients:
                        await client.close()
    assert all(client.is_closed() for client in sdk_clients)
    return {"pooled": pooled, "mode": "stream_ttft" if streaming else "structured_completion",
            "requests": server.requests - requests_before, "sdk_creations": len(sdk_clients),
            "tcp_connections": server.connections - connections_before, **_timings(samples)}


async def benchmark(requests):
    if requests < 3:
        raise ValueError("requests must be at least 3")
    # 固定直连、无租户头，隔离宿主机代理及 OpenAI 环境设置。
    ignored = {key: "" for key in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        "OPENAI_CUSTOM_HEADERS", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID",
    )}
    # Windows 系统代理也可能被 urllib/httpx 读取，显式排除本地服务。
    ignored.update(NO_PROXY="*", no_proxy="*")
    server = Server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with patch.dict(os.environ, ignored):
            results = [await measure(server, requests=requests, pooled=pooled, streaming=streaming)
                       for streaming in (False, True) for pooled in (False, True)]
        return {"environment": {"python": platform.python_version(), "openai": openai.__version__,
                                "platform": platform.platform()},
                "method": "Loopback HTTP/1.1, no TLS/model inference; start before model construction; first sample cold",
                "results": results}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = asyncio.run(benchmark(args.requests))
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
