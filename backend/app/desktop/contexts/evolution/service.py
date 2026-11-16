"""本文件对外提供 ContextEvolutionService 与 ContextCheckpointBusy 应用端口。

输入为 Context 身份、checkpoint 或自动策展定义；输出为当前引用、精确读取或明确的忙碌/发布冲突。
具体工作流为自动策展与工作区初始化先准备合法候选，再原子发布；仅空闲 Main 的实际 HEAD 可导入，活动 Main 保有结算发布权。
旧输入只在 current 或任务仍持有快照时可读取，不重建永久历史档案。
示例：await service.ensure_checkpoint_revision(session, context_id, checkpoint_id)。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from focus.history import serialize_history_message as serialize_message
from focus.history.bridge import checkpoint_records
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.contexts.evolution.checkpoint_writer import (
    LangGraphContextCheckpointWriter,
)
from backend.app.desktop.contexts.evolution.history import (
    continued_revision_history,
)
from backend.app.desktop.contexts.evolution.publisher import ContextRevisionPublisher
from backend.app.desktop.contexts.evolution.reader import ContextRevisionReader
from backend.app.desktop.contexts.evolution.repository import ContextRevisionRepository
from backend.app.desktop.contexts.evolution.schemas import (
    ContextRevisionContract,
    ContextRevisionMessageView,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionPrepareRequest,
    ContextRevisionProjectionStatus,
    ContextRevisionRef,
    ContextRevisionSourceContract,
)
from backend.app.desktop.persistence.models import DesktopRun, DesktopThread


class ContextCheckpointBusy(RuntimeError):
    """A running Main owns publication of its execution checkpoints."""


class ContextEvolutionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        checkpointer: Any,
        graph_factory: Any,
    ) -> None:
        self._sessions = session_factory
        self._checkpointer = checkpointer
        self._repository = ContextRevisionRepository()
        self._reader = ContextRevisionReader(self._repository, checkpointer)
        self._writer = LangGraphContextCheckpointWriter(graph_factory, checkpointer)
        self._publisher = ContextRevisionPublisher(self._repository, self._writer)

    async def current(self, session: AsyncSession, context_id: str) -> ContextRevisionContract | None:
        return await self._repository.current(session, context_id)

    async def current_ref(self, context_id: str) -> ContextRevisionRef | None:
        async with self._sessions() as session:
            current = await self._repository.current(session, context_id)
            return current.ref if current is not None else None

    async def read_current_display(
        self, context_id: str
    ) -> ContextRevisionMessageView | None:
        async with self._sessions() as session:
            value = await self._reader.read_current(session, context_id, "display")
        if value is None:
            return None
        if not isinstance(value, ContextRevisionMessageView):
            raise TypeError("display reader 返回了错误视图")
        return value

    async def read_checkpoint_display(
        self,
        session: AsyncSession,
        context_id: str,
        checkpoint_id: str,
    ) -> ContextRevisionMessageView | None:
        revision = await self._repository.find_by_checkpoint(
            session, context_id, checkpoint_id
        )
        if revision is None:
            return None
        value = await self._reader.read(session, revision.ref, "display")
        if not isinstance(value, ContextRevisionMessageView):
            raise TypeError("display reader 返回了错误视图")
        return value

    async def read_checkpoint_semantic(
        self, session: AsyncSession, context_id: str, checkpoint_id: str,
    ) -> ContextRevisionMessageView | None:
        revision = await self._repository.find_by_checkpoint(session, context_id, checkpoint_id)
        if revision is None:
            return None
        value = await self._reader.read(session, revision.ref, "semantic")
        if not isinstance(value, ContextRevisionMessageView):
            raise TypeError("semantic reader 返回了错误视图")
        return value

    async def ensure_checkpoint_revision(
        self,
        session: AsyncSession,
        context_id: str,
        checkpoint_id: str,
        *,
        make_current_if_empty: bool = True,
    ) -> ContextRevisionRef:
        existing = await self._repository.find_by_checkpoint(session, context_id, checkpoint_id)
        if existing is not None:
            return (await self._repository.get_input(session, existing.ref)).ref
        task = await session.get(DesktopThread, context_id, with_for_update=True, populate_existing=True)
        if task is None:
            raise ValueError(f"Context 不存在: {context_id}")
        current = await self._repository.current(session, context_id)
        active = await session.scalar(select(DesktopRun.run_id).where(
            DesktopRun.task_id == context_id,
            DesktopRun.kind == "main",
            or_(DesktopRun.status.in_(("pending", "running")), DesktopRun.settled_at.is_(None)),
        ).limit(1))
        if active is not None:
            # Main admission pins its base under this same Context lock. Importing
            # its graph HEAD here would replace that base before finalization and
            # make the successful Run look superseded. Curation can use current
            # or an already-pinned stable input while Main owns new publication.
            raise ContextCheckpointBusy("Main is active; use its published stable Context input")
        thread_id = current.ref.execution_thread_id if current is not None else task.thread_id
        namespace = current.ref.checkpoint_ns if current is not None else ""
        head = await self._checkpointer.aget_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": namespace}})
        actual_head = head.config.get("configurable", {}).get("checkpoint_id") if head is not None else None
        if actual_head != checkpoint_id:
            raise ValueError("checkpoint is neither current execution head nor a published pinned input")
        if current is None and not make_current_if_empty:
            raise ValueError("unpublished checkpoint imports require explicit current-state publication")
        expected = current.ref if current is not None else None
        ref = ContextRevisionRef(context_id=context_id, revision_id=uuid.uuid4().hex,
            generation=expected.generation + 1 if expected else 1,
            execution_thread_id=thread_id, checkpoint_ns=namespace,
            checkpoint_id=checkpoint_id, payload_mode=ContextRevisionPayloadMode.CHECKPOINT)
        history = await self._checkpoint_history(thread_id, namespace, checkpoint_id, current)
        contract = ContextRevisionContract(ref=ref, sources=current.sources if current else (),
            history_payload=history, initial_message_ids=current.initial_message_ids if current else (),
            content_hash=self._hash("checkpoint", context_id, checkpoint_id, history.model_dump(mode="json")),
            projection_status=ContextRevisionProjectionStatus.VALID,
            origin_kind=ContextRevisionOriginKind.MIGRATION, origin_id=context_id,
            created_at=datetime.now(UTC))
        await self._repository.insert(session, contract)
        await self._repository.switch_current(session, ref, expected)
        return ref

    async def stage_definition(
        self,
        session: AsyncSession,
        context_id: str,
        authored_messages: tuple[dict[str, Any], ...],
        sources: tuple[ContextRevisionSourceContract, ...],
        origin_kind: ContextRevisionOriginKind,
        origin_id: str | None,
        suffix_messages: tuple[dict[str, Any], ...] = (),
    ) -> ContextRevisionContract:
        current = await self._repository.current(session, context_id)
        candidate = await self._publisher.prepare(
            session,
            ContextRevisionPrepareRequest(
                context_id=context_id,
                expected_base=current.ref if current is not None else None,
                sources=sources,
                authored_messages=authored_messages,
                origin_kind=origin_kind,
                origin_id=origin_id,
            ),
            suffix_messages=suffix_messages,
        )
        await self._publisher.publish(session, candidate)
        return await self._repository.get(session, candidate.revision.ref)

    async def continuation_messages(
        self,
        session: AsyncSession,
        current: ContextRevisionContract | None,
        fallback_thread_id: str,
    ) -> tuple[dict[str, Any], ...]:
        messages: list[dict[str, Any]] = []
        initial_ids: set[str] = set()
        if current is not None:
            ancestor = await self._definition_ancestor(session, current)
            initial_ids = set(ancestor.initial_message_ids if ancestor is not None else ())
            if current.ref.is_runnable:
                checkpoint = await self._checkpointer.aget_tuple(
                    current.ref.checkpoint_config()
                )
                if checkpoint is not None:
                    messages.extend(
                        serialize_message(message)
                        for message in checkpoint.checkpoint.get(
                            "channel_values", {}
                        ).get("messages", [])
                        if getattr(message, "id", None) not in initial_ids
                    )
        fallback = await self._checkpointer.aget_tuple(
            {
                "configurable": {
                    "thread_id": fallback_thread_id,
                    "checkpoint_ns": "",
                }
            }
        )
        known = {message.get("id") for message in messages if message.get("id")}
        if fallback is not None:
            for message in fallback.checkpoint.get("channel_values", {}).get(
                "messages", []
            ):
                serialized = serialize_message(message)
                message_id = serialized.get("id")
                if message_id in initial_ids or (message_id and message_id in known):
                    continue
                messages.append(serialized)
                if message_id:
                    known.add(message_id)
        return tuple(messages)

    async def publish_checkpoint(
        self,
        context_id: str,
        checkpoint_id: str,
        origin_kind: ContextRevisionOriginKind,
        origin_id: str | None = None,
    ) -> ContextRevisionRef:
        async with self._sessions.begin() as session:
            task = await session.get(DesktopThread, context_id)
            if task is None:
                raise ValueError(f"Context 不存在: {context_id}")
            current = await self._repository.current(session, context_id)
            if current is not None and current.ref.checkpoint_id == checkpoint_id:
                return current.ref
            thread_id = current.ref.execution_thread_id if current is not None else task.thread_id
            namespace = current.ref.checkpoint_ns if current is not None else ""
            await self._require_checkpoint(thread_id, namespace, checkpoint_id)
            expected = current.ref if current is not None else None
            ref = ContextRevisionRef(
                context_id=context_id,
                revision_id=uuid.uuid4().hex,
                generation=await self._repository.next_generation(session, context_id),
                execution_thread_id=thread_id,
                checkpoint_ns=namespace,
                checkpoint_id=checkpoint_id,
                payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
            )
            sources = (
                (ContextRevisionSourceContract(source=expected, position=0),)
                if expected is not None
                else ()
            )
            history = await self._checkpoint_history(thread_id, namespace, checkpoint_id, current)
            contract = ContextRevisionContract(
                ref=ref,
                sources=sources,
                history_payload=history,
                content_hash=self._hash("checkpoint", context_id, checkpoint_id, history.model_dump(mode="json")),
                projection_status=ContextRevisionProjectionStatus.VALID,
                origin_kind=origin_kind,
                origin_id=origin_id,
                created_at=datetime.now(UTC),
            )
            await self._repository.insert(session, contract)
            await self._repository.switch_current(session, ref, expected)
            return ref

    async def publish_latest_checkpoint(
        self,
        context_id: str,
        origin_kind: ContextRevisionOriginKind,
        origin_id: str | None = None,
    ) -> ContextRevisionRef | None:
        async with self._sessions() as session:
            task = await session.get(DesktopThread, context_id)
            if task is None:
                raise ValueError(f"Context 不存在: {context_id}")
            current = await self._repository.current(session, context_id)
            thread_id = current.ref.execution_thread_id if current is not None else task.thread_id
            namespace = current.ref.checkpoint_ns if current is not None else ""
        checkpoint = await self._checkpointer.aget_tuple(
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": namespace}}
        )
        checkpoint_id = (
            checkpoint.config.get("configurable", {}).get("checkpoint_id")
            if checkpoint is not None
            else None
        )
        if checkpoint_id is None:
            return None
        return await self.publish_checkpoint(
            context_id, checkpoint_id, origin_kind, origin_id
        )

    async def publish_tombstone(
        self,
        session: AsyncSession,
        context_id: str,
        origin_id: str | None = None,
    ) -> ContextRevisionRef:
        task = await session.get(DesktopThread, context_id)
        if task is None:
            raise ValueError(f"Context 不存在: {context_id}")
        current = await self._repository.current(session, context_id)
        expected = current.ref if current is not None else None
        now = datetime.now(UTC)
        ref = ContextRevisionRef(
            context_id=context_id,
            revision_id=uuid.uuid4().hex,
            generation=await self._repository.next_generation(session, context_id),
            execution_thread_id=task.thread_id,
            checkpoint_ns="context-revision-tombstone",
            checkpoint_id=None,
            payload_mode=ContextRevisionPayloadMode.DEFINITION,
        )
        contract = ContextRevisionContract(
            ref=ref,
            sources=(
                (ContextRevisionSourceContract(source=expected, position=0),)
                if expected is not None and expected.is_runnable
                else ()
            ),
            content_hash=self._hash("deleted", context_id, now.isoformat()),
            projection_status=ContextRevisionProjectionStatus.DELETED,
            origin_kind=ContextRevisionOriginKind.MIGRATION,
            origin_id=origin_id or context_id,
            created_at=now,
            deleted_at=now,
        )
        await self._repository.insert(session, contract)
        await self._repository.switch_current(session, ref, expected)
        return ref

    async def _definition_ancestor(
        self,
        session: AsyncSession,
        current: ContextRevisionContract,
    ) -> ContextRevisionContract | None:
        # Initial definition identities are carried by the current state itself.
        return current if current.initial_message_ids else None

    async def _require_checkpoint(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> None:
        checkpoint = await self._checkpointer.aget_tuple(
            {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                }
            }
        )
        actual = (
            checkpoint.config.get("configurable", {}).get("checkpoint_id")
            if checkpoint is not None
            else None
        )
        if actual != checkpoint_id:
            raise ValueError(f"checkpoint 不属于 Context execution identity: {checkpoint_id}")

    async def _checkpoint_history(self, thread_id, namespace, checkpoint_id, current):
        checkpoint = await self._checkpointer.aget_tuple({"configurable": {
            "thread_id": thread_id, "checkpoint_ns": namespace, "checkpoint_id": checkpoint_id,
        }})
        records = checkpoint_records(checkpoint.checkpoint.get("channel_values", {}))
        return continued_revision_history(current.authored_messages if current is not None else (), records,
                                          previous=current.history_payload if current else None)

    @staticmethod
    def _hash(*parts: Any) -> str:
        raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
