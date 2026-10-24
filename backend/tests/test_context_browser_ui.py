"""Real Focus HTML/API/SSE and native browser bindings in headless Chromium."""
import asyncio
import ctypes
import json
import os
from pathlib import Path
import uuid
import time

import pytest

from backend.tests.test_session_patrol_workbench_api import client as client, SESSION
from backend.tests.http_asgi_bridge import ASGIHTTPBridge

pytestmark = pytest.mark.skipif(os.name != "nt" or os.environ.get("FOCUS_BROWSER_REAL") != "1", reason="opt-in Windows browser UI")


def test_creation_failure_keeps_durable_run_pending_until_failed(client, monkeypatch):
    from backend.app.gateway.app import app
    from backend.app.desktop.browser.transport import BrowserError
    from backend.app.desktop.runs.orchestration.models import RunDispatch
    from sqlalchemy import select
    http, task, _, sampled = client
    service = app.state.desktop_service
    http.portal.call(app.state.agent_loop_runtime.close)
    attempted, gate = asyncio.Event(), asyncio.Event()
    async def create(context_id):
        attempted.set()
        await gate.wait()
        raise BrowserError("creation_failed", "deliberate startup failure")
    monkeypatch.setattr(service.browsers, "ensure", create)
    response = http.post(f"/desktop/api/tasks/{task['task_id']}/main/runs", headers=SESSION,
                         json={"message": "must not run before browser creation", "model_name": "deepseek-v4-flash"})
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    async def reached():
        await asyncio.wait_for(attempted.wait(), 5)
    http.portal.call(reached)
    pending = http.get(f"/desktop/api/runs/{run_id}", headers=SESSION).json()
    assert pending["status"] == "pending", pending
    assert service.run_manager.get(run_id) is None
    assert not sampled
    async def unblock():
        gate.set()
    http.portal.call(unblock)
    deadline = time.monotonic() + 5
    while True:
        failed = http.get(f"/desktop/api/runs/{run_id}", headers=SESSION).json()
        if failed["status"] == "error":
            break
        assert time.monotonic() < deadline, failed
        time.sleep(.025)
    assert "creation_failed" in failed["error"]
    assert service.run_manager.get(run_id) is None and not sampled
    async def dispatch_state():
        async with service.session_factory() as session:
            row = await session.scalar(select(RunDispatch).where(RunDispatch.run_id == run_id))
            return row.status, row.running_at
    assert http.portal.call(dispatch_state) == ("failed_to_start", None)


