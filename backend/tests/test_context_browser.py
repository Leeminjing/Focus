"""Protocol multiplexing and the existing Run worker's browser resource boundary."""
import asyncio
from dataclasses import replace
import json
import sys

import pytest
from types import SimpleNamespace

from backend.app.desktop.browser.transport import BrowserError, BrowserTransport
from backend.tests.runtime_context_support import runtime_context, tool_runtime
from focus.runtime.runs.manager import RunManager
from focus.runtime.runs.schemas import RunStatus
from focus.runtime.runs.worker import run_agent
from focus.runtime.stream_bridge.memory import MemoryStreamBridge


def test_jsonl_out_of_order_and_events_do_not_block_other_browser(tmp_path):
    host = tmp_path / "host.py"
    host.write_text('''import json,sys
print(json.dumps({"event":"ready","pid":1,"protocol_version":1}),flush=True)
held=None
for line in sys.stdin:
 r=json.loads(line)
 if r["method"]=="slow": held=r; continue
 print(json.dumps({"event":"control_changed","browser_id":"B","state":"manual"}),flush=True)
 print(json.dumps({"id":r["id"],"ok":True,"result":{"label":r["params"]["label"]}}),flush=True)
 if held:
  print(json.dumps({"id":held["id"],"ok":True,"result":{"done":True}}),flush=True)
  held=None
''', encoding="utf-8")

    async def scenario():
        events = []
        transport = BrowserTransport([sys.executable, "-u", str(host)], events.append)
        slow = asyncio.create_task(transport.call("slow"))
        await transport.start()
        fast = await transport.call("fast", label="你好")
        assert fast == {"label": "你好"}
        assert await slow == {"done": True}
        assert events[1]["state"] == "manual"
        await transport.disconnect()
        await asyncio.to_thread(transport.process.wait, 5)
        try:
            await transport.call("create")
        except BrowserError as error:
            assert error.code == "connection_lost"
        else:
            raise AssertionError("old connection reused")
    asyncio.run(scenario())


def test_uncertain_tool_result_is_an_error_message_and_is_not_replayed():
    from backend.app.desktop.browser.tools import build_browser_tools
    from langchain_core.messages import ToolMessage
    class Run:
        calls = 0
        async def act(self, *args):
            self.calls += 1
            raise BrowserError("action_failed", "partial input", {"dispatched_steps": 1, "result_unknown": True})
    run = Run()
    runtime = tool_runtime(agent_id="main:c", task_id="c")
    runtime.context["security_context"] = replace(runtime.context["security_context"], extras={"browser_run": run})
    tools = {item.name: item for item in build_browser_tools()}
    result = asyncio.run(tools["browser_click"].ainvoke({"type": "tool_call", "id": "uncertain", "name": "browser_click",
        "args": {"x": 1, "y": 2, "observation_id": "view", "runtime": runtime}}))
    assert isinstance(result, ToolMessage) and result.status == "error"
    payload = json.loads(result.content)
    assert payload["error"]["detail"]["result_unknown"] is True
    assert payload["retry_automatically"] is False and run.calls == 1


def test_create_failure_never_announces_running_and_always_finishes():
    class Resource:
        failure = None
        finished = False
        async def start(self):
            raise BrowserError("creation_failed", "test failure")
        async def finish(self):
            self.finished = True

    async def scenario():
        manager, bridge, resource = RunManager(), MemoryStreamBridge(), Resource()
        record = manager.create("thread-1", run_id="run-1")
        context = runtime_context(agent_id="main:c", task_id="c")
        security = context["security_context"]
        context["security_context"] = replace(security, extras={"run_resource": resource})
        await run_agent(record=record, bridge=bridge, run_manager=manager,
                        app_config=SimpleNamespace(), graph_input={}, runnable_config={}, langgraph_context=context)
        assert record.status == RunStatus.error
        assert "creation_failed" in record.error
        assert resource.finished
        frames = bridge._streams[record.run_id].events
        assert not any(frame.event == "metadata" and frame.data["data"].get("status") == "running" for frame in frames)
        assert any(frame.event == "error" for frame in frames)
    asyncio.run(scenario())


