"""Run 拥有独占模型连接，借用的应用连接应存活到应用退出。"""

import asyncio
from types import SimpleNamespace

import pytest

from focus.config.app_config import AppConfig
from focus.models.factory import create_chat_model
from focus.models.lifecycle import model_client_scope, retain_model_clients
from focus.models.response_clients import response_client_session
from focus.runtime.runs.manager import RunManager
from focus.runtime.runs.schemas import RunStatus
from focus.runtime.runs.worker import run_agent
from focus.runtime.stream_bridge.memory import MemoryStreamBridge
from focus.security.context import AuthorizationIdentity, RoutingIdentity, SecurityContext
from focus.security.policy import AccessMode


def config(protocol):
    return AppConfig.model_validate({"models": [{
        "name": "fixture", "display_name": "Fixture", "model": "fixture",
        "use": "focus.models.responses:FocusResponsesChatModel" if protocol == "responses"
        else "langchain_openai:ChatOpenAI",
        "provider": "openai", "protocol": protocol, "api_key": "fixture-key",
        "base_url": "https://fixture.test/v1", "default": True,
    }]})


def sdk_clients(model, protocol):
    if protocol == "responses":
        return model._client._get(), model._async_client._get()
    return model.root_client, model.root_async_client


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
@pytest.mark.parametrize("outcome", ["success", "error", "cancel", "assembly_error"])
def test_run_closes_owned_model_clients(tmp_path, monkeypatch, protocol, outcome):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)

    async def exercise():
        app = config(protocol)
        manager = RunManager()
        record = manager.create("fixture")
        security = SecurityContext(
            AuthorizationIdentity(tmp_path, (tmp_path,), (), AccessMode.READ_ONLY, "main"),
            RoutingIdentity("fixture", "workspace", "main", "fixture", "", record.run_id),
        )
        made = []
        models = []

        def make_model():
            model = create_chat_model(app_config=app)
            models.append(model)
            made.extend(sdk_clients(model, protocol))
            assert all(not sdk.is_closed() for sdk in made)

        class Graph:
            async def astream(self, *args, **kwargs):
                # LangGraph 节点由子任务运行，也必须归属同一个 Run。
                async def node():
                    make_model()
                await asyncio.create_task(node())
                if outcome == "error":
                    raise ValueError("fixture failure")
                if outcome == "cancel":
                    raise asyncio.CancelledError()
                yield "values", {}

        async def factory():
            make_model()
            if outcome == "assembly_error":
                raise ValueError("fixture assembly failure")
            return Graph()

        async def default_factory(**kwargs):
            assert kwargs["app_config"] is app
            return await factory()

        monkeypatch.setattr("focus.runtime.runs.worker.make_lead_agent", default_factory)
        try:
            await run_agent(record=record, bridge=MemoryStreamBridge(), run_manager=manager,
                app_config=app, graph_input={}, runnable_config={},
                langgraph_context=security.to_runtime_context(),
                agent_factory=None if outcome == "success" and protocol == "responses" else factory)
        except asyncio.CancelledError:
            assert outcome == "cancel"
        assert made
        try:
            assert all(sdk.is_closed() for sdk in made)
        finally:
            for sdk in made[::2]:
                sdk.close()
            for sdk in made[1::2]:
                await sdk.close()
        if outcome == "success":
            assert record.status is RunStatus.success

    asyncio.run(exercise())


