"""Opt-in Windows/WebView2 acceptance through Focus's own binding and tools.

FOCUS_BROWSER_REAL=1 python -m pytest backend/tests/test_context_browser_real.py -q -s
Uses an isolated PostgreSQL database, local HTML and a scripted tool-calling model.
"""
import asyncio
import base64
import ctypes
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.browser.service import ContextBrowsers
from backend.app.desktop.browser.tools import build_browser_tools
from backend.app.desktop.browser.transport import BrowserError
from backend.app.desktop.persistence.models import DesktopThread, DesktopWorkspace
from backend.tests.browser_fixture import BrowserFixture
from backend.tests.runtime_context_support import runtime_context
from focus.runtime.runs.manager import RunManager
from focus.runtime.runs.schemas import RunStatus
from focus.runtime.runs.worker import run_agent
from focus.runtime.stream_bridge.memory import MemoryStreamBridge

pytestmark = [pytest.mark.skipif(os.name != "nt" or os.environ.get("FOCUS_BROWSER_REAL") != "1", reason="opt-in real Windows browser"),
              pytest.mark.usefixtures("isolated_postgres_database")]


async def setup():
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace, a, b, c = [uuid.uuid4().hex for _ in range(4)]
    async with sessions.begin() as session:
        session.add(DesktopWorkspace(workspace_id=workspace, path=f"C:/browser-test/{workspace}", display_name="Browser acceptance"))
        await session.flush()
        for item in (a, b, c):
            session.add(DesktopThread(task_id=item, workspace_id=workspace, thread_id=uuid.uuid4().hex, title=item))
    return engine, ContextBrowsers(sessions, MemoryStreamBridge()), (a, b, c)


async def wait_for(predicate, timeout=8):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "browser event deadline"
        await asyncio.sleep(.02)


async def error_code(code, awaitable):
    with pytest.raises(BrowserError) as caught:
        await awaitable
    assert caught.value.code == code, caught.value


