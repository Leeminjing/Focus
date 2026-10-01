"""本文件对外提供 AgentInbox 与 DurableInboxMiddleware 的耐久协作消息投递。

输入为受治理 recipient/task/Run 身份和精确 checkpoint；输出为稳定 collaboration Messages 与唯一投递 receipt。
具体工作流为只读获取、append 消息、等待 sync checkpoint，再校验该 checkpoint 的 typed 来源后同事务记 receipt/已读。
恢复会确认已保存未确认的消息；rollback 缺少消息时重新投递原始正文，正文不重复追加且不提升用户授权。
示例：middleware = DurableInboxMiddleware(AgentInbox(session_factory, checkpointer))。
"""

from datetime import datetime, timezone
import json

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.config import get_config
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from backend.app.desktop.models import AgentMessage, AgentMessageDelivery
from focus.history import content_hash
from focus.security.context import security_context_of


class AgentInbox:
    def __init__(self, sessions, checkpointer):
        self._sessions = sessions
        self._checkpointer = checkpointer

    async def prepare(self, routing, retained: set[str]):
        known = [identity.removeprefix("inbox:") for identity in retained if identity.startswith("inbox:")]
        async with self._sessions() as session:
            base = select(AgentMessage).where(AgentMessage.to_agent == routing.agent_id, AgentMessage.task_id == routing.task_id)
            rows = (await session.scalars(base.where(AgentMessage.message_id.not_in(known))
                                         .order_by(AgentMessage.created_at, AgentMessage.message_id).limit(100))).all()
            pending = (await session.scalars(base.where(AgentMessage.message_id.in_(known), AgentMessage.read_at.is_(None)))).all()
        updates = [self._message(row) for row in rows]
        return updates, list(dict.fromkeys(row.message_id for row in [*pending, *rows]))

    async def confirm(self, routing, checkpoint_id: str, message_ids: list[str]):
        if not message_ids:
            return
        config = {"configurable": {"thread_id": routing.thread_id, "checkpoint_ns": routing.checkpoint_ns,
                                   "checkpoint_id": checkpoint_id}}
        checkpoint = await self._checkpointer.aget_tuple(config)
        if checkpoint is None or checkpoint.config["configurable"].get("checkpoint_id") != checkpoint_id:
            raise RuntimeError("inbox 投递 checkpoint 身份不可验证")
        items = checkpoint.checkpoint.get("channel_values", {}).get("execution_items", ())
        delivered = {ref["message_id"] for item in items if item["kind"] == "agent_collaboration"
                     and item["origin"] == "collaborator" for ref in item["source_refs"]
                     if ref.get("kind") == "inbox" and ref.get("recipient") == routing.agent_id
                     and ref.get("task_id") == routing.task_id}
        if not set(message_ids).issubset(delivered):
            raise RuntimeError("inbox 消息尚未耐久写入 typed checkpoint")
        async with self._sessions() as session:
            rows = (await session.scalars(select(AgentMessage).where(
                AgentMessage.message_id.in_(message_ids), AgentMessage.task_id == routing.task_id,
                AgentMessage.to_agent == routing.agent_id).with_for_update())).all()
            if {row.message_id for row in rows} != set(message_ids):
                raise RuntimeError("inbox 来源不属于当前 recipient/task")
            for row in rows:
                record = {"message_id": row.message_id, "execution_thread_id": routing.thread_id,
                          "checkpoint_ns": routing.checkpoint_ns, "checkpoint_id": checkpoint_id,
                          "run_id": routing.run_id}
                await session.execute(insert(AgentMessageDelivery).values(
                    delivery_id=content_hash(record), **record).on_conflict_do_nothing())
            await session.execute(update(AgentMessage).where(AgentMessage.message_id.in_(message_ids))
                                  .values(read_at=datetime.now(timezone.utc)))
            await session.commit()

    @staticmethod
    def _message(row):
        content = json.dumps({"author": row.from_agent, "recipient": row.to_agent, "kind": row.kind,
                              "created_at": row.created_at.isoformat(), "content": row.content}, ensure_ascii=False)
        return HumanMessage(id=f"inbox:{row.message_id}", content=f"Agent collaboration:\n{content}", additional_kwargs={
            "focus_context": {"kind": "agent_collaboration", "origin": "collaborator", "scope": "execution",
                              "source_refs": [{"kind": "inbox", "message_id": row.message_id,
                                               "author": row.from_agent, "recipient": row.to_agent, "task_id": row.task_id}]}})


class DurableInboxMiddleware(AgentMiddleware):
    def __init__(self, inbox: AgentInbox):
        self._inbox = inbox

    async def abefore_model(self, state, runtime):
        routing = security_context_of(runtime.context).routing
        updates, pending = await self._inbox.prepare(routing, {message.id for message in state["messages"] if message.id})
        return {"messages": updates, "inbox_delivery": {"message_ids": pending}}

    async def awrap_model_call(self, request, handler):
        routing = security_context_of(request.runtime.context).routing
        pending = request.state.get("inbox_delivery", {}).get("message_ids", [])
        checkpoint_id = get_config()["configurable"].get("checkpoint_map", {}).get("")
        if pending and not checkpoint_id:
            raise RuntimeError("inbox 确认需要模型节点的精确 parent checkpoint")
        await self._inbox.confirm(routing, checkpoint_id, pending)
        return await handler(request)
