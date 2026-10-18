"""离线验证应用级 Responses 连接复用、配置失效和逐请求用量隔离。"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict

from backend.app.desktop.agent_loop.structured_worker import StructuredWorkerModel
from focus.config.app_config import AppConfig, apply_app_config
from focus.models import response_clients as clients
from focus.models.factory import create_chat_model


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


def config():
    return AppConfig.model_validate({"models": [{
        "name": "fixture", "display_name": "Fixture", "model": "fixture",
        "use": "focus.models.responses:FocusResponsesChatModel", "provider": "openai",
        "protocol": "responses", "base_url": "https://fixture.test/v1", "api_key": "fixture-key",
        "curation_default": True, "curation_output_method": "json_schema",
    }]})


def install_sdk(monkeypatch, *, failure=None, started=None):
    constructed, requests = [], []

    async def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            if started is not None:
                started.set()
            if failure == "cancel":
                await asyncio.Future()
            if failure == "provider_error":
                return httpx.Response(503, json={"error": {"message": "fixture unavailable"}})
        # 使并发请求交错完成，用独立 payload 产生独立输出与 usage。
        await asyncio.sleep(0)
        content = body["input"][0]["content"]
        if not isinstance(content, str):
            content = content[0]["text"]
        value = json.loads(content.split("<worker_input>")[1].split("</worker_input>")[0])["value"]
        return httpx.Response(200, json={
            "object": "response", "id": f"r{value}", "created_at": 0, "model": "fixture",
            "status": "completed", "output": [{"type": "message", "id": f"m{value}",
                "role": "assistant", "status": "completed", "content": [{
                    "type": "output_text", "text": json.dumps({"value": value}), "annotations": [],
                }]}], "usage": {"input_tokens": value, "output_tokens": 2, "total_tokens": value + 2},
        })

    def transport(**_kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False)

    def factory(**options):
        if options.get("http_client") is None:
            options["http_client"] = transport()
        sdk = AsyncOpenAI(**options)
        constructed.append(sdk)
        return sdk

    monkeypatch.setattr(clients, "AsyncOpenAI", factory)
    monkeypatch.setattr(clients, "ProxyFirstAsyncHttpClient", transport)
    monkeypatch.setattr(clients, "ObservedAsyncHttpClient", transport)
    return constructed, requests


def test_preview_is_lazy_and_workers_share_connections_but_not_requests_or_receipts(monkeypatch):
    constructed, requests = install_sdk(monkeypatch)

    class Receipts:
        def __init__(self):
            self.settlements = []

        async def reserve(self, size, output):
            return object()

        async def settle(self, receipt, usage, reported):
            self.settlements.append((receipt, usage, reported))

    async def run():
        app = config()
        async with clients.response_client_session(app):
            assert app.model_copy(deep=True)._response_client_pool is app._response_client_pool
            workers = [StructuredWorkerModel(app) for _ in range(3)]
            receipts = [Receipts() for _ in workers]
            for index, worker in enumerate(workers):
                worker.bind_usage_receipts(receipts[index])
                assert worker.estimate_input_tokens(Result, "Return JSON", {"value": index + 1}) > 0
            assert constructed == requests == []
            results = await asyncio.gather(*[
                worker.invoke(Result, "Return JSON", {"value": index + 1})
                for index, worker in enumerate(workers)
            ])
            assert [result.value for result in results] == [1, 2, 3]
            assert len(constructed) == 1 and len(requests) == 3
            for index, worker in enumerate(workers):
                assert worker.last_usage_reported
                assert worker.last_usage.input_tokens == index + 1
                assert worker.usage.model_calls == 1
                assert receipts[index].settlements[0][1:] == (worker.last_usage, True)
            assert len({id(item.settlements[0][0]) for item in receipts}) == 3
            # 关闭借用端口不关闭会话共享连接，也不能让此端口重建。
            model = create_chat_model("fixture", app_config=app)
            assert model._async_client._get() is constructed[0]
            await model._async_client.close()
            assert not constructed[0].is_closed()
            with pytest.raises(RuntimeError, match="客户端已关闭"):
                model._async_client._get()
        assert all(sdk.is_closed() for sdk in constructed)
        assert app._response_client_pool is None

    asyncio.run(run())


@pytest.mark.parametrize("change", ["api_key", "base_url", "provider", "proxy", "no_proxy", "project", "timeout", "retry"])
def test_configuration_changes_invalidate_cached_clients(monkeypatch, change):
    constructed, _ = install_sdk(monkeypatch)

    async def run():
        app = config()
        async with clients.response_client_session(app) as pool:
            original = create_chat_model("fixture", app_config=app)._async_client._get()
            fresh = config()
            kwargs = {}
            if change in {"api_key", "base_url", "provider"}:
                setattr(fresh.models[0], change, {"api_key": "rotated-key", "base_url": "https://other.test/v1",
                                               "provider": "deepseek"}[change])
                apply_app_config(app, fresh)
            elif change == "proxy":
                monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8999")
            elif change == "no_proxy":
                monkeypatch.setenv("NO_PROXY", "fixture.test")
            elif change == "project":
                monkeypatch.setenv("OPENAI_PROJECT_ID", "another-project")
            elif change == "timeout":
                kwargs["timeout"] = 42
            else:
                kwargs["max_retries"] = 1
            model = create_chat_model("fixture", app_config=app, **kwargs)
            current = model._async_client._get()
            assert current is not original
            assert model._async_client._get() is current
            assert len(pool._clients) == 1
            assert len(pool._retired) == 1
            assert not original.is_closed()  # 在途调用可以继续完成。
            assert "rotated-key" not in repr(pool._clients)
        assert len(constructed) == 2 and all(sdk.is_closed() for sdk in constructed)

    asyncio.run(run())


def test_model_and_output_options_do_not_partition_http_connections(monkeypatch):
    constructed, _ = install_sdk(monkeypatch)

    async def run():
        app = config()
        second = app.models[0].model_copy(update={"name": "second", "model": "other", "curation_default": False})
        app.models.append(second)
        async with clients.response_client_session(app):
            first = create_chat_model("fixture", app_config=app, max_tokens=512)
            other = create_chat_model("second", app_config=app, max_tokens=1024, temperature=0.5)
            assert first._async_client._get() is other._async_client._get()
            assert first.model_name != other.model_name and first.max_tokens != other.max_tokens
            app.models[0].api_key = "rotated-key"
            rotated = create_chat_model("fixture", app_config=app)
            assert rotated._async_client._get() is not other._async_client._get()
            assert other._async_client._get() is constructed[0]
        assert len(constructed) == 2 and all(sdk.is_closed() for sdk in constructed)

    asyncio.run(run())


def test_async_clients_cannot_cross_event_loops_or_outlive_session(monkeypatch):
    constructed, _ = install_sdk(monkeypatch)
    app = config()
    with asyncio.Runner() as owner, asyncio.Runner() as other:
        async def enter():
            session = clients.response_client_session(app)
            await session.__aenter__()
            model = create_chat_model("fixture", app_config=app)
            model._async_client._get()
            return session, model

        session, model = owner.run(enter())

        async def access():
            return model._async_client._get()

        with pytest.raises(RuntimeError, match="跨事件循环"):
            other.run(access())
        owner.run(session.__aexit__(None, None, None))
        with pytest.raises(RuntimeError, match="会话已关闭"):
            owner.run(access())
    assert constructed[0].is_closed()


def test_custom_transport_is_not_pooled_or_closed_by_session():
    async def run():
        app = config()
        custom = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)), trust_env=False)
        try:
            async with clients.response_client_session(app) as pool:
                model = create_chat_model("fixture", app_config=app, http_async_client=custom)
                assert model._async_client._get()._client is custom
                assert not pool._clients
            assert not custom.is_closed
            await model._async_client.close()
            assert custom.is_closed
        finally:
            await custom.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["provider_error", "cancel"])
def test_failed_or_cancelled_request_does_not_poison_shared_client(monkeypatch, failure):
    async def run():
        started = asyncio.Event()
        constructed, _ = install_sdk(monkeypatch, failure=failure, started=started)
        app = config()
        async with clients.response_client_session(app):
            failed = StructuredWorkerModel(app)
            task = asyncio.create_task(failed.invoke(Result, "Return JSON", {"value": 1}))
            await asyncio.wait_for(started.wait(), 2)
            if failure == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                from openai import InternalServerError
                with pytest.raises(InternalServerError):
                    await task
            assert failed.last_usage.model_calls == 1 and not failed.last_usage_reported
            next_worker = StructuredWorkerModel(app)
            assert (await next_worker.invoke(Result, "Return JSON", {"value": 2})).value == 2
            assert next_worker.usage.model_calls == 1 and next_worker.usage.input_tokens == 2
            assert len(constructed) == 1 and not constructed[0].is_closed()
        assert constructed[0].is_closed()

    asyncio.run(run())


def test_sync_borrowers_share_one_client_across_threads_and_close_independently(monkeypatch):
    made = []

    class SDK:
        def __init__(self, **options):
            self.closed = False
            made.append(self)

        def close(self):
            assert not self.closed
            self.closed = True

    monkeypatch.setattr(clients, "OpenAI", SDK)
    monkeypatch.setattr(clients, "ProxyFirstHttpClient", lambda **_kwargs: None)
    monkeypatch.setattr(clients, "ObservedHttpClient", lambda **_kwargs: None)

    async def run():
        app = config()
        async with clients.response_client_session(app):
            models = [create_chat_model("fixture", app_config=app) for _ in range(8)]
            with ThreadPoolExecutor(max_workers=8) as threads:
                owners = list(threads.map(lambda model: model._client._get(), models))
            assert all(owner is made[0] for owner in owners) and len(made) == 1
            for model in models:
                model._client.close()
            assert not made[0].closed
        assert made[0].closed

    asyncio.run(run())


def test_actual_http_connections_are_reused_without_timing_thresholds():
    from backend.scripts.benchmark_response_clients import benchmark

    results = asyncio.run(benchmark(3))["results"]
    for result in results:
        expected = 1 if result["pooled"] else 3
        assert result["requests"] == 3
        assert result["sdk_creations"] == result["tcp_connections"] == expected


def test_empty_session_and_close_of_unused_ports_never_construct_sdk(monkeypatch):
    def unexpected(**kwargs):
        raise AssertionError("unused SDK initialized")

    monkeypatch.setattr(clients, "OpenAI", unexpected)
    monkeypatch.setattr(clients, "AsyncOpenAI", unexpected)

    async def run():
        app = config()
        async with clients.response_client_session(app):
            model = create_chat_model("fixture", app_config=app)
            model._client.close()
            await model._async_client.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, "provider_error", "cancel"])
def test_unpooled_worker_closes_actual_sdk_after_request(monkeypatch, failure):
    async def run():
        started = asyncio.Event()
        constructed, requests = install_sdk(monkeypatch, failure=failure, started=started)
        worker = StructuredWorkerModel(config())
        task = asyncio.create_task(worker.invoke(Result, "Return JSON", {"value": 1}))
        await asyncio.wait_for(started.wait(), 2)
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif failure == "provider_error":
            from openai import InternalServerError
            with pytest.raises(InternalServerError):
                await task
        else:
            assert (await task).value == 1
        assert len(requests) == len(constructed) == 1
        assert constructed[0].is_closed()

    asyncio.run(run())


def test_shared_observed_transport_records_each_borrowers_model_and_invocation(monkeypatch, caplog):
    from focus.models.http_clients import ObservedAsyncHttpClient, _SDK_HTTPX
    from focus.models.telemetry import model_invocation

    caplog.set_level("INFO", logger="focus.performance")
    transports = []

    async def handle(request):
        body = json.loads(request.content)
        await asyncio.sleep(0)
        return _SDK_HTTPX.Response(200, json={
            "object": "response", "id": "r", "created_at": 0, "model": body["model"],
            "status": "completed", "output": [{"type": "message", "id": "m",
                "role": "assistant", "status": "completed", "content": [{
                    "type": "output_text", "text": "ok", "annotations": [],
                }]}], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        })

    def transport(**options):
        client = ObservedAsyncHttpClient(
            **options, transport=_SDK_HTTPX.MockTransport(handle), trust_env=False,
        )
        transports.append(client)
        return client

    monkeypatch.setattr(clients, "ObservedAsyncHttpClient", transport)
    monkeypatch.setattr(clients, "ProxyFirstAsyncHttpClient", transport)

    async def run():
        app = config()
        app.models.append(app.models[0].model_copy(update={
            "name": "second", "model": "other", "curation_default": False,
        }))
        async with clients.response_client_session(app):
            first = create_chat_model("fixture", app_config=app)
            second = create_chat_model("second", app_config=app)
            # Direct calls also need the borrower's identity, rather than the first pool creator's.
            await asyncio.gather(first.ainvoke("first"), second.ainvoke("second"))
            with model_invocation(category="index_prewarm", run_id="probe") as invocation:
                await second.ainvoke("scoped")
            assert first._async_client._get() is second._async_client._get()
            assert len(transports) == 1 and not transports[0].is_closed
        assert transports[0].is_closed
        return invocation["invocation_id"]

    invocation_id = asyncio.run(run())
    records = [json.loads(record.getMessage().split("focus_metric ", 1)[1])
               for record in caplog.records if record.getMessage().startswith("focus_metric ")]
    requests = [record for record in records if record["event"] == "provider_request"]
    assert sorted(record["model"] for record in requests) == ["fixture", "other", "other"]
    direct = [record for record in requests if record["category"] == "unclassified"]
    assert len({record["invocation_id"] for record in direct}) == 2
    scoped = [record for record in requests if record["category"] == "index_prewarm"]
    assert len(scoped) == 1 and scoped[0]["model"] == "other"
    assert scoped[0]["invocation_id"] == invocation_id and scoped[0]["run_id"] == "probe"