def test_closed_browser_interrupts_pending_model_as_run_error_and_releases():
    class Resource:
        failure = None
        finished = False
        async def start(self):
            self.task = asyncio.current_task()
        def check(self):
            if self.failure:
                raise self.failure
        async def finish(self):
            self.finished = True

    async def scenario():
        manager, bridge, resource = RunManager(), MemoryStreamBridge(), Resource()
        record = manager.create("thread-1", run_id="run-1")
        context = runtime_context(agent_id="main:c", task_id="c")
        context["security_context"] = replace(context["security_context"], extras={"run_resource": resource})
        entered = asyncio.Event()
        class Graph:
            async def astream(self, *args, **kwargs):
                entered.set()
                await asyncio.Event().wait()
                yield "values", {}
        async def factory():
            return Graph()
        record.task = asyncio.create_task(run_agent(record=record, bridge=bridge, run_manager=manager,
            app_config=SimpleNamespace(), graph_input={}, runnable_config={}, langgraph_context=context, agent_factory=factory))
        await entered.wait()
        resource.failure = BrowserError("closed", "native window closed")
        resource.task.cancel()
        await asyncio.wait_for(record.task, 2)
        assert record.status == RunStatus.error
        assert "closed" in record.error
        assert resource.finished
    asyncio.run(scenario())


def test_late_context_selection_cannot_present_over_the_newer_context():
    from backend.app.desktop.browser.service import BrowserSession, ContextBrowsers
    from backend.app.desktop.browser.routes import BrowserPresentation
    async def scenario():
        browsers = ContextBrowsers(None, MemoryStreamBridge())
        delayed = asyncio.Event()
        presented = []
        async def binding(context_id):
            if context_id == "A":
                await delayed.wait()
            return BrowserSession(context_id, "available", browsers.transport.host_id)
        async def snapshot(context_id):
            return {"context_id": context_id, "state": "available", "browser_id": context_id}
        async def start():
            pass
        async def call(method, **params):
            presented.append(params["browser_id"])
            return {"state": "available", "visible": True, "foreground": False}
        browsers.binding, browsers.snapshot = binding, snapshot
        browsers.transport.start, browsers.transport.call = start, call
        earlier = asyncio.create_task(browsers.present("A", BrowserPresentation(client_id="window", sequence=1)))
        await asyncio.sleep(0)
        await browsers.present("B", BrowserPresentation(client_id="window", sequence=2))
        delayed.set()
        assert (await earlier)["superseded"] is True
        assert presented == ["B"]
    asyncio.run(scenario())


def page_run():
    from backend.app.desktop.browser.service import BrowserSession, ContextBrowsers
    owner = ContextBrowsers(None, MemoryStreamBridge())
    browser = BrowserSession("browser", "automatic", owner.transport.host_id, selected_page_id="parent")
    owner.browsers[browser.browser_id] = browser
    run = owner.resource("context", "run")
    run.browser, run.token, run._task = browser, "lease", None
    owner.runs.add(run)
    owner.lease_owners[browser.browser_id] = run
    return owner, run


def test_page_targets_are_bound_to_observation_and_selection_revokes_only_observation():
    async def scenario():
        owner, run = page_run()
        calls = []
        async def call(method, **params):
            calls.append((method, params))
            if method == "observe":
                return {"page_id": "parent", "viewport": {"width": 100, "height": 100}, "image": {"width": 200, "height": 200}}
            return {"control_still_valid": True}
        owner.transport.call = call
        view = await run.observe("parent")
        owner._event({"event": "navigation_completed", "browser_id": "browser", "state": "automatic", "detail": {"page_id": "background"}})
        await run.act(view["observation_id"], {"type": "click", "x": 80, "y": 40})
        assert calls[-1][1]["page_id"] == "parent" and calls[-1][1]["action"]["x"] == 40
        owner._event({"event": "page_selected", "browser_id": "browser", "state": "automatic", "detail": {"page_id": "child"}})
        assert run.token == "lease" and owner.lease_owners["browser"] is run
        with pytest.raises(BrowserError, match="observation_required"):
            await run.act(view["observation_id"], {"type": "text", "text": "stale"})
        assert len(calls) == 2
        owner._event({"event": "page_closed", "browser_id": "browser", "state": "automatic", "detail": {"page_id": "child"}})
        assert run.failure is None and run.token == "lease"
    asyncio.run(scenario())