def test_scopes_isolate_concurrent_owners_and_keep_application_connections_alive():
    async def exercise():
        app = config("responses")
        first_done, second_ready = asyncio.Event(), asyncio.Event()
        models, sdks = [], []
        async with response_client_session(app):
            async def first():
                async with model_client_scope():
                    model = create_chat_model(app_config=app)
                    models.append(model)
                    sdks.append(sdk_clients(model, "responses"))
                    await second_ready.wait()
                first_done.set()

            async def second():
                async with model_client_scope():
                    model = create_chat_model(app_config=app)
                    models.append(model)
                    sdks.append(sdk_clients(model, "responses"))
                    second_ready.set()
                    await first_done.wait()
                    assert sdks[0] == sdks[1]
                    assert sdk_clients(model, "responses") == sdks[0]
                    assert all(not sdk.is_closed() for sdk in sdks[0])

            await asyncio.gather(first(), second())
            # 端口属于各自 scope，池内连接仍属于应用。
            assert all(not sdk.is_closed() for sdk in sdks[0])
        assert all(sdk.is_closed() for sdk in sdks[0])

    asyncio.run(exercise())


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
def test_injected_transports_still_belong_to_caller(protocol):
    import httpx

    async def exercise():
        sync = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        asynchronous = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        try:
            async with model_client_scope():
                model = create_chat_model(app_config=config(protocol),
                    http_client=sync, http_async_client=asynchronous)
                sdks = sdk_clients(model, protocol)
            assert not sync.is_closed and not asynchronous.is_closed
            sdks[0].close()
            await sdks[1].close()
        finally:
            sync.close()
            await asynchronous.aclose()

    asyncio.run(exercise())


def test_failed_close_does_not_skip_other_clients_or_mask_request_failure(caplog):
    closed = []

    class Client:
        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail

        async def close(self):
            closed.append(self.name)
            if self.fail:
                raise ValueError("close failure")

    async def exercise():
        with pytest.raises(ValueError, match="request failure"):
            async with model_client_scope():
                retain_model_clients(async_client=Client("healthy"))
                retain_model_clients(async_client=Client("broken", fail=True))
                raise ValueError("request failure")
        assert closed == ["broken", "healthy"]

    asyncio.run(exercise())
    assert "模型连接回收失败" in caplog.text


def test_runtime_stops_runs_before_closing_shared_connections(tmp_path):
    from backend.app.gateway.deps import langgraph_runtime

    async def exercise():
        app_config = config("responses")
        app_config.checkpointer.type = "memory"
        app_config.langgraph_store.backend = "memory"
        app_config.langgraph_store.vector_enabled = False
        app = SimpleNamespace(state=SimpleNamespace())
        started = asyncio.Event()
        models, sdks, finishing = [], [], []
        async with langgraph_runtime(app, app_config):
            manager = app.state.run_manager
            record = manager.create("fixture")
            security = SecurityContext(
                AuthorizationIdentity(tmp_path, (tmp_path,), (), AccessMode.READ_ONLY, "main"),
                RoutingIdentity("fixture", "workspace", "main", "fixture", "", record.run_id),
            )

            class Graph:
                async def astream(self, *args, **kwargs):
                    model = create_chat_model(app_config=app_config)
                    models.append(model)
                    sdks.extend(sdk_clients(model, "responses"))
                    started.set()
                    try:
                        await asyncio.Future()
                    finally:
                        finishing.append(all(not sdk.is_closed() for sdk in sdks))
                    yield "values", {}

            async def factory():
                return Graph()

            record.task = asyncio.create_task(run_agent(record=record, bridge=app.state.stream_bridge,
                run_manager=manager, app_config=app_config, graph_input={}, runnable_config={},
                langgraph_context=security.to_runtime_context(), agent_factory=factory))
            await asyncio.wait_for(started.wait(), 10)
        assert record.task.done()
        assert record.status is RunStatus.interrupted
        assert finishing == [True]
        assert all(sdk.is_closed() for sdk in sdks)

    asyncio.run(exercise())


def test_cancellation_during_cleanup_waits_for_all_connections():
    async def exercise():
        closing, release = asyncio.Event(), asyncio.Event()
        closed = []

        class Client:
            async def close(self):
                closing.set()
                await release.wait()
                closed.append(True)

        async def request():
            async with model_client_scope():
                retain_model_clients(async_client=Client())

        task = asyncio.create_task(request())
        await closing.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == [True]

    asyncio.run(exercise())