def test_real_independence_sharing_handoff_lifecycle(tmp_path):
    async def scenario():
        engine, browsers, (a, b, c) = await setup()
        fixture = BrowserFixture()
        resources = []
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.IsWindow.argtypes = [ctypes.c_void_p]
        try:
            ra, rb = browsers.resource(a, "run-A"), browsers.resource(b, "run-B")
            resources.extend((ra, rb))
            await asyncio.gather(ra.start(), rb.start())
            assert ra.browser.browser_id != rb.browser.browser_id
            await error_code("observation_required", ra.act("old", {"type": "click", "x": 1, "y": 1}))

            async def populate(run, name):
                view = await run.observe()
                await run.act(view["observation_id"], {"type": "navigate", "url": fixture.url(name)})
                view = await run.observe()
                # Supply image pixels, verifying Focus converts them into CSS.
                sx = view["image"]["width"] / view["viewport"]["width"]
                sy = view["image"]["height"] / view["viewport"]["height"]
                await run.act(view["observation_id"], {"type": "click", "x": 80 * sx, "y": 40 * sy})
                await run.act(view["observation_id"], {"type": "text", "text": "你好 " + name})
                await run.act(view["observation_id"], {"type": "click", "x": 80 * sx, "y": 105 * sy})
                return await asyncio.to_thread(fixture.wait, lambda row: row["name"] == name and row["count"] == 1)
            va, vb = await asyncio.gather(populate(ra, "A"), populate(rb, "B"))
            assert va["text"] == "你好 A" and vb["text"] == "你好 B"
            assert va["cookie"] != vb["cookie"] and va["storage"] != vb["storage"]
            view = await ra.observe()
            for run in (ra, rb):
                snapshot = await run.observe()
                output = Path(os.environ.get("FOCUS_BROWSER_ARTIFACTS", tmp_path))
                output.mkdir(parents=True, exist_ok=True)
                (output / f"{run.run_id}.png").write_bytes(base64.b64decode(snapshot["image"]["base64"]))
            assert (await browsers.present(a))["browser_id"] == ra.browser.browser_id
            assert ra.token and rb.token
            print("PASS independent Context browsers, parallel native input, isolated cookies/storage, present retains pages")

            # A pending navigation must not block B's pipe response.
            view = await ra.observe()
            slow = asyncio.create_task(ra.act(view["observation_id"], {"type": "navigate", "url": fixture.url("slow-A")}))
            before = asyncio.get_running_loop().time()
            await browsers.present(b)
            assert asyncio.get_running_loop().time() - before < .8
            await slow
            print("PASS navigation on A does not block B")

            await ra.release()
            shared = await browsers.replace(c, a)
            assert shared["browser_id"] == ra.browser.browser_id
            rc = browsers.resource(c, "run-C")
            resources.append(rc)
            await rc.start()
            first = await ra.observe()
            waiting = asyncio.create_task(rc.observe())
            await asyncio.sleep(.1)
            assert not waiting.done()
            await ra.release()
            second = await asyncio.wait_for(waiting, 5)
            assert first["observation_id"] != second["observation_id"]
            assert second["url"] == fixture.url("slow-A")
            await error_code("observation_required", rc.act(first["observation_id"], {"type": "key", "key": "Tab"}))
            await rc.release()
            await rc.finish()
            assert (await browsers.present(a))["browser_id"] == ra.browser.browser_id
            # Detaching C cannot close the browser still used by A.
            await browsers.replace(c)
            assert user32.IsWindow(int(ra.browser.hwnd))
            print("PASS shared real page, Focus lease scheduling, mandatory new observation, shared browser retained")

            observed = await ra.observe()
            hwnd = int(ra.browser.hwnd)
            user32.SendMessageW(hwnd, 0x0111, 101, 0)
            await wait_for(lambda: ra.browser.state == "manual")
            await error_code("observation_required", ra.act(observed["observation_id"], {"type": "key", "key": "Enter"}))
            await error_code("control_unavailable", ra.observe())
            user32.SendMessageW(hwnd, 0x0111, 101, 0)
            await wait_for(lambda: ra.browser.state == "available")
            await error_code("observation_required", ra.act(observed["observation_id"], {"type": "key", "key": "Enter"}))
            fresh = await ra.observe()
            assert fresh["observation_id"] != observed["observation_id"]
            for show in (6, 0):  # Minimize, then hide, using the native handlers.
                user32.ShowWindow(hwnd, show)
                await wait_for(lambda: ra.browser.state == "paused")
                assert (await browsers.present(a))["state"] == "paused"
                await error_code("control_unavailable", ra.observe())
                user32.SendMessageW(hwnd, 0x0111, 101, 0)
                await wait_for(lambda: ra.browser.state == "available")
                await ra.observe()
            print("PASS native takeover/handback, minimized/hidden pause, present never auto-resumes old actions")

            retained_id = ra.browser.browser_id
            await ra.finish()
            assert (await browsers.present(a))["browser_id"] == retained_id
            again = browsers.resource(a, "run-A-next")
            resources.append(again)
            await again.start()
            assert again.browser.browser_id == retained_id
            await again.observe()
            await again.finish()
            await browsers.transport.call("close", browser_id=retained_id)
            await wait_for(lambda: browsers.browsers[retained_id].state == "closed")
            await error_code("closed", browsers.ensure(a))
            await rb.finish()
            remaining = int(rb.browser.hwnd)
            await browsers.transport.disconnect()
            assert user32.IsWindow(remaining)
            await error_code("disconnected", browsers.ensure(b))
            restarted = ContextBrowsers(browsers.sessions, MemoryStreamBridge())
            await error_code("disconnected", restarted.ensure(b))
            print("PASS Run retention/reuse, closed IDs invalid, disconnect retains native window, restart does not silently recreate")
            # Close only this test's disconnected native windows so its host exits.
            for browser in browsers.browsers.values():
                user32.SendMessageW(int(browser.hwnd or 0), 0x0010, 0, 0)
            await asyncio.to_thread(browsers.transport.process.wait, 10)
        finally:
            for run in resources:
                await run.finish()
            if not browsers.transport.failure:
                for browser in tuple(browsers.browsers.values()):
                    if browser.state != "closed":
                        await browsers.transport.call("close", browser_id=browser.browser_id)
                await browsers.close()
            fixture.close()
            await engine.dispose()
    asyncio.run(scenario())


