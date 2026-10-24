"""CompressionService 的业务实现与显式依赖端口。"""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import HTTPException
from focus.agents.compression import apply_compression_ranges, hit_keyword_message_ids
from focus.agents.compression.schemas import validate_apply_decision
from focus.agents.lead import make_lead_agent
from focus.runtime.checkpointer.namespaced import NamespacedCheckpointer
from focus.runtime.runs.events import deserialize_messages
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.contexts.evolution import (
    ContextRevisionOriginKind,
    ContextRevisionRepository,
)
from backend.app.desktop.persistence.models import (
    DesktopRun,
    DesktopThread,
    DesktopWorkspace,
)


class CompressionServicePorts(Protocol):
    async def _get_task_entities(
        self, session: AsyncSession, task_id: str
    ) -> tuple[DesktopThread, DesktopWorkspace]: ...

    app_config: Any
    checkpointer: Any
    context_patrol: Any
    contexts: Any

    async def get_checkpoint_messages(
        self, thread_id: str, checkpoint_ns: str
    ) -> list[dict[str, Any]]: ...

    session_factory: Any


class CompressionService:
    def __init__(self, ports: CompressionServicePorts) -> None:
        self._ports = ports

    async def _compression_execution_identity(
        self, session: AsyncSession, task: DesktopThread
    ) -> tuple[str, str]:
        ports = self._ports
        "让压缩快照、预览与应用使用同一 current Revision 的线程和命名空间。"
        current_revision = (
            await ContextRevisionRepository().current(session, task.task_id)
            if hasattr(ports, "contexts")
            else None
        )
        if current_revision is None:
            return (task.thread_id, "")
        return (
            current_revision.ref.execution_thread_id,
            current_revision.ref.checkpoint_ns,
        )

    async def get_compression_messages(self, task_id: str) -> list[dict[str, Any]]:
        ports = self._ports
        "读取当前 Revision 执行位置的完整消息快照，保留压缩块来源元数据。"
        async with ports.session_factory() as session:
            task, _ = await ports._get_task_entities(session, task_id)
            (
                execution_thread_id,
                execution_checkpoint_ns,
            ) = await self._compression_execution_identity(session, task)
        return await ports.get_checkpoint_messages(
            execution_thread_id, execution_checkpoint_ns
        )

    async def quick_compression_preview(
        self, task_id: str, keyword: str, case_sensitive: bool = False
    ) -> dict[str, Any]:
        ports = self._ports
        async with ports.session_factory() as session:
            task, _ = await ports._get_task_entities(session, task_id)
            await ports.contexts.ensure_runnable(session, task_id)
            (
                execution_thread_id,
                execution_checkpoint_ns,
            ) = await self._compression_execution_identity(session, task)
        messages = await ports.get_checkpoint_messages(
            execution_thread_id, execution_checkpoint_ns
        )
        hit_ids = hit_keyword_message_ids(messages, keyword, case_sensitive)
        hit_set = set(hit_ids)
        hit_messages = [message for message in messages if message.get("id") in hit_set]
        return {"keyword": keyword, "hit_ids": hit_ids, "hit_messages": hit_messages}

    async def quick_compression_apply(
        self,
        task_id: str,
        ranges: list[dict[str, Any]],
        scrub_terms: list[str] | None = None,
    ) -> dict[str, Any]:
        ports = self._ports
        async with ports.session_factory() as session:
            task, _ = await ports._get_task_entities(session, task_id)
            (
                execution_thread_id,
                execution_checkpoint_ns,
            ) = await self._compression_execution_identity(session, task)
            active = await session.scalar(
                select(DesktopRun.run_id)
                .where(
                    DesktopRun.task_id == task.task_id,
                    DesktopRun.agent_id == f"main:{task.task_id}",
                    DesktopRun.status.in_(["pending", "running"]),
                )
                .limit(1)
            )
            if active:
                raise HTTPException(
                    409,
                    {
                        "code": "main_run_active",
                        "message": "主 Agent 正在运行，请先等待其结束或取消后再执行快捷压缩",
                    },
                )
        messages = await ports.get_checkpoint_messages(
            execution_thread_id, execution_checkpoint_ns
        )
        base_messages = deserialize_messages(messages)
        current_ids = {m.id for m in base_messages if m.id}
        missing = [
            sid
            for r in ranges
            for sid in r.get("source_ids") or []
            if sid not in current_ids
        ]
        if missing:
            raise HTTPException(
                409,
                {
                    "code": "context_changed",
                    "message": "上下文已更新（部分消息已被折叠进压缩块），请刷新压缩面板后重试",
                },
            )
        normalized, error = validate_apply_decision(
            {"type": "compression", "decision": "apply", "ranges": ranges},
            base_messages,
        )
        if error:
            raise HTTPException(422, f"快捷压缩范围非法: {error}")
        update = apply_compression_ranges(base_messages, normalized)
        config = {"configurable": {"thread_id": execution_thread_id}}
        graph = await make_lead_agent(
            tools=[], system_prompt="", middlewares=[], app_config=ports.app_config
        )
        graph.checkpointer = (
            NamespacedCheckpointer(ports.checkpointer, execution_checkpoint_ns)
            if execution_checkpoint_ns
            else ports.checkpointer
        )
        updated_config = await graph.aupdate_state(config, update)
        if hasattr(ports, "contexts"):
            checkpoint_id = (
                updated_config.get("configurable", {}).get("checkpoint_id")
                if isinstance(updated_config, dict)
                else None
            )
            if not checkpoint_id:
                raise RuntimeError("快捷压缩写入后未返回 checkpoint_id")
            revision_origin = (
                ContextRevisionOriginKind.COMPRESSION_RESTORE
                if any((item.get("restore") for item in normalized))
                else ContextRevisionOriginKind.COMPRESSION
            )
            await ports.contexts.evolution.publish_checkpoint(
                task_id, checkpoint_id, revision_origin, origin_id=checkpoint_id
            )
        await ports.context_patrol.notify_stable_context_checkpoint(task_id)
        return {
            "messages": await ports.get_checkpoint_messages(
                execution_thread_id, execution_checkpoint_ns
            )
        }
