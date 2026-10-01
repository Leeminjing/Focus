"""本文件对外提供以下合同的验证冻结选择、Run 作用域投影与数据库 inbox 投递的耐久边界。

输入为真实数据库消息/receipt、sync graph checkpoint 与注入失败点；输出为无提前确认、无重复正文及分支重新投递断言。
具体工作流为冻结内容、准备 checkpoint、注入 ack 故障后恢复，并确认 rollback 的精确 delivery 引用。
示例：pytest backend/tests/test_scoped_context_inbox.py。
"""

import asyncio
from dataclasses import replace
from itertools import cycle
import os
import uuid

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.context_assembly import desktop_contexts
from backend.app.desktop.inbox import AgentInbox, DurableInboxMiddleware
from backend.app.desktop.models import AgentMessage, AgentMessageDelivery, DesktopThread, DesktopWorkspace
from focus.agents.lead_agent_state import LeadAgentState
from focus.context.middleware import WorldStateMiddleware
from focus.context.scoped import FrozenContext, scoped_messages
from focus.history import messages_to_items, semantic_messages
from focus.history.middleware import TypedHistoryMiddleware
from focus.security.context import AuthorizationIdentity, RoutingIdentity, SecurityContext
from focus.security.policy import AccessMode


def _security(path, task, run="run"):
    return SecurityContext(AuthorizationIdentity(path, (path,), ("read",), AccessMode.READ_ONLY, "main"),
                           RoutingIdentity(task, "workspace", "main", task, "", run))


def test_selected_context_scope_and_frozen_hash(tmp_path):
    async def run():
        context = FrozenContext("memory", "version one", ({"memory_id": "m"},))
        frozen = context.record()
        assert desktop_contexts({"memory_snapshots": [frozen]}, "")[0].content == "version one"
        with pytest.raises(ValueError, match="哈希"):
            FrozenContext.from_record({**frozen, "content": "version two"})
        saver = InMemorySaver()
        observed = []
        class Model(GenericFakeChatModel):
            async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
                observed.append(messages)
                return await super()._agenerate(messages, stop, run_manager, **kwargs)
        graph = create_agent(Model(messages=cycle([AIMessage(content="answer")])), [], middleware=[
            WorldStateMiddleware([], "base", frozenset(), "test", "v1", frozen_contexts=(context,)),
            TypedHistoryMiddleware()], state_schema=LeadAgentState, checkpointer=saver, context_schema=dict)
        security = _security(tmp_path, "context")
        config = {"configurable": {"thread_id": "context"}}
        first = await graph.ainvoke({"messages": [HumanMessage(content="task")]}, config,
                                   context=security.to_runtime_context(), durability="sync")
        second = await graph.ainvoke({"messages": [HumanMessage(content="continue")]}, config,
                                    context=replace(security, routing=replace(security.routing, run_id="next")).to_runtime_context(),
                                    durability="sync")
        assert len([m for m in second["messages"] if m.content == "version one"]) == 2
        assert len([m for m in observed[-1] if m.content == "version one"]) == 1
        visible = scoped_messages(second["messages"], "next")
        assert all(not m.id.startswith("context:run:") for m in visible)
        references = [m for m in semantic_messages(messages_to_items(second["messages"])) if m.get("semantic_policy") == "reference_only"]
        assert len(references) == 1
        assert first["request_manifest"]["status"] == "sampled"
    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
@pytest.mark.parametrize("failure", ["checkpoint", "before_ack", "after_ack", "none"])
def test_inbox_durable_ack_recovery_and_rollback(tmp_path, failure):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        task = uuid.uuid4().hex
        message_id = uuid.uuid4().hex
        async with sessions() as session:
            session.add(DesktopWorkspace(workspace_id=task, path=str(tmp_path), display_name="inbox"))
            await session.flush()
            session.add(DesktopThread(task_id=task, thread_id=task, workspace_id=task, title="inbox"))
            await session.flush()
            session.add(AgentMessage(message_id=message_id, task_id=task, from_agent="teammate", to_agent="main",
                                     kind="message", content="work result"))
            await session.commit()
        injected = []
        model_calls = []
        class Saver(InMemorySaver):
            async def aput(self, config, checkpoint, metadata, new_versions):
                if failure == "checkpoint" and not injected and checkpoint["channel_values"].get("request_manifest"):
                    injected.append(True)
                    raise RuntimeError("injected checkpoint")
                return await super().aput(config, checkpoint, metadata, new_versions)
        saver = Saver()
        class Inbox(AgentInbox):
            async def confirm(self, routing, checkpoint_id, identities):
                if identities and failure == "before_ack" and not injected:
                    injected.append(True)
                    raise RuntimeError("injected before_ack")
                await super().confirm(routing, checkpoint_id, identities)
                if identities and failure == "after_ack" and not injected:
                    injected.append(True)
                    raise RuntimeError("injected after_ack")
        inbox = Inbox(sessions, saver)
        class Model(GenericFakeChatModel):
            async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
                async with sessions() as session:
                    row = await session.get(AgentMessage, message_id)
                    assert row.read_at is not None
                model_calls.append(messages)
                return await super()._agenerate(messages, stop, run_manager, **kwargs)
        graph = create_agent(Model(messages=cycle([AIMessage(content="answer")])), [], middleware=[
            DurableInboxMiddleware(inbox), WorldStateMiddleware([], "base", frozenset(), "test", "v1"), TypedHistoryMiddleware()],
            state_schema=LeadAgentState, checkpointer=saver, context_schema=dict)
        security = _security(tmp_path, task)
        config = {"configurable": {"thread_id": task}}
        try:
            if failure != "none":
                with pytest.raises(RuntimeError, match="injected"):
                    await graph.ainvoke({"messages": [HumanMessage(content="task")]}, config,
                                        context=security.to_runtime_context(), durability="sync")
                assert model_calls == []
                async with sessions() as session:
                    row = await session.get(AgentMessage, message_id)
                    assert (row.read_at is not None) == (failure == "after_ack")
                state = await graph.ainvoke(None, config, context=security.to_runtime_context(), durability="sync")
            else:
                state = await graph.ainvoke({"messages": [HumanMessage(content="task")]}, config,
                                           context=security.to_runtime_context(), durability="sync")
            assert len([m for m in state["messages"] if m.id == f"inbox:{message_id}"]) == 1
            async with sessions() as session:
                receipts = (await session.scalars(select(AgentMessageDelivery).where(AgentMessageDelivery.message_id == message_id))).all()
                assert receipts
                receipt = receipts[0]
            await asyncio.gather(*(inbox.confirm(security.routing, receipt.checkpoint_id, [message_id]) for _ in range(2)))
            async with sessions() as session:
                count = len((await session.scalars(select(AgentMessageDelivery).where(AgentMessageDelivery.message_id == message_id))).all())
                assert count == len(receipts)
            updates, pending = await inbox.prepare(security.routing, set())
            assert [m.id for m in updates] == [f"inbox:{message_id}"] and pending == [message_id]
            updates, pending = await inbox.prepare(security.routing, {f"inbox:{message_id}"})
            assert updates == [] and pending == []
        finally:
            async with sessions() as session:
                await session.execute(delete(DesktopThread).where(DesktopThread.task_id == task))
                await session.execute(delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == task))
                await session.commit()
            await engine.dispose()
    asyncio.run(run())