def test_real_renewal_cancellation_and_connection_failure(monkeypatch):
    monkeypatch.setattr("backend.app.desktop.browser.service.LEASE_TTL_MS", 1000)
    monkeypatch.setattr("backend.app.desktop.browser.service.RENEW_INTERVAL_SECONDS", .2)
    async def scenario():
        engine, browsers, (a, _, _) = await setup()
        manager = RunManager()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsWindow.argtypes = [ctypes.c_void_p]
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        old_transport = None
        async def pending_run(run_id):
            record = manager.create(run_id, run_id=run_id)
            resource = browsers.resource(a, run_id)
            context = runtime_context(agent_id="main:c", task_id=a, run_id=run_id)
            context["security_context"] = replace(context["security_context"], extras={"run_resource": resource})
            entered = asyncio.Event()
            class Graph:
                async def astream(self, *args, **kwargs):
                    await resource.observe()
                    entered.set()
                    await asyncio.Event().wait()
                    yield "values", {}
            async def factory():
                return Graph()
            record.task = asyncio.create_task(run_agent(record=record, bridge=browsers.bridge, run_manager=manager,
                app_config=SimpleNamespace(), graph_input={}, runnable_config={}, langgraph_context=context, agent_factory=factory))
            await entered.wait()
            return record, resource
        try:
            record, resource = await pending_run("cancel-run")
            token = resource.token
            await asyncio.sleep(1.5)
            await resource.observe()
            assert resource.token == token, "lease must renew beyond native 1s expiry"
            assert manager.cancel(record.run_id, "interrupt")
            await asyncio.gather(record.task, return_exceptions=True)
            assert record.status == RunStatus.interrupted and resource.token is None
            old_id = resource.browser.browser_id
            assert (await browsers.present(a))["browser_id"] == old_id
            print("PASS native renewal beyond TTL, Run cancellation releases control and retains original page")
            second, active = await pending_run("disconnect-run")
            hwnd = int(active.browser.hwnd)
            old_transport = browsers.transport
            await old_transport.disconnect()
            await asyncio.wait_for(second.task, 3)
            assert second.status == RunStatus.error and "disconnected" in second.error
            assert user32.IsWindow(hwnd)
            await error_code("disconnected", browsers.ensure(a))
            replacement = await browsers.replace(a)
            assert replacement["browser_id"] != old_id
            assert user32.IsWindow(hwnd), "old disconnected window belongs to manual user"
            user32.SendMessageW(hwnd, 0x0010, 0, 0)
            await asyncio.to_thread(old_transport.process.wait, 10)
            print("PASS disconnect immediately fails active Run, invalidates old token, explicit replacement uses a new connection")
        finally:
            for resource in tuple(browsers.runs):
                await resource.finish()
            if not browsers.transport.failure:
                for browser in tuple(browsers.browsers.values()):
                    if browser.host_id == browsers.transport.host_id and browser.state != "closed":
                        await browsers.transport.call("close", browser_id=browser.browser_id)
                await browsers.close()
            await engine.dispose()
    asyncio.run(scenario())


