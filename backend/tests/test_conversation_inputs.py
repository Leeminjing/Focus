"""普通会话补充、结算后队列、撤回和启动回执恢复的真实 PostgreSQL/Graph 回归。"""

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

from backend.app.desktop.conversation_inputs import ConversationInput, ConversationInputCreate, ConversationInputs
from backend.app.desktop.conversation_input_delivery import ConversationInputDelivery
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.run_orchestration.admission import RunAdmissionService
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


@pytest.mark.parametrize("timing", ["before_model", "during_final", "before_tool"])
def test_steer_reaches_typed_checkpoint_before_model_and_stops_stale_tool(tmp_path, timing):
    async def scenario():
        engine, sessions, task, run, desktop, inputs = await setup(tmp_path)
        saver = InMemorySaver()
        received, calls, tools = [], [], []
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
                if timing != "before_model" and len(calls) == 1:
                    await submit()
                else:
                    async with sessions() as session:
                        row = await session.get(ConversationInput, received[0]["input_id"])
                        assert row.status == "delivered" and row.checkpoint_id
                return await super()._agenerate(messages, stop, run_manager, **kwargs)
        answers = [AIMessage(content="done")]
        if timing == "before_tool":
            answers.insert(0, AIMessage(content="", tool_calls=[{"id": "old-tool", "name": "modify_database", "args": {}}]))
        graph = create_agent(Model(messages=iter(answers) if timing == "before_tool" else cycle(answers)),
            [modify_database], middleware=[ConversationInputDelivery(sessions, saver),
                WorldStateMiddleware([], "base", frozenset(), "test", "v1"), TypedHistoryMiddleware()],
            state_schema=LeadAgentState, checkpointer=saver, context_schema=dict)
        security = SecurityContext(AuthorizationIdentity(tmp_path, (tmp_path,), ("read",), AccessMode.READ_ONLY, "main"),
            RoutingIdentity(task, task, f"main:{task}", task, "", run.run_id))
        try:
            if timing == "before_model":
                await submit()
            result = await graph.ainvoke({"messages": [HumanMessage(content="initial")]},
                {"configurable": {"thread_id": task}}, context=security.to_runtime_context(), durability="sync")
            assert len(calls) == (1 if timing == "before_model" else 2)
            assert any(message.content == "只修前端" for message in calls[-1])
            assert len([message for message in result["messages"] if message.id == received[0]["input_id"]]) == 1
            assert tools == []
            assert any(item["origin"] == "direct_user" and item["message_id"] == received[0]["input_id"]
                       for item in result["execution_items"])
        finally:
            await engine.dispose()
    asyncio.run(scenario())
