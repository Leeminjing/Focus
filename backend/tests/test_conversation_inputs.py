"""普通会话补充、结算后队列、撤回和启动回执恢复的真实 PostgreSQL/Graph 回归。"""

import backend.app.desktop.capabilities.service as _desktop_capabilities_service
import backend.app.desktop.conversations.compression_service as _desktop_conversations_compression_service
import backend.app.desktop.runs.agent_factory as _desktop_runs_agent_factory
import backend.app.desktop.runs.lifecycle_service as _desktop_runs_lifecycle_service


import asyncio
from datetime import UTC, datetime
from itertools import cycle
import os
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.conversations.inputs import ConversationInput, ConversationInputCreate, ConversationInputs
from backend.app.desktop.conversations.input_delivery import ConversationInputDelivery
from backend.app.desktop.persistence.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.runs.orchestration.admission import RunAdmissionService
from focus.agents.lead_agent_state import LeadAgentState
from focus.context.middleware import WorldStateMiddleware
from focus.history.middleware import TypedHistoryMiddleware
from focus.security.context import AuthorizationIdentity, RoutingIdentity, SecurityContext
from focus.security.policy import AccessMode

pytestmark = pytest.mark.usefixtures("runtime_postgres_database")


class Desktop:
    def __init__(self, sessions):
        self.session_factory = sessions
        self.started = []

    async def run_by_idempotency(self, key):
        async with self.session_factory() as session:
            row = await session.scalar(select(DesktopRun).where(DesktopRun.idempotency_key == key))
            return {"run_id": row.run_id} if row else None

    async def start_main_run(self, **body):
        self.started.append(body)
        identity = body["run_identity"]
        run = make_run(body["task_id"], identity["message_id"], body["message"])
        run.idempotency_key = identity["idempotency_key"]
        async with self.session_factory.begin() as session:
            result = await RunAdmissionService().admit(session, run)
        return SimpleNamespace(payload={"run_id": result.run.run_id})


def make_run(task, identity=None, message="initial"):
    return DesktopRun(run_id=identity or uuid.uuid4().hex, task_id=task, agent_id=f"main:{task}",
        kind="main", origin="direct_user", status="running", execution_thread_id=task, checkpoint_ns="",
        input_messages=[{"role": "human", "content": message}], equipment={}, workspace_anchor={})


async def setup(path):
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    task = uuid.uuid4().hex
    async with sessions.begin() as session:
        session.add(DesktopWorkspace(workspace_id=task, path=str(path), display_name="conversation"))
        await session.flush()
        session.add(DesktopThread(task_id=task, thread_id=task, workspace_id=task, title="conversation"))
        await session.flush()
        run = make_run(task)
        session.add(run)
    desktop = Desktop(sessions)
    return engine, sessions, task, run, desktop, ConversationInputs(desktop)


async def settle(sessions, run_id, status="success", settled=True):
    async with sessions.begin() as session:
        row = await session.get(DesktopRun, run_id)
        row.status = status
        row.settled_at = datetime.now(UTC) if settled else None


