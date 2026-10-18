"""将普通会话补充注入当前 Run，并在精确 typed checkpoint 提交后确认交付。

模型前追加稳定消息；模型返回最终答案时复查新补充；工具开始前若有未消费补充，
闭合原调用并让模型先读补充。已经开始的工具在原执行边界结束后接收要求。
"""

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.config import get_config
from sqlalchemy import select

from backend.app.desktop.conversation_inputs import ConversationInput
from backend.app.desktop.message_reference import project_message_reference
from focus.security.context import security_context_of


class ConversationInputDelivery(AgentMiddleware):
    def __init__(self, sessions, checkpointer):
        self.sessions = sessions
        self.checkpointer = checkpointer

    async def pending(self, routing):
        async with self.sessions() as session:
            return (await session.scalars(select(ConversationInput).where(
                ConversationInput.task_id == routing.task_id, ConversationInput.target_run_id == routing.run_id,
                ConversationInput.mode == "steer", ConversationInput.status == "accepted").order_by(
                ConversationInput.created_at, ConversationInput.input_id))).all()

    async def abefore_model(self, state, runtime):
        routing = security_context_of(runtime.context).routing
        pending = await self.pending(routing)
        if not pending:
            return None
        known = {message.id for message in state["messages"]}
        records = [{"role": "human", "id": row.input_id, "content": row.payload["message"]}
                   for row in pending if row.input_id not in known]
        references = {row.input_id: row.payload.get("message_reference") for row in pending}
        for record in records:
            record["additional_kwargs"] = {"focus_context": {"kind": "message", "origin": "direct_user",
                "scope": "execution", "authority": "task", "source_refs": [{"kind": "conversation_input",
                    "message_id": record["id"], "task_id": routing.task_id, "run_id": routing.run_id}]}}
        records = [project_message_reference(record, references[record["id"]]) for record in records]
        return {"messages": [HumanMessage(id=record["id"], content=record["content"],
                                          additional_kwargs=record["additional_kwargs"]) for record in records]}

    async def awrap_model_call(self, request, handler):
        routing = security_context_of(request.runtime.context).routing
        pending = await self.pending(routing)
        known = {message.id for message in request.state["messages"]}
        identities = {row.input_id for row in pending if row.input_id in known}
        if identities:
            checkpoint_id = get_config()["configurable"].get("checkpoint_map", {}).get("")
            checkpoint = await self.checkpointer.aget_tuple({"configurable": {"thread_id": routing.thread_id,
                "checkpoint_ns": routing.checkpoint_ns, "checkpoint_id": checkpoint_id}}) if checkpoint_id else None
            if checkpoint is None or checkpoint.config["configurable"].get("checkpoint_id") != checkpoint_id:
                raise RuntimeError("补充交付需要精确 checkpoint")
            proven = {item.get("message_id") for item in checkpoint.checkpoint.get("channel_values", {}).get("execution_items", [])
                      if item.get("kind") == "message" and item.get("origin") == "direct_user"
                      and any(ref.get("kind") == "conversation_input" and ref.get("run_id") == routing.run_id
                              and ref.get("task_id") == routing.task_id for ref in item.get("source_refs", []))}
            if not identities.issubset(proven):
                raise RuntimeError("补充尚未耐久写入当前轮上下文")
            async with self.sessions.begin() as session:
                rows = (await session.scalars(select(ConversationInput).where(
                    ConversationInput.input_id.in_(identities), ConversationInput.status == "accepted").with_for_update())).all()
                for row in rows:
                    row.status, row.checkpoint_id = "delivered", checkpoint_id
        return await handler(request)

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state, runtime):
        routing = security_context_of(runtime.context).routing
        if not getattr(state["messages"][-1], "tool_calls", None) and await self.pending(routing):
            return {"jump_to": "model"}
        return None

    async def awrap_tool_call(self, request, handler):
        routing = security_context_of(request.runtime.context).routing
        if await self.pending(routing):
            return ToolMessage(content="用户提交了当前轮补充。此工具尚未执行，请先阅读补充后重新决定操作。",
                               tool_call_id=request.tool_call["id"], name=request.tool_call["name"], status="error")
        return await handler(request)