def test_context_selection_binding_and_native_status_in_real_focus(client, tmp_path, monkeypatch):
    from playwright.sync_api import sync_playwright
    from backend.app.gateway.app import app
    http, first, _, _ = client
    service = app.state.desktop_service
    http.portal.call(app.state.agent_loop_runtime.close)
    second_response = http.post(f"/desktop/api/workspaces/{first['workspace_id']}/threads", headers=SESSION,
        json={"thread_id": uuid.uuid4().hex, "title": "Browser Context B"})
    assert second_response.status_code == 200, second_response.text
    second = second_response.json()
    aid, bid = first["task_id"], second["task_id"]
    def endpoint(context_id):
        return f"/desktop/api/contexts/{context_id}/browser"
    assert http.get(endpoint(aid)).status_code == 401
    assert http.get(endpoint(aid), headers=SESSION).json()["state"] == "unbound"
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from backend.app.desktop.browser.middleware import BrowserControlMiddleware
    from backend.tests.test_session_patrol_workbench_api import wait_run
    class Model(FakeListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self
    async def graph(**kwargs):
        assert "browser_observe" in {tool.name for tool in kwargs["tools"]}
        from focus.tools.catalog import compile_agent_tool_catalog
        middleware = [item for item in kwargs["additional_middlewares"] if isinstance(item, BrowserControlMiddleware)]
        catalog = compile_agent_tool_catalog(kwargs["tools"], middleware)
        return create_agent(Model(responses=["Browser-bound Run completed"]), tools=catalog.regular_tools,
                            middleware=middleware, context_schema=dict)
    monkeypatch.setattr('backend.app.desktop.conversations.compression_service.make_lead_agent', graph)
    monkeypatch.setattr('backend.app.desktop.runs.agent_factory.make_lead_agent', graph)
    started = http.post(f"/desktop/api/tasks/{aid}/main/runs", headers=SESSION,
                        json={"message": "Run with an independent browser", "model_name": "deepseek-v4-flash"})
    assert started.status_code == 200, started.text
    wait_run(http, started.json()["run_id"])
    bound = http.get(endpoint(aid), headers=SESSION).json()
    assert bound["state"] == "available" and bound["browser_id"]
    a_browser = bound["browser_id"]
    same_binding = http.put(endpoint(aid), headers=SESSION, json={"source_context_id": aid})
    assert same_binding.status_code == 409
    # Generic UI persistence cannot forge or erase the server-owned binding.
    saved = http.put(f"/desktop/api/tasks/{aid}/ui-state", headers=SESSION,
                    json={"_distinct_browser": {"browser_id": "forged", "host_id": "forged"}, "input": "draft"})
    assert saved.status_code == 200, saved.text
    assert http.get(endpoint(aid), headers=SESSION).json()["browser_id"] == a_browser

    output = Path(os.environ.get("FOCUS_BROWSER_ARTIFACTS", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    original_policy = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        with ASGIHTTPBridge(http, headers=SESSION) as bridge, sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000}, bypass_csp=True)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(bridge.url + "/desktop/", wait_until="domcontentloaded")
            page.wait_for_function("typeof switchTask === 'function' && state.tasks.length >= 2")
            page.evaluate("id => switchTask(id)", aid)
            page.locator(f'[data-browser-status="{aid}"]').wait_for()
            page.get_by_role("button", name="浏览器绑定", exact=True).click()
            page.get_by_role("heading", name="浏览器绑定", exact=True).wait_for()
            page.screenshot(path=str(output / "browser-binding.png"), full_page=True)
            page.get_by_role("button", name="取消", exact=True).click()
            # Clicking the actual Context rail invokes present on its existing ID.
            page.locator(f'[data-action="context-rail-card"][data-task-id="{bid}"]').click()
            page.locator(f'[data-browser-status="{bid}"]').wait_for()
            page.get_by_role("button", name="浏览器绑定", exact=True).click()
            page.locator('dialog[open] select[name="source"]').select_option(aid)
            page.get_by_role("button", name="应用绑定", exact=True).click()
            page.wait_for_function("!document.querySelector('dialog[open]')")
            page.wait_for_function("id => document.querySelector(`[data-browser-status='${id}']`)?.textContent.includes('2 个 Context')", arg=bid)
            assert http.get(endpoint(bid), headers=SESSION).json()["browser_id"] == a_browser
            page.screenshot(path=str(output / "browser-shared-context.png"), full_page=True)
            hwnd = int(service.browsers.browsers[a_browser].hwnd)
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
            user32.ShowWindow(hwnd, 6)
            page.wait_for_function("id => document.querySelector(`[data-browser-status='${id}']`)?.textContent.includes('已暂停')", arg=bid)
            page.get_by_role("button", name="浏览器", exact=True).click()
            page.wait_for_timeout(100)
            assert http.get(endpoint(bid), headers=SESSION).json()["state"] == "paused"
            user32.SendMessageW(hwnd, 0x0111, 101, 0)
            page.wait_for_function("id => document.querySelector(`[data-browser-status='${id}']`)?.textContent.includes('可申请控制')", arg=bid)
            page.locator(f'[data-action="context-rail-card"][data-task-id="{aid}"]').click()
            page.locator(f'[data-browser-status="{aid}"]').wait_for()
            page.screenshot(path=str(output / "browser-context-view.png"), full_page=True)
            assert any(item[0] == "POST" and item[1] == endpoint(aid) + "/present" and item[2] == 200 for item in bridge.traffic)
            assert not errors, errors
            browser.close()
        print("PASS production Focus Context clicks, browser binding UI, shared status, native pause/present/handback SSE, authenticated API")
    finally:
        asyncio.set_event_loop_policy(original_policy)
        async def close_windows():
            for item in tuple(service.browsers.browsers.values()):
                if item.state not in {"closed", "disconnected"}:
                    await service.browsers.transport.call("close", browser_id=item.browser_id)
        http.portal.call(close_windows)


def test_production_agent_browser_tools_checkpoint_and_lifecycle(client, monkeypatch):
    """Replace provider sampling only; keep production factory, policy and ledger."""
    from backend.app.gateway.app import app
    from backend.tests.browser_fixture import BrowserFixture
    from langchain_core.messages import ToolMessage, AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk
    from focus.models.responses import FocusResponsesChatModel

    http, task, _, _ = client
    service = app.state.desktop_service
    http.portal.call(app.state.agent_loop_runtime.close)
    service.app_config.get_model("deepseek-v4-flash").supports_image_input = True
    fixture = BrowserFixture()
    calls = []

    async def stream(model, messages, stop=None, run_manager=None, **kwargs):
        outputs = [message for message in messages if isinstance(message, ToolMessage)]
        assert not any(message.status == "error" for message in outputs), [(message.name, message.content) for message in outputs]
        views = [json.loads(message.content[0]["text"]) for message in outputs if message.name == "browser_observe"]
        view = views[-1] if views else None
        fields = {"observation_id": view["observation_id"]} if view else {}
        sx = view["image"]["width"] / view["viewport"]["width"] if view else 1
        sy = view["image"]["height"] / view["viewport"]["height"] if view else 1
        steps = [("browser_observe", {}), ("browser_navigate", {**fields, "url": fixture.url("Production")}),
                 ("browser_observe", {}), ("browser_click", {**fields, "x": 80*sx, "y": 40*sy}),
                 ("browser_type", {**fields, "text": "Production Focus tools"}),
                 ("browser_click", {**fields, "x": 80*sx, "y": 105*sy}),
                 ("browser_observe", {}), ("browser_release", {})]
        if len(outputs) < len(steps):
            name, args = steps[len(outputs)]
            calls.append(name)
            yield ChatGenerationChunk(message=AIMessageChunk(content="", id=f"production-{len(outputs)}", tool_call_chunks=[
                {"name": name, "args": json.dumps(args), "id": f"browser-{len(outputs)}", "index": 0}]))
        else:
            yield ChatGenerationChunk(message=AIMessageChunk(content="Verified native browser result.", id="production-final"))

    monkeypatch.setattr(FocusResponsesChatModel, "_astream", stream)
    context_id = task["task_id"]
    try:
        mode = http.put(f"/desktop/api/tasks/{context_id}/access-mode", headers=SESSION, json={"access_mode": "danger-full-access"})
        assert mode.status_code == 200, mode.text
        started = http.post(f"/desktop/api/tasks/{context_id}/main/runs", headers=SESSION,
                            json={"message": "Observe, fill the page and apply", "model_name": "deepseek-v4-flash", "access_mode": "danger-full-access"})
        assert started.status_code == 200, started.text
        run_id = started.json()["run_id"]
        deadline = time.monotonic() + 35
        while True:
            run = http.get(f"/desktop/api/runs/{run_id}", headers=SESSION).json()
            if run["status"] in {"success", "error", "interrupted", "timeout"}:
                break
            assert time.monotonic() < deadline, run
            time.sleep(.05)
        assert run["status"] == "success", run
        assert len(calls) == 8 and calls[-1] == "browser_release", calls
        actual = fixture.wait(lambda row: row["name"] == "Production" and row["count"] == 1)
        assert actual["text"] == "Production Focus tools"
        status = http.get(f"/desktop/api/contexts/{context_id}/browser", headers=SESSION).json()
        assert status["state"] == "available"
        shown = http.post(f"/desktop/api/contexts/{context_id}/browser/present", headers=SESSION)
        assert shown.status_code == 200 and shown.json()["browser_id"] == status["browser_id"]
        print("PASS full production Agent factory, policy, ToolExecutionLedger, typed history/checkpoint, durable Run, native page and retained browser")
    finally:
        async def close_windows():
            for item in tuple(service.browsers.browsers.values()):
                if item.state not in {"closed", "disconnected"}:
                    await service.browsers.transport.call("close", browser_id=item.browser_id)
        http.portal.call(close_windows)
        fixture.close()
