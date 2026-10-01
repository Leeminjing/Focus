"""本文件对外提供以下合同的验证真实 PostgreSQL checkpoint、模型 partial 审计和工具不确定执行恢复。

输入为独立测试数据库、SDK SSE fixture 与执行结果保存失败点；输出为精确 audit 来源及副作用不重复断言。
具体工作流为保存 prepared checkpoint、采样断流，再恢复已 claim 未保存结果的工具，确认不能盲目重试。
示例：pytest backend/tests/test_execution_attempt_audit.py。
"""

import asyncio
from dataclasses import replace
import json
import os
import selectors
import uuid

import httpx
import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from itertools import cycle
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.execution_attempts import ModelAttemptJournal, ModelAttemptMiddleware, ToolExecutionLedger
from backend.app.desktop.inbox import AgentInbox, DurableInboxMiddleware
from backend.app.desktop.models import AgentMessage, AgentMessageDelivery, DesktopRun, DesktopThread, DesktopWorkspace, ModelAttemptAudit
from focus.agents.lead_agent_state import LeadAgentState
from focus.context.middleware import WorldStateMiddleware
from focus.history.middleware import TypedHistoryMiddleware
from focus.models.provider_contract import ProviderContract
from focus.models.response_output import ResponseAttemptError
from focus.models.responses import FocusResponsesChatModel
from focus.runtime.tool_attempts import ToolExecutionMiddleware, ToolExecutionUncertain
from focus.security.context import AuthorizationIdentity, RoutingIdentity, SecurityContext
from focus.security.effects import NO_LOCAL_EFFECT, declare_effect
from focus.security.policy import AccessMode

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


class _ToolModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _run(coroutine):
    return asyncio.run(coroutine, loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))


async def _seed(sessions, path):
    identity = uuid.uuid4().hex
    async with sessions() as session:
        session.add(DesktopWorkspace(workspace_id=identity, path=str(path), display_name="attempt"))
        await session.flush()
        session.add(DesktopThread(task_id=identity, thread_id=identity, workspace_id=identity, title="attempt"))
        await session.flush()
        session.add(DesktopRun(run_id=identity, task_id=identity, agent_id="main", kind="main", status="running", input_messages=[]))
        await session.commit()
    security = SecurityContext(AuthorizationIdentity(path, (path,), ("read",), AccessMode.READ_ONLY, "main"),
                               RoutingIdentity(identity, identity, "main", identity, "", identity))
    return identity, security


async def _clean(sessions, identity):
    async with sessions() as session:
        await session.execute(delete(DesktopThread).where(DesktopThread.task_id == identity))
        await session.execute(delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == identity))
        await session.commit()


def test_real_checkpoint_inbox_receipt_and_partial_model_audit(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        identity, security = await _seed(sessions, tmp_path)
        async with sessions() as session:
            session.add(AgentMessage(message_id=identity, task_id=identity, to_agent="main", from_agent="teammate", content="evidence"))
            await session.commit()
        events = [
            {"type": "response.created", "sequence_number": 0, "response": {"id": "partial", "object": "response", "status": "in_progress", "output": []}},
            {"type": "response.output_item.added", "sequence_number": 1, "output_index": 0,
             "item": {"type": "message", "role": "assistant", "id": "m", "status": "in_progress", "content": []}},
            {"type": "response.output_text.delta", "sequence_number": 2, "output_index": 0, "content_index": 0, "item_id": "m", "delta": "partial text"},
        ]
        content = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content,
                                                                      headers={"content-type": "text/event-stream"})))
        model = FocusResponsesChatModel(model="fixture", api_key="fixture", base_url="https://fixture.test/v1",
                                        provider_contract=ProviderContract("deepseek", "responses"), http_async_client=client)
        connstring = make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql").render_as_string(hide_password=False)
        try:
            async with AsyncPostgresSaver.from_conn_string(connstring) as saver:
                await saver.setup()
                graph = create_agent(model, [], middleware=[DurableInboxMiddleware(AgentInbox(sessions, saver)),
                    WorldStateMiddleware([], "base", frozenset(), "fixture", "v1"), TypedHistoryMiddleware(),
                    ModelAttemptMiddleware(ModelAttemptJournal(sessions, saver))], state_schema=LeadAgentState, checkpointer=saver, context_schema=dict)
                config = {"configurable": {"thread_id": identity}}
                with pytest.raises(ResponseAttemptError, match="disconnected"):
                    async for _ in graph.astream({"messages": [HumanMessage(content="task")]}, config,
                                                context=security.to_runtime_context(), stream_mode=["messages", "values"], durability="sync"):
                        pass
                async with sessions() as session:
                    receipt = await session.scalar(select(AgentMessageDelivery).where(AgentMessageDelivery.message_id == identity))
                    audit = await session.scalar(select(ModelAttemptAudit).where(ModelAttemptAudit.run_id == identity))
                    assert receipt and audit and audit.status == "disconnected"
                    assert audit.usage is None and "partial text" in str(audit.audit)
                    checkpoint = await saver.aget_tuple({"configurable": {"thread_id": identity, "checkpoint_id": audit.checkpoint_id}})
                    assert checkpoint.checkpoint["channel_values"]["request_manifest"]["attempt_id"] == audit.attempt_id
                    assert all(item["kind"] != "reasoning" for item in checkpoint.checkpoint["channel_values"]["execution_items"])
                    assert not any("partial text" in str(item["payload"]) for item in checkpoint.checkpoint["channel_values"]["execution_items"])
                await saver.adelete_thread(identity)
        finally:
            await _clean(sessions, identity)
            await client.aclose()
            model._client.close()
            await engine.dispose()
    _run(run())


