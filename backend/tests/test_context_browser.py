"""Protocol multiplexing and the existing Run worker's browser resource boundary."""
import asyncio
from dataclasses import replace
import json
import sys
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
