r"""本文件对外提供 ContextEvolutionService，作为 Context revision 的权威应用端口。

输入为 Context identity、精确 checkpoint、authored definition、版本化来源、审批 hashes 与操作
origin；输出为不可变 current/historical revision、display view 或 tombstone。具体工作流为把外部
checkpoint 规范化成 revision，definition 先在 shadow identity 编译并写 checkpoint，合法候选原子
发布，待审批候选作为不可运行 current revision 留存，accept/reject 再产生新 revision；受管更新可从
旧 current revision 计算初始前缀之后的真实运行后缀并接入新 checkpoint，全程不读写 identity-level
definition/source 表。示例：`ref = await service.stage_definition(...)`。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
from typing import Any
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.context_evolution.checkpoint_writer import (
    LangGraphContextCheckpointWriter,
)
from backend.app.desktop.context_evolution.publisher import ContextRevisionPublisher
from backend.app.desktop.context_evolution.reader import ContextRevisionReader
from backend.app.desktop.context_evolution.repository import ContextRevisionRepository
from backend.app.desktop.context_evolution.schemas import (
    ContextRevisionContract,
    ContextRevisionMessageView,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionPrepareRequest,
    ContextRevisionProjectionStatus,
    ContextRevisionRef,
    ContextRevisionSourceContract,
)
from backend.app.desktop.models import DesktopThread
from focus.runtime.runs.events import serialize_message


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

    async def ensure_checkpoint_revision(
        self,
        session: AsyncSession,
        context_id: str,
        checkpoint_id: str,
        *,
        make_current_if_empty: bool = True,
    ) -> ContextRevisionRef:
        existing = await self._repository.find_by_checkpoint(
            session, context_id, checkpoint_id
        )
        if existing is not None:
            return existing.ref
        task = await session.get(DesktopThread, context_id)
        if task is None:
            raise ValueError(f"Context 不存在: {context_id}")
        await self._require_checkpoint(task.thread_id, "", checkpoint_id)
        current = await self._repository.current(session, context_id)
        ref = ContextRevisionRef(
            context_id=context_id,
            revision_id=uuid.uuid4().hex,
            generation=await self._repository.next_generation(session, context_id),
            execution_thread_id=task.thread_id,
            checkpoint_ns="",
            checkpoint_id=checkpoint_id,
            payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
        )
        contract = ContextRevisionContract(
            ref=ref,
            sources=(),
            content_hash=self._hash("checkpoint", context_id, checkpoint_id),
            projection_status=ContextRevisionProjectionStatus.VALID,
            origin_kind=ContextRevisionOriginKind.MIGRATION,
            origin_id=context_id,
            created_at=datetime.now(UTC),
        )
        await self._repository.insert(session, contract)
        if make_current_if_empty and current is None:
            await self._repository.switch_current(session, ref, None)
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
        if candidate.publishable:
            await self._publisher.publish(session, candidate)
        else:
            await self._repository.insert(session, candidate.revision)
            await self._repository.switch_current(
                session, candidate.revision.ref, candidate.expected_base
            )
        return candidate.revision

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

    async def decide_definition(
        self,
        session: AsyncSession,
        context_id: str,
        decision: str,
        definition_hash: str,
        projection_hash: str,
    ) -> ContextRevisionContract:
        current = await self._repository.current(session, context_id)
        if current is None:
            raise ValueError("Context 尚无 definition revision")
        if current.projection_status is not ContextRevisionProjectionStatus.APPROVAL_REQUIRED:
            raise ValueError("Context 当前不需要审批")
        if current.definition_hash != definition_hash or current.projection_hash != projection_hash:
            raise RuntimeError("Context 定义或投影已变化")
        if decision == "accept":
            return await self._approve(session, current)
        if decision == "reject":
            return await self._reject(session, current)
        raise ValueError("未知投影决定")

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
            contract = ContextRevisionContract(
                ref=ref,
                sources=sources,
                content_hash=self._hash("checkpoint", context_id, checkpoint_id),
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

    async def _approve(
        self, session: AsyncSession, current: ContextRevisionContract
    ) -> ContextRevisionContract:
        revision_id = uuid.uuid4().hex
        shadow = ContextRevisionRef(
            context_id=current.ref.context_id,
            revision_id=revision_id,
            generation=await self._repository.next_generation(session, current.ref.context_id),
            execution_thread_id=f"shadow:{current.ref.context_id}:{revision_id}",
            checkpoint_ns="context-revision-shadow",
            checkpoint_id=None,
            payload_mode=ContextRevisionPayloadMode.DEFINITION,
        )
        checkpoint_id, initial_ids = await self._writer.write(
            shadow, tuple(deepcopy(current.execution_messages))
        )
        ref = ContextRevisionRef.model_validate(
            {**shadow.model_dump(mode="python"), "checkpoint_id": checkpoint_id}
        )
        contract = self._decision_contract(
            current, ref, ContextRevisionProjectionStatus.APPROVED, initial_ids
        )
        await self._repository.insert(session, contract)
        await self._repository.switch_current(session, ref, current.ref)
        return contract

    async def _reject(
        self, session: AsyncSession, current: ContextRevisionContract
    ) -> ContextRevisionContract:
        ref = ContextRevisionRef(
            context_id=current.ref.context_id,
            revision_id=uuid.uuid4().hex,
            generation=await self._repository.next_generation(session, current.ref.context_id),
            execution_thread_id=current.ref.execution_thread_id,
            checkpoint_ns="context-revision-rejected",
            checkpoint_id=None,
            payload_mode=ContextRevisionPayloadMode.DEFINITION,
        )
        contract = self._decision_contract(
            current, ref, ContextRevisionProjectionStatus.REJECTED, ()
        )
        await self._repository.insert(session, contract)
        await self._repository.switch_current(session, ref, current.ref)
        return contract

    def _decision_contract(
        self,
        current: ContextRevisionContract,
        ref: ContextRevisionRef,
        status: ContextRevisionProjectionStatus,
        initial_ids: tuple[str, ...],
    ) -> ContextRevisionContract:
        return ContextRevisionContract(
            ref=ref,
            sources=current.sources,
            authored_messages=current.authored_messages,
            execution_messages=current.execution_messages,
            repair_manifest=current.repair_manifest,
            issues=current.issues,
            initial_message_ids=initial_ids,
            definition_hash=current.definition_hash,
            projection_hash=current.projection_hash,
            content_hash=self._hash("decision", current.content_hash, status.value),
            projection_status=status,
            origin_kind=ContextRevisionOriginKind.PROJECTION_DECISION,
            origin_id=current.ref.revision_id,
            created_at=datetime.now(UTC),
        )

    async def _definition_ancestor(
        self,
        session: AsyncSession,
        current: ContextRevisionContract,
    ) -> ContextRevisionContract | None:
        candidate = current
        visited: set[str] = set()
        while candidate.ref.revision_id not in visited:
            visited.add(candidate.ref.revision_id)
            if (
                candidate.ref.payload_mode is ContextRevisionPayloadMode.DEFINITION
                and candidate.initial_message_ids
            ):
                return candidate
            same_context = next(
                (
                    edge.source
                    for edge in candidate.sources
                    if edge.source.context_id == current.ref.context_id
                ),
                None,
            )
            if same_context is None:
                return None
            candidate = await self._repository.get(session, same_context)
        return None

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

    @staticmethod
    def _hash(*parts: Any) -> str:
        raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