def test_queue_order_settlement_withdraw_and_crash_recovery(tmp_path):
    async def scenario():
        engine, sessions, task, run, desktop, inputs = await setup(tmp_path)
        try:
            first_body = ConversationInputCreate(mode="queue", run={"message": "first", "skills": ["test"],
                "material_inputs": [{"material_id": "material", "note": "notes"}], "access_mode": "read-only"})
            first, duplicate = await asyncio.gather(inputs.submit(task, first_body, "first"), inputs.submit(task, first_body, "first"))
            assert first["input_id"] == duplicate["input_id"]
            second = await inputs.submit(task, ConversationInputCreate(mode="queue", run={"message": "second"}), "second")
            removed = await inputs.submit(task, ConversationInputCreate(mode="queue", run={"message": "delete"}), "delete")
            await inputs.withdraw(task, removed["input_id"])
            await inputs.drain()
            assert desktop.started == []
            await settle(sessions, run.run_id, settled=False)
            await inputs.drain()
            assert desktop.started == []  # success 不等于清理、结算已完成
            await settle(sessions, run.run_id)
            await asyncio.gather(inputs.drain(), inputs.drain())
            assert len(desktop.started) == 1
            assert desktop.started[0]["skills"] == ["test"]
            assert desktop.started[0]["material_inputs"][0].note == "notes"
            assert desktop.started[0]["access_mode"] == "read-only"
            with pytest.raises(HTTPException) as error:
                await inputs.withdraw(task, first["input_id"])
            assert error.value.status_code == 409
            # 准入已提交、回执未保存的崩溃窗口。
            async with sessions.begin() as session:
                row = await session.get(ConversationInput, first["input_id"])
                row.status, row.run_id = "queued", None
            with pytest.raises(HTTPException) as interrupted_receipt:
                await inputs.withdraw(task, first["input_id"])
            assert interrupted_receipt.value.status_code == 409
            await ConversationInputs(desktop).drain()
            assert len(desktop.started) == 1
            await settle(sessions, first["input_id"])
            await inputs.drain()
            assert [body["message"] for body in desktop.started] == ["first", "second"]
            assert (await inputs.withdraw(task, removed["input_id"]))["status"] == "withdrawn"
            assert any(row["input_id"] == second["input_id"] and row["status"] == "started" for row in await inputs.list(task))
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_steer_scoping_idempotency_and_unconsumed_receipt(tmp_path):
    async def scenario():
        engine, sessions, task, run, desktop, inputs = await setup(tmp_path)
        try:
            body = ConversationInputCreate(mode="steer", target_run_id=run.run_id, run={"message": "先不要改数据库"})
            accepted = await inputs.submit(task, body, "steer")
            assert (await inputs.submit(task, body, "steer"))["input_id"] == accepted["input_id"]
            with pytest.raises(HTTPException) as changed:
                await inputs.submit(task, body.model_copy(update={"run": body.run.model_copy(update={"message": "changed"})}), "steer")
            assert changed.value.status_code == 409
            with pytest.raises(HTTPException):
                await inputs.submit(task, body.model_copy(update={"target_run_id": uuid.uuid4().hex}), "wrong")
            queued = await inputs.submit(task, ConversationInputCreate(mode="queue", run={"message": "tests"}), "queue")
            await settle(sessions, run.run_id, "interrupted")
            await inputs.drain()
            rows = {row["input_id"]: row for row in await inputs.list(task)}
            assert rows[accepted["input_id"]]["status"] == "not_delivered"
            assert rows[queued["input_id"]]["status"] == "blocked"
            assert desktop.started == []
            assert (await inputs.submit(task, body, "steer"))["status"] == "not_delivered"
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("timing", ["before_model", "during_final", "before_tool", "checkpoint_failure"])
def test_steer_reaches_typed_checkpoint_before_model_and_stops_stale_tool(tmp_path, timing, monkeypatch):
    async def scenario():
        engine, sessions, task, run, desktop, inputs = await setup(tmp_path)
        received, calls, tools = [], [], []
        failures = []
        class Saver(InMemorySaver):
            async def aput(self, config, checkpoint, metadata, new_versions):
                if timing == "checkpoint_failure" and not failures and any(item.get("origin") == "direct_user"
                    for item in checkpoint.get("channel_values", {}).get("execution_items", [])):
                    failures.append(True)
                    raise RuntimeError("injected checkpoint failure")
                return await super().aput(config, checkpoint, metadata, new_versions)
        saver = Saver()
        async def submit():
            received.append(await inputs.submit(task, ConversationInputCreate(mode="steer", target_run_id=run.run_id,
                run={"message": "只修前端"}), "steer"))
        @tool
        def modify_database() -> str:
            """Modify the database."""
            tools.append(True)
            return "modified"
        class Model(GenericFakeChatModel):
            def bind_tools(self, tools, **kwargs):
                return self

            async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
                calls.append(messages)
                if timing in {"during_final", "before_tool"} and len(calls) == 1:
                    await submit()
                else:
                    async with sessions() as session:
                        row = await session.get(ConversationInput, received[0]["input_id"])
                        assert row.status == "delivered" and row.checkpoint_id
                return await super()._agenerate(messages, stop, run_manager, **kwargs)
        answers = [AIMessage(content="done")]
        if timing == "before_tool":
            answers.insert(0, AIMessage(content="", tool_calls=[{"id": "old-tool", "name": "modify_database", "args": {}}]))
        from focus.agents.lead import agent as lead
        from backend.tests.tool_catalog_support import registry_for
        model = Model(messages=iter(answers) if timing == "before_tool" else cycle(answers))
        monkeypatch.setattr(lead, "create_chat_model", lambda **kwargs: model)
        monkeypatch.setattr(lead, "get_plugin_registry", lambda: registry_for([]))
        graph = await lead.make_lead_agent(model_name="fixture", tools=[modify_database], middlewares=[],
            system_prompt="base", user_input_middleware=ConversationInputDelivery(sessions, saver))
        graph.checkpointer = saver
        security = SecurityContext(AuthorizationIdentity(tmp_path, (tmp_path,), ("read",), AccessMode.READ_ONLY, "main"),
            RoutingIdentity(task, task, f"main:{task}", task, "", run.run_id))
        try:
            if timing in {"before_model", "checkpoint_failure"}:
                await submit()
            config = {"configurable": {"thread_id": task}}
            initial = {"messages": [HumanMessage(content="initial")]}
            if timing == "checkpoint_failure":
                with pytest.raises(RuntimeError, match="injected checkpoint failure"):
                    await graph.ainvoke(initial, config, context=security.to_runtime_context(), durability="sync")
                assert calls == []
                assert (await inputs.list(task))[0]["status"] == "accepted"
                initial = None
            result = await graph.ainvoke(initial, config, context=security.to_runtime_context(), durability="sync")
            assert len(calls) == (2 if timing in {"during_final", "before_tool"} else 1)
            assert any(message.content == "只修前端" for message in calls[-1])
            assert len([message for message in result["messages"] if message.id == received[0]["input_id"]]) == 1
            assert tools == []
            assert any(item["origin"] == "direct_user" and item["message_id"] == received[0]["input_id"]
                       for item in result["execution_items"])
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("finalization_fault", [None, "before_commit", "after_commit"])
def test_http_delivery_and_queued_run_use_production_dispatch(tmp_path, monkeypatch, wait_until, finalization_fault):
    """HTTP→准入→真实 dispatcher/worker→精确补充 checkpoint→结算→下一轮。"""
    from backend.app.gateway.app import app
    from backend.app.desktop.composition import service as service_module
    from fastapi.testclient import TestClient

    controls, sampled, assembled = {}, [], []

    async def empty_tools(**kwargs):
        return []

    async def make_agent(**kwargs):
        assembled.append((kwargs.get("model_name"), kwargs.get("reasoning_effort")))
        class Model(GenericFakeChatModel):
            async def _astream(self, messages, stop=None, run_manager=None, **options):
                sampled.append(messages)
                if len(sampled) == 1:
                    controls["started"], controls["release"] = True, asyncio.Event()
                    await controls["release"].wait()
                async for chunk in super()._astream(messages, stop, run_manager, **options):
                    yield chunk
        return create_agent(Model(messages=cycle([AIMessage(content="answer")])), [],
            middleware=[kwargs["user_input_middleware"], *kwargs["additional_middlewares"],
                WorldStateMiddleware([], "base", frozenset(), "test", "v1"), TypedHistoryMiddleware()],
            state_schema=LeadAgentState, context_schema=dict)

    monkeypatch.setattr(_desktop_capabilities_service, 'get_available_tools', empty_tools)
    monkeypatch.setattr(_desktop_runs_agent_factory, 'get_available_tools', empty_tools)
    monkeypatch.setattr(_desktop_conversations_compression_service, 'make_lead_agent', make_agent)
    monkeypatch.setattr(_desktop_runs_agent_factory, 'make_lead_agent', make_agent)
    monkeypatch.setattr(_desktop_runs_lifecycle_service, '_RUN_FINALIZATION_RETRY_INITIAL_SECONDS', 0.01)
    headers = {"X-Focus-Session": "focus-dev-session"}
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        config = app.state.desktop_service.app_config
        selected_model = config.get_model(config.resolve_default_model_name())
        monkeypatch.setattr(selected_model, "reasoning_efforts", ["high"])
        model_options = {"model_name": selected_model.name, "reasoning_effort": "high"}
        folder = tmp_path / "workspace"
        folder.mkdir()
        workspace = client.post("/desktop/api/workspaces", headers=headers, json={"path": str(folder)})
        assert workspace.status_code == 200, workspace.text
        task = client.post(f"/desktop/api/workspaces/{workspace.json()['workspace_id']}/threads", headers=headers,
                           json={"thread_id": f"conversation-{uuid.uuid4().hex}", "title": "conversation"}).json()
        path = f"/desktop/api/tasks/{task['task_id']}/main"
        unsupported = client.post(f"{path}/runs", headers=headers,
            json={"message": "invalid effort", "model_name": selected_model.name, "reasoning_effort": "low"})
        assert unsupported.status_code == 422, unsupported.text
        initial = client.post(f"{path}/runs", headers=headers, json={"message": "initial", "permissions": ["read"], **model_options})
        assert initial.status_code == 200, initial.text
        run = initial.json()
        service = app.state.desktop_service
        finalizer = service.run_lifecycle
        original_finalize = finalizer.finalize
        original_enqueue = finalizer._outbox.enqueue_settled
        attempts = []

        async def enqueue(session, run_id, payload):
            event = await original_enqueue(session, run_id, payload)
            if finalization_fault == "before_commit" and run_id == run["run_id"] and len(attempts) == 1:
                raise RuntimeError("injected settlement transaction failure")
            return event

        async def finalize(record, *args, **kwargs):
            if record.run_id == run["run_id"]:
                attempts.append(record.run_id)
                if len(attempts) == 2:
                    controls["retry_waiting"] = True
                    controls["retry_release"] = asyncio.Event()
                    await controls["retry_release"].wait()
            result = await original_finalize(record, *args, **kwargs)
            if finalization_fault == "after_commit" and record.run_id == run["run_id"] and len(attempts) == 1:
                raise RuntimeError("injected lost settlement acknowledgement")
            return result

        monkeypatch.setattr(finalizer._outbox, "enqueue_settled", enqueue)
        monkeypatch.setattr(finalizer, "finalize", finalize)
        try:
            wait_until(lambda: controls.get("started") or client.get(
                f"/desktop/api/runs/{run['run_id']}", headers=headers).json()["status"] in {"error", "interrupted"}, timeout=30)
        except AssertionError:
            async def dispatch_state():
                service = app.state.desktop_service
                dispatcher = service._run_dispatch_task
                current = await service.get_run_payload(run["run_id"])
                return {"dispatcher_done": dispatcher is None or dispatcher.done(),
                        "status": current["status"], "error": current["error"], "dispatch": current.get("dispatch")}
            raise AssertionError(client.portal.call(dispatch_state)) from None
        assert controls.get("started"), client.get(f"/desktop/api/runs/{run['run_id']}", headers=headers).json()
        steer = client.post(f"{path}/inputs", headers={**headers, "Idempotency-Key": "steer"},
            json={"mode": "steer", "target_run_id": run["run_id"], "run": {"message": "只修前端"}})
        assert steer.status_code == 200, steer.text
        queued = client.post(f"{path}/inputs", headers={**headers, "Idempotency-Key": "queue"},
            json={"mode": "queue", "run": {"message": "补一组测试", "permissions": ["read"], **model_options}})
        assert queued.status_code == 200, queued.text
        assert steer.json()["status"] == "accepted" and queued.json()["status"] == "queued"
        async def release():
            controls["release"].set()
        client.portal.call(release)
        if finalization_fault:
            wait_until(lambda: controls.get("retry_waiting"), timeout=30)
            if finalization_fault == "before_commit":
                async def unsettled():
                    async with service.session_factory() as session:
                        row = await session.get(DesktopRun, run["run_id"])
                        return row.status, row.settled_at
                assert client.portal.call(unsettled) == ("running", None)
                assert next(row for row in client.get(f"{path}/inputs", headers=headers).json()
                            if row["input_id"] == queued.json()["input_id"])["status"] == "queued"
                assert len(assembled) == 1
            async def release_retry():
                controls["retry_release"].set()
            client.portal.call(release_retry)
        wait_until(lambda: any(row["status"] == "started" for row in client.get(f"{path}/inputs", headers=headers).json()), timeout=30)
        rows = {row["input_id"]: row for row in client.get(f"{path}/inputs", headers=headers).json()}
        assert rows[steer.json()["input_id"]]["status"] == "delivered"
        second_run = rows[queued.json()["input_id"]]["run_id"]
        wait_until(lambda: client.get(f"/desktop/api/runs/{second_run}", headers=headers).json()["status"] == "success", timeout=30)
        assert any(any(message.content == "只修前端" for message in messages) for messages in sampled)
        assert any(any(message.content == "补一组测试" for message in messages) for messages in sampled)
        detail = client.get(f"/desktop/api/tasks/{task['task_id']}", headers=headers).json()
        assert len(detail["conversation_inputs"]) == 2
        assert detail["latest_main_run"]["model_name"] == selected_model.name
        assert detail["latest_main_run"]["equipment"]["reasoning_effort"] == "high"
        assert detail["context_usage"]["estimated_tokens"] > 0
        assert assembled == [(selected_model.name, "high"), (selected_model.name, "high")]
        wait_until(lambda: run["run_id"] not in service._pending_run_finalizations, timeout=30)

        async def settlement_facts():
            from backend.app.desktop.contexts.evolution.models import ContextRevision
            from backend.app.desktop.runs.orchestration.models import RunDispatch, RunOutboxEvent

            async with service.session_factory() as session:
                runs = (await session.scalars(select(DesktopRun).where(DesktopRun.task_id == task["task_id"]))).all()
                revisions = (await session.scalars(select(ContextRevision).where(
                    ContextRevision.origin_id == run["run_id"], ContextRevision.origin_kind == "run_settled"))).all()
                events = (await session.scalars(select(RunOutboxEvent).where(RunOutboxEvent.run_id == run["run_id"]))).all()
                dispatch = await session.scalar(select(RunDispatch).where(RunDispatch.run_id == run["run_id"]))
                return len(runs), len(revisions), len(events), dispatch.status, all(row.settled_at for row in runs)

        wait_until(lambda: client.portal.call(settlement_facts)[-1], timeout=30)
        assert client.portal.call(settlement_facts)[:4] == (2, 1, 1, "settled")
        assert len(attempts) == (2 if finalization_fault else 1)