def test_late_capture_cannot_restore_observation_after_page_switch():
    async def scenario():
        owner, run = page_run()
        entered, finish = asyncio.Event(), asyncio.Event()
        async def call(method, **params):
            entered.set()
            await finish.wait()
            return {"page_id": "parent"}
        owner.transport.call = call
        task = asyncio.create_task(run.observe())
        await entered.wait()
        owner._event({"event": "page_selected", "browser_id": "browser", "state": "automatic", "detail": {"page_id": "child"}})
        finish.set()
        with pytest.raises(BrowserError, match="page_changed"):
            await task
        assert run.observation is None and run.token == "lease"
    asyncio.run(scenario())


def test_page_management_needs_browser_lease_and_never_grants_an_observation():
    async def scenario():
        owner, run = page_run()
        calls = []
        async def call(method, **params):
            calls.append((method, params))
            return {"pages": [], "selected_page_id": "parent"} if method == "list_pages" else {"page_id": params["page_id"]}
        owner.transport.call = call
        run.observation = {"observation_id": "old", "page_id": "parent"}
        await run.list_pages()
        assert run.observation is not None and "token" not in calls[-1][1]
        await run.page_command("select_page", "child")
        assert run.observation is None and calls[-1][1]["token"] == "lease"
        owner._event({"event": "control_changed", "browser_id": "browser", "state": "manual", "detail": {"reason": "human_input"}})
        with pytest.raises(BrowserError, match="control_unavailable"):
            await run.page_command("close_page", "child")
        assert len(calls) == 2 and run.token is None
    asyncio.run(scenario())


def test_download_tool_is_read_only_during_manual_control_and_has_no_grant_interface():
    from backend.app.desktop.browser.tools import build_browser_tools
    async def scenario():
        owner, run = page_run()
        owner._event({"event": "control_changed", "browser_id": "browser", "state": "manual",
                      "detail": {"reason": "download_started"}})
        calls = []
        async def call(method, **params):
            calls.append((method, params))
            return {"downloads": [{"download_id": "file", "page_id": "parent", "state": "in_progress",
                                   "bytes_received": 512, "total_bytes": 2048, "path": "C:/Downloads/file.txt"}],
                    "interaction_pending": True}
        owner.transport.call = call
        runtime = tool_runtime(agent_id="main:context", task_id="context")
        runtime.context["security_context"] = replace(runtime.context["security_context"], extras={"browser_run": run})
        tools = {item.name: item for item in build_browser_tools()}
        result = json.loads(await tools["browser_list_downloads"].ainvoke({"runtime": runtime}))
        assert result["downloads"][0]["bytes_received"] == 512 and result["interaction_pending"]
        assert calls == [("list_downloads", {"browser_id": "browser"})]
        assert run.token is None and run.observation is None and run.browser.state == "manual"
        assert not any("grant" in name or "permission" in name or "cancel_download" in name for name in tools)
    asyncio.run(scenario())


def test_site_events_notify_shared_runs_and_do_not_resume_them_or_affect_other_browsers():
    from backend.app.desktop.browser.service import BrowserSession
    async def scenario():
        owner, run = page_run()
        shared = owner.resource("shared-context", "shared-run")
        shared.browser, shared._task = run.browser, None
        owner.runs.add(shared)
        other = owner.resource("other-context", "other-run")
        other.browser = BrowserSession("other", "automatic", owner.transport.host_id)
        other.token, other.observation, other._task = "independent", {"observation_id": "valid"}, None
        owner.runs.add(other)
        run.observation = {"observation_id": "old", "page_id": "parent"}
        events = [
            {"event": "download_changed", "detail": {"download_id": "file", "page_id": "child", "state": "cancelled", "reason": "human_cancel"}},
            {"event": "download_rejected", "detail": {"page_id": "child", "reason": "download_limit"}},
            {"event": "permission_requested", "detail": {"request_id": "permission", "page_id": "parent", "kind": "geolocation", "url": "https://site.example/", "state": "pending"}},
            {"event": "permission_resolved", "detail": {"request_id": "permission", "page_id": "parent", "kind": "geolocation", "url": "https://site.example/", "state": "denied", "reason": "human_decision"}},
        ]
        for event in events:
            owner._event({**event, "browser_id": "browser", "state": "manual"})
        async def call(method, **params):
            assert method == "list_downloads" and params == {"browser_id": "browser"}
            return {"downloads": [events[0]["detail"]], "interaction_pending": False}
        owner.transport.call = call
        listing = await run.list_downloads()
        assert listing["permissions"][0]["state"] == "denied"
        assert listing["rejected"][0]["reason"] == "download_limit"
        assert not listing["interaction_pending"] and run.browser.state == "manual"
        assert run.token is None and run.observation is None and shared.handoff
        assert other.token == "independent" and other.observation == {"observation_id": "valid"}
        for resource in (run, shared):
            owner.bridge.publish_end(resource.run_id)
            frames = [frame async for frame in owner.bridge.subscribe(resource.run_id) if getattr(frame, "event", None) == "browser"]
            assert [frame.data["data"]["event"] for frame in frames] == [event["event"] for event in events]
    asyncio.run(scenario())


