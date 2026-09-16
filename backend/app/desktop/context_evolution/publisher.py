r"""本文件对外提供单 Context definition revision 的准备、验证与原子发布端口。

输入为 authored definition、版本化来源、可选真实运行后缀、期望基础 revision、checkpoint writer
与调用方 AsyncSession；输出为不可路由 shadow candidate 或已发布 revision 引用。具体工作流为
确定性编译 authored messages，待审批候选停在无 checkpoint 状态，合法候选把 execution 前缀和后缀
写入 shadow identity，最后在嵌套事务中执行 immutable insert 与 current pointer CAS；失败不会留下
可运行 revision。
示例：`candidate = await publisher.prepare(session, request); await publisher.publish(session, candidate)`。
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.context_evolution.checkpoint_writer import ContextCheckpointWriter
from backend.app.desktop.context_evolution.repository import (
    ContextRevisionRepository,
    StaleContextRevision,
)
from backend.app.desktop.context_evolution.schemas import (
    ContextRevisionContract,
    ContextRevisionPayloadMode,
    ContextRevisionPrepareRequest,
    ContextRevisionProjectionStatus,
    ContextRevisionPublicationResult,
    ContextRevisionRef,
    PreparedContextRevision,
)
from backend.app.desktop.context_projection import compile_context_messages


class ContextRevisionPreparationFailed(RuntimeError):
    pass


class ContextRevisionPublicationBlocked(RuntimeError):
    pass


class ContextRevisionPublisher:
    def __init__(
        self,
        repository: ContextRevisionRepository,
        checkpoint_writer: ContextCheckpointWriter,
    ) -> None:
        self._repository = repository
        self._checkpoint_writer = checkpoint_writer

    async def prepare(
        self,
        session: AsyncSession,
        request: ContextRevisionPrepareRequest,
        *,
        suffix_messages: tuple[dict[str, object], ...] = (),
    ) -> PreparedContextRevision:
        current = await self._repository.current(session, request.context_id)
        current_ref = current.ref if current is not None else None
        if current_ref != request.expected_base:
            raise StaleContextRevision(
                f"prepare base 已变化: expected={self._id(request.expected_base)}, "
                f"actual={self._id(current_ref)}"
            )
        projection = compile_context_messages(list(request.authored_messages))
        revision_id = uuid.uuid4().hex
        shadow_ref = ContextRevisionRef(
            context_id=request.context_id,
            revision_id=revision_id,
            generation=(current_ref.generation + 1 if current_ref is not None else 1),
            execution_thread_id=f"shadow:{request.context_id}:{revision_id}",
            checkpoint_ns="context-revision-shadow",
            checkpoint_id=None,
            payload_mode=ContextRevisionPayloadMode.DEFINITION,
        )
        status = ContextRevisionProjectionStatus(projection.status)
        ref = shadow_ref
        initial_ids: tuple[str, ...] = ()
        if status in {
            ContextRevisionProjectionStatus.VALID,
            ContextRevisionProjectionStatus.REPAIRED,
        }:
            try:
                if suffix_messages:
                    checkpoint_id, initial_ids = await self._checkpoint_writer.write(
                        shadow_ref,
                        tuple(projection.execution_messages),
                        suffix_messages,
                    )
                else:
                    checkpoint_id, initial_ids = await self._checkpoint_writer.write(
                        shadow_ref,
                        tuple(projection.execution_messages),
                    )
            except Exception as exc:
                raise ContextRevisionPreparationFailed(
                    f"shadow checkpoint 准备失败: {revision_id}"
                ) from exc
            ref = ContextRevisionRef.model_validate(
                {**shadow_ref.model_dump(mode="python"), "checkpoint_id": checkpoint_id}
            )
        revision = ContextRevisionContract(
            ref=ref,
            sources=request.sources,
            authored_messages=tuple(projection.authored_messages),
            execution_messages=tuple(projection.execution_messages),
            repair_manifest=tuple(projection.repair_manifest),
            issues=tuple(projection.issues),
            initial_message_ids=initial_ids,
            definition_hash=projection.definition_hash,
            projection_hash=projection.projection_hash,
            content_hash=self._content_hash(request, projection.projection_hash),
            projection_status=status,
            origin_kind=request.origin_kind,
            origin_id=request.origin_id,
            created_at=datetime.now(UTC),
        )
        return PreparedContextRevision(expected_base=current_ref, revision=revision)

    async def publish(
        self,
        session: AsyncSession,
        candidate: PreparedContextRevision,
    ) -> ContextRevisionPublicationResult:
        if not candidate.publishable:
            raise ContextRevisionPublicationBlocked(
                f"candidate 不可发布: {candidate.revision.projection_status.value}"
            )
        async with session.begin_nested():
            current = await self._repository.current(session, candidate.revision.ref.context_id)
            current_ref = current.ref if current is not None else None
            if current_ref != candidate.expected_base:
                raise StaleContextRevision(
                    f"publish base 已变化: expected={self._id(candidate.expected_base)}, "
                    f"actual={self._id(current_ref)}"
                )
            inserted = await self._repository.insert(session, candidate.revision)
            await self._repository.switch_current(
                session,
                inserted.ref,
                candidate.expected_base,
            )
        return ContextRevisionPublicationResult(
            previous=candidate.expected_base,
            published=candidate.revision.ref,
        )

    @staticmethod
    def _content_hash(request: ContextRevisionPrepareRequest, projection_hash: str) -> str:
        payload = {
            "context_id": request.context_id,
            "projection_hash": projection_hash,
            "sources": [source.model_dump(mode="json") for source in request.sources],
            "origin_kind": request.origin_kind.value,
            "origin_id": request.origin_id,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _id(ref: ContextRevisionRef | None) -> str | None:
        return ref.revision_id if ref is not None else None
