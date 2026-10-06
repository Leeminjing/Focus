"""本文件对外提供 ModelAttemptJournal 与 ModelAttemptMiddleware 的耐久模型尝试审计。

输入为精确 prepared checkpoint、Run 来源和完整模型结果；输出为独立审计、终态及可复用 canonical 结果。
具体工作流为验证 checkpoint 来源后开立尝试并原子预留共享预算，失败保存 partial audit，完成保存结果并结算 receipt；
每次采样复核控制版本，未知用量保留预留，恢复不重复已完成采样。
示例：ModelAttemptMiddleware(ModelAttemptJournal(sessions, checkpointer))。
"""

import asyncio
from datetime import datetime, timezone
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware import ModelResponse
from langgraph.config import get_config
from langgraph.errors import GraphInterrupt
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from backend.app.desktop.models import ModelAttemptAudit
from focus.security.context import security_context_of
from focus.history import deserialize_history_messages, serialize_history_message
from backend.app.desktop.execution_attempts.usage_receipts import ModelUsageReceipts
from focus.messages import estimate_messages_tokens


class ModelAttemptJournal:
    def __init__(self, sessions, checkpointer):
        self._sessions = sessions
        self._checkpointer = checkpointer

    async def begin(self, routing, checkpoint_id, manifest, *, input_tokens=0, output_tokens=0):
        if not checkpoint_id or not manifest or manifest.get("status") != "prepared":
            raise RuntimeError("模型审计需要精确 prepared checkpoint")
        checkpoint = await self._checkpointer.aget_tuple({"configurable": {
            "thread_id": routing.thread_id, "checkpoint_ns": routing.checkpoint_ns, "checkpoint_id": checkpoint_id}})
        if checkpoint is None or checkpoint.config["configurable"].get("checkpoint_id") != checkpoint_id or checkpoint.checkpoint.get("channel_values", {}).get("request_manifest") != manifest:
            raise RuntimeError("模型审计来源未耐久提交")
        async with self._sessions.begin() as session:
            await session.execute(insert(ModelAttemptAudit).values(attempt_id=manifest["attempt_id"], run_id=routing.run_id,
                execution_thread_id=routing.thread_id, checkpoint_ns=routing.checkpoint_ns, checkpoint_id=checkpoint_id,
                source_manifest=manifest, status="prepared").on_conflict_do_nothing())
            row = await session.get(ModelAttemptAudit, manifest["attempt_id"], with_for_update=True)
            if row.source_manifest != manifest or row.checkpoint_id != checkpoint_id:
                raise RuntimeError("模型 attempt identity 来源冲突")
            if row.status == "completed":
                return deserialize_history_messages(row.audit["completed_results"])
            if row.status != "prepared":
                raise RuntimeError("终态模型尝试不能复用；需重新准备 execution checkpoint")
            await ModelUsageReceipts().reserve(session, row, input_tokens=input_tokens, output_tokens=output_tokens)
        return None

    async def settle(self, identity, status, audit, usage):
        async with self._sessions() as session:
            row = await session.scalar(select(ModelAttemptAudit).where(ModelAttemptAudit.attempt_id == identity).with_for_update())
            if row is None:
                raise RuntimeError("模型尝试缺少 prepared 审计来源")
            if row.status != "prepared":
                if (row.status, row.audit, row.usage) != (status, audit, usage):
                    raise RuntimeError("模型尝试终态冲突")
                return
            row.status, row.audit, row.usage = status, audit, usage
            row.settled_at = datetime.now(timezone.utc)
            await ModelUsageReceipts().settle(session, row)
            await session.commit()


class ModelAttemptMiddleware(AgentMiddleware):
    def __init__(self, journal):
        self._journal = journal

    async def awrap_model_call(self, request, handler):
        routing = security_context_of(request.runtime.context).routing
        manifest = request.state.get("request_manifest")
        checkpoint_id = get_config()["configurable"].get("checkpoint_map", {}).get("")
        completed = await self._journal.begin(routing, checkpoint_id, manifest, input_tokens=estimate_messages_tokens(request.messages),
                                              output_tokens=int(getattr(request.model, "max_tokens", None) or 0))
        if completed is not None:
            return ModelResponse(result=completed)
        try:
            response = await handler(request)
        except BaseException as exc:
            status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "interrupted" if isinstance(exc, GraphInterrupt) else getattr(exc, "status", "failed")
            audit = getattr(exc, "audit", {"error_type": type(exc).__name__})
            await asyncio.shield(self._journal.settle(manifest["attempt_id"], status, audit, audit.get("usage")))
            raise
        metadata = response.result[-1].response_metadata if response.result else {}
        audit = {"response_message_ids": [message.id for message in response.result],
                 "provider": metadata.get("provider"), "protocol": metadata.get("protocol"), "status": metadata.get("status"),
                 "request_source_manifest": metadata.get("request_source_manifest"),
                 "completed_results": [serialize_history_message(message) for message in response.result]}
        usage = metadata.get("usage") or (getattr(response.result[-1], "usage_metadata", None) if response.result else None)
        await self._journal.settle(manifest["attempt_id"], "completed", audit, usage)
        return response