def test_real_agent_tool_calls_through_run_worker():
    from langchain.agents import create_agent
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from backend.app.desktop.browser.middleware import BrowserControlMiddleware
    from focus.security.middleware import AccessPolicyMiddleware
    from focus.security.policy import AccessMode
    from langgraph.checkpoint.memory import InMemorySaver

    class BrowserModel(BaseChatModel):
        url: str
        @property
        def _llm_type(self):
            return "scripted-browser-acceptance"
        def bind_tools(self, tools, **kwargs):
            return self
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            outputs = [message for message in messages if isinstance(message, ToolMessage)]
            observations = [json.loads(message.content[0]["text"]) for message in outputs if message.name == "browser_observe"]
            view = observations[-1] if observations else None
            args = {"observation_id": view["observation_id"]} if view else {}
            sx = view["image"]["width"] / view["viewport"]["width"] if view else 1
            sy = view["image"]["height"] / view["viewport"]["height"] if view else 1
            calls = [("browser_observe", {}), ("browser_navigate", {**args, "url": self.url}),
                     ("browser_observe", {}), ("browser_click", {**args, "x": 80*sx, "y": 40*sy}),
                     ("browser_type", {**args, "text": "Focus Agent 实际输入"}),
                     ("browser_click", {**args, "x": 80*sx, "y": 105*sy}),
                     ("browser_key", {**args, "key": "Tab"}),
                     ("browser_scroll", {**args, "x": 400*sx, "y": 200*sy, "delta_y": 20}),
                     ("browser_observe", {}), ("browser_release", {})]
            if len(outputs) < len(calls):
                name, fields = calls[len(outputs)]
                message = AIMessage(content="", tool_calls=[{"id": f"call-{len(outputs)}", "name": name, "args": fields}])
            else:
                assert all(not (isinstance(item.content, str) and '"ok": false' in item.content) for item in outputs)
                message = AIMessage(content="Observed the result and released browser control.")
            return ChatResult(generations=[ChatGeneration(message=message)])

    async def scenario():
        engine, browsers, (a, _, _) = await setup()
        fixture = BrowserFixture()
        manager = RunManager()
        record = manager.create("thread-1", run_id="run-1")
        resource = browsers.resource(a, record.run_id)
        context = runtime_context(agent_id="main:c", task_id=a, access_mode=AccessMode.FULL)
        context["security_context"] = replace(context["security_context"], extras={"browser_run": resource, "run_resource": resource})
        async def factory():
            return create_agent(BrowserModel(url=fixture.url("Agent")), tools=build_browser_tools(),
                                middleware=[AccessPolicyMiddleware(), BrowserControlMiddleware()], context_schema=dict)
        try:
            record.task = asyncio.create_task(run_agent(record=record, bridge=browsers.bridge, run_manager=manager,
                app_config=SimpleNamespace(), graph_input={"messages": [{"role": "user", "content": "Fill and apply"}]},
                runnable_config={"recursion_limit": 60, "configurable": {"thread_id": "browser-agent"}},
                langgraph_context=context, agent_factory=factory, checkpointer=InMemorySaver()))
            await asyncio.wait_for(record.task, 25)
            assert record.status == RunStatus.success, record.error
            actual = await asyncio.to_thread(fixture.wait, lambda row: row["name"] == "Agent" and row["count"] == 1)
            assert actual["text"] == "Focus Agent 实际输入"
            assert resource.token is None
            assert (await browsers.present(a))["browser_id"] == resource.browser.browser_id
            print("PASS actual Agent tool registration/injection, observe/navigate/click/type/key/scroll/release, native page result, worker lifecycle")

            # Closing a window while the next model is pending fails the Run now.
            second = manager.create("thread-2", run_id="run-2")
            pending = browsers.resource(a, second.run_id)
            ctx = runtime_context(agent_id="main:c", task_id=a, run_id="run-2")
            ctx["security_context"] = replace(ctx["security_context"], extras={"run_resource": pending})
            entered = asyncio.Event()
            class PendingGraph:
                async def astream(self, *args, **kwargs):
                    entered.set()
                    await asyncio.Event().wait()
                    yield "values", {}
            async def pending_factory():
                return PendingGraph()
            second.task = asyncio.create_task(run_agent(record=second, bridge=browsers.bridge, run_manager=manager,
                app_config=SimpleNamespace(), graph_input={}, runnable_config={}, langgraph_context=ctx, agent_factory=pending_factory))
            await entered.wait()
            await browsers.transport.call("close", browser_id=resource.browser.browser_id)
            await asyncio.wait_for(second.task, 3)
            assert second.status == RunStatus.error and "closed" in second.error
            print("PASS native window closure immediately fails a Run awaiting its model")
        finally:
            for browser in tuple(browsers.browsers.values()):
                if browser.state != "closed":
                    await browsers.transport.call("close", browser_id=browser.browser_id)
            await browsers.close()
            fixture.close()
            await engine.dispose()
    asyncio.run(scenario())