def test_uncertain_tool_result_blocks_recovery_and_completed_result_is_reusable(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        identity, security = await _seed(sessions, tmp_path)
        effects = []
        @tool
        def write_effect(key: str) -> str:
            """Perform one test effect and return evidence."""
            effects.append(key)
            return "done"
        declare_effect(write_effect, NO_LOCAL_EFFECT)
        class Ledger(ToolExecutionLedger):
            async def complete(self, identity, result):
                raise RuntimeError("result persistence failed")
        ledger = Ledger(sessions)
        context = replace(security, extras={"tool_execution_ledger": ledger}).to_runtime_context()
        model = _ToolModel(messages=cycle([AIMessage(content="", id="call-source", tool_calls=[
            {"id": "call", "name": "write_effect", "args": {"key": "x"}, "type": "tool_call"}])]))
        graph = create_agent(model, [write_effect], middleware=[ToolExecutionMiddleware(), TypedHistoryMiddleware()],
                             state_schema=LeadAgentState, checkpointer=InMemorySaver(), context_schema=dict)
        config = {"configurable": {"thread_id": identity}}
        try:
            with pytest.raises(RuntimeError, match="persistence"):
                await graph.ainvoke({"messages": [HumanMessage(content="task")]}, config, context=context, durability="sync")
            assert effects == ["x"]
            with pytest.raises(ToolExecutionUncertain, match="结果不确定"):
                await graph.ainvoke(None, config, context=context, durability="sync")
            assert effects == ["x"]
            normal = ToolExecutionLedger(sessions)
            call = {"id": "other", "name": "write_effect", "args": {"key": "y"}}
            assert await normal.claim("2" * 64, identity, call) is None
            result = ToolMessage(content="saved", tool_call_id="other", id="result")
            await normal.complete("2" * 64, result)
            assert (await normal.claim("2" * 64, identity, call)).model_dump() == result.model_dump()
        finally:
            await _clean(sessions, identity)
            await engine.dispose()
    _run(run())


def test_completed_attempt_recovery_reuses_canonical_result_without_resampling(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        identity, security = await _seed(sessions, tmp_path)
        requests = []
        def handle(request):
            requests.append(request.url.path)
            return httpx.Response(200, json={"object": "response", "id": "completed", "created_at": 0,
                "model": "fixture", "status": "completed", "output": [{"type": "message", "id": "m",
                "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": "saved", "annotations": []}]}]})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        model = FocusResponsesChatModel(model="fixture", api_key="fixture", base_url="https://fixture.test/v1",
            provider_contract=ProviderContract("deepseek", "responses"), http_async_client=client)
        crashed = []
        class Journal(ModelAttemptJournal):
            async def settle(self, identity, status, audit, usage):
                await super().settle(identity, status, audit, usage)
                if status == "completed" and not crashed:
                    crashed.append(True)
                    raise RuntimeError("completed saved before checkpoint")
        connstring = make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql").render_as_string(hide_password=False)
        try:
            async with AsyncPostgresSaver.from_conn_string(connstring) as saver:
                graph = create_agent(model, [], middleware=[WorldStateMiddleware([], "base", frozenset(), "fixture", "v1"),
                    TypedHistoryMiddleware(), ModelAttemptMiddleware(Journal(sessions, saver))],
                    state_schema=LeadAgentState, checkpointer=saver, context_schema=dict)
                config = {"configurable": {"thread_id": identity}}
                with pytest.raises(RuntimeError, match="completed saved"):
                    await graph.ainvoke({"messages": [HumanMessage(content="task")]}, config,
                        context=security.to_runtime_context(), durability="sync")
                state = await graph.ainvoke(None, config, context=security.to_runtime_context(), durability="sync")
                assert requests == ["/v1/responses"]
                assert state["messages"][-1].content == "saved"
                async with sessions() as session:
                    audit = await session.scalar(select(ModelAttemptAudit).where(ModelAttemptAudit.run_id == identity))
                    assert audit.status == "completed" and audit.usage is None
                    assert audit.audit["request_source_manifest"]["request_hash"]
                await saver.adelete_thread(identity)
        finally:
            await _clean(sessions, identity)
            await client.aclose()
            model._client.close()
            await engine.dispose()
    _run(run())