def test_model_waits_for_explicit_handback_then_receives_download_and_permission_results():
    from backend.app.desktop.browser.middleware import BrowserControlMiddleware
    from langchain_core.messages import HumanMessage
    async def scenario():
        owner, run = page_run()
        runtime = tool_runtime(agent_id="main:context", task_id="context")
        runtime.context["security_context"] = replace(runtime.context["security_context"], extras={"browser_run": run})
        request = SimpleNamespace(runtime=runtime, messages=[HumanMessage(content="download")])
        request.override = lambda **fields: SimpleNamespace(**fields)
        owner._event({"event": "permission_requested", "browser_id": "browser", "state": "manual", "detail": {
            "request_id": "location", "page_id": "parent", "kind": "geolocation", "state": "pending"}})
        decisions = []
        async def model(request):
            decisions.extend(message.content for message in request.messages)
            return "continued"
        waiting = asyncio.create_task(BrowserControlMiddleware().awrap_model_call(request, model))
        await asyncio.sleep(0)
        owner._event({"event": "permission_resolved", "browser_id": "browser", "state": "manual", "detail": {
            "request_id": "location", "page_id": "parent", "kind": "geolocation", "state": "denied"}})
        owner._event({"event": "download_changed", "browser_id": "browser", "state": "manual", "detail": {
            "download_id": "file", "page_id": "child", "state": "cancelled", "reason": "human_cancel"}})
        await asyncio.sleep(0)
        assert not decisions and not waiting.done(), "terminal results must not grant control"
        owner._event({"event": "control_changed", "browser_id": "browser", "state": "available", "detail": {"reason": "hand_back"}})
        assert await asyncio.wait_for(waiting, 1) == "continued"
        assert "browser_observe" in decisions[-1] and "browser_list_downloads" in decisions[-1]
        assert "cancelled" in decisions[-1] and "denied" in decisions[-1]
        assert "禁止" in decisions[-1] and run.token is None and run.observation is None
    asyncio.run(scenario())


def test_attachment_navigation_keeps_native_error_and_requires_download_query_without_replay():
    from backend.app.desktop.browser.tools import build_browser_tools
    class Run:
        calls = 0
        async def act(self, *args):
            self.calls += 1
            raise BrowserError("navigation_failed", "CONNECTION_ABORTED", {"web_error_status": 9, "result_unknown": True})
    run = Run()
    runtime = tool_runtime(agent_id="main:context", task_id="context")
    runtime.context["security_context"] = replace(runtime.context["security_context"], extras={"browser_run": run})
    tools = {item.name: item for item in build_browser_tools()}
    message = asyncio.run(tools["browser_navigate"].ainvoke({"type": "tool_call", "id": "attachment", "name": "browser_navigate",
        "args": {"url": "https://site.example/file", "observation_id": "view", "runtime": runtime}}))
    result = json.loads(message.content)
    assert message.status == "error" and result["error"]["code"] == "navigation_failed"
    assert result["error"]["detail"]["web_error_status"] == 9 and result["error"]["detail"]["result_unknown"]
    assert result["error"]["detail"]["next_tool"] == "browser_list_downloads"
    assert result["retry_automatically"] is False and run.calls == 1


def test_late_download_query_cannot_hide_a_new_permission_wait():
    async def scenario():
        owner, run = page_run()
        async def call(method, **params):
            owner._event({"event": "permission_requested", "browser_id": "browser", "state": "manual", "detail": {
                "request_id": "new", "page_id": "parent", "kind": "geolocation", "state": "pending"}})
            return {"downloads": [], "interaction_pending": False}  # captured before the new request
        owner.transport.call = call
        value = await run.list_downloads()
        assert value["permissions"][0]["state"] == "pending" and value["interaction_pending"]
        assert run.token is None and run.browser.state == "manual"
    asyncio.run(scenario())
