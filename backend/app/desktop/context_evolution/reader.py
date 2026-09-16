r"""本文件对外提供当前、历史及多来源 Context revision 的统一只读端口。

输入为唯一 `ContextRevisionRef`、读取视图、可选分页边界和调用方 AsyncSession；输出为 authored、
execution、display、display page、checkpoint、historical、frontier-summary 或 deleted-source 投影。
具体工作流为从不可变 repository 解析 revision，按 payload mode 加载精确 checkpoint；分页读取只
序列化命中页，完整读取保持原稳定映射，且不追随最新 checkpoint。示例：`await reader.read(session, ref, "display")`。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.context_evolution.repository import (
    ContextRevisionIdentityMismatch,
    ContextRevisionNotFound,
    ContextRevisionRepository,
)
from backend.app.desktop.context_evolution.schemas import (
    ContextFrontierSummary,
    ContextRevisionCheckpointView,
    ContextRevisionContract,
    ContextRevisionHistoricalView,
    ContextRevisionMessageView,
    ContextRevisionMessageViewKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionRef,
    DeletedContextSource,
    DeletedContextSourceView,
)
from backend.app.desktop.models import DesktopThread
from focus.runtime.runs.events import serialize_message


ContextRevisionViewKind = Literal[
    "authored",
    "execution",
    "display",
    "checkpoint",
    "historical",
    "frontier-summary",
    "deleted-source",
]
ContextRevisionReadResult = (
    ContextRevisionMessageView
    | ContextRevisionCheckpointView
    | ContextRevisionHistoricalView
    | ContextFrontierSummary
    | DeletedContextSourceView
)


@dataclass(frozen=True)
class ContextRevisionMessagePage:
    ref: ContextRevisionRef
    messages: tuple[dict[str, Any], ...]
    total: int
    start: int
    end: int


class ContextRevisionReader:
    def __init__(self, repository: ContextRevisionRepository, checkpointer: Any) -> None:
        self._repository = repository
        self._checkpointer = checkpointer

    async def read_current(
        self,
        session: AsyncSession,
        context_id: str,
        view: ContextRevisionViewKind,
    ) -> ContextRevisionReadResult | None:
        current = await self._repository.current(session, context_id)
        if current is None:
            return None
        return await self._read_contract(session, current, view)

    async def read(
        self,
        session: AsyncSession,
        ref: ContextRevisionRef,
        view: ContextRevisionViewKind,
    ) -> ContextRevisionReadResult:
        revision = await self._repository.get(session, ref)
        return await self._read_contract(session, revision, view)

    async def read_message_page(
        self,
        revision: ContextRevisionContract,
        *,
        before: int | None,
        limit: int,
    ) -> ContextRevisionMessagePage:
        runtime = await self._runtime_message_objects(revision.ref)
        if revision.ref.payload_mode is ContextRevisionPayloadMode.CHECKPOINT:
            total = len(runtime)
            end = total if before is None else min(max(before, 0), total)
            start = max(0, end - limit)
            messages = tuple(serialize_message(message) for message in runtime[start:end])
            return ContextRevisionMessagePage(revision.ref, messages, total, start, end)

        authored = revision.authored_messages
        initial_ids = set(revision.initial_message_ids)
        runtime_total = sum(
            1 for message in runtime if self._message_id(message) not in initial_ids
        )
        total = len(authored) + runtime_total
        end = total if before is None else min(max(before, 0), total)
        start = max(0, end - limit)
        selected: list[dict[str, Any]] = []
        authored_end = min(end, len(authored))
        if start < authored_end:
            selected.extend(deepcopy(authored[start:authored_end]))
        suffix_start = max(0, start - len(authored))
        suffix_end = max(0, end - len(authored))
        if suffix_start < suffix_end:
            suffix_index = 0
            for message in runtime:
                if self._message_id(message) in initial_ids:
                    continue
                if suffix_index >= suffix_end:
                    break
                if suffix_index >= suffix_start:
                    selected.append(serialize_message(message))
                suffix_index += 1
        return ContextRevisionMessagePage(
            revision.ref,
            tuple(selected),
            total,
            start,
            end,
        )

    async def _read_contract(
        self,
        session: AsyncSession,
        revision: ContextRevisionContract,
        view: ContextRevisionViewKind,
    ) -> ContextRevisionReadResult:
        if view in {"authored", "execution", "display"}:
            return await self._messages(revision, view)
        if view == "checkpoint":
            checkpoint = await self._checkpoint(revision.ref)
            if checkpoint is None:
                raise ContextRevisionNotFound(
                    f"revision 尚无可读取 checkpoint: {revision.ref.revision_id}"
                )
            return checkpoint
        if view == "historical":
            return await self._historical(revision)
        if view == "frontier-summary":
            return await self._frontier_summary(revision)
        if view == "deleted-source":
            return await self._deleted_sources(session, revision)
        raise ValueError(f"未知 Context revision view: {view}")

    async def _messages(
        self,
        revision: ContextRevisionContract,
        view: ContextRevisionMessageViewKind,
    ) -> ContextRevisionMessageView:
        runtime = await self._runtime_messages(revision.ref)
        if revision.ref.payload_mode is ContextRevisionPayloadMode.CHECKPOINT:
            messages = runtime
        elif view == "authored":
            messages = list(deepcopy(revision.authored_messages))
        elif view == "execution":
            messages = runtime or list(deepcopy(revision.execution_messages))
        else:
            messages = self._definition_display(revision, runtime)
        return ContextRevisionMessageView(ref=revision.ref, view=view, messages=tuple(messages))

    async def _historical(
        self,
        revision: ContextRevisionContract,
    ) -> ContextRevisionHistoricalView:
        return ContextRevisionHistoricalView(
            revision=revision,
            authored=await self._messages(revision, "authored"),
            execution=await self._messages(revision, "execution"),
            display=await self._messages(revision, "display"),
            checkpoint=await self._checkpoint(revision.ref),
        )

    async def _frontier_summary(
        self,
        revision: ContextRevisionContract,
    ) -> ContextFrontierSummary:
        display = await self._messages(revision, "display")
        return ContextFrontierSummary(
            ref=revision.ref,
            projection_status=revision.projection_status,
            content_hash=revision.content_hash,
            summary=self._short_summary(display.messages),
            message_count=len(display.messages),
            source_frontier=tuple(source.source for source in revision.sources),
            deleted=(
                revision.deleted_at is not None
                or revision.projection_status is ContextRevisionProjectionStatus.DELETED
            ),
        )

    async def _deleted_sources(
        self,
        session: AsyncSession,
        revision: ContextRevisionContract,
    ) -> DeletedContextSourceView:
        sources: list[DeletedContextSource] = []
        for edge in revision.sources:
            source = await self._repository.get(session, edge.source)
            source_identity = (
                await session.get(DesktopThread, edge.source.context_id)
                if session is not None
                else None
            )
            identity_deleted = (
                source_identity is None
                if session is not None
                else False
            ) or (
                source_identity is not None and source_identity.deleted_at is not None
            )
            deleted = (
                identity_deleted
                or source.deleted_at is not None
                or source.projection_status is ContextRevisionProjectionStatus.DELETED
            )
            sources.append(
                DeletedContextSource(
                    position=edge.position,
                    source=edge.source,
                    deleted=deleted,
                    deleted_at=(
                        source.deleted_at
                        or (source_identity.deleted_at if source_identity is not None else None)
                    ),
                )
            )
        return DeletedContextSourceView(target=revision.ref, sources=tuple(sources))

    async def _checkpoint(
        self,
        ref: ContextRevisionRef,
    ) -> ContextRevisionCheckpointView | None:
        checkpoint = await self._checkpoint_tuple(ref)
        if checkpoint is None:
            return None
        values = checkpoint.checkpoint.get("channel_values", {})
        messages = tuple(serialize_message(message) for message in values.get("messages", []))
        return ContextRevisionCheckpointView(
            ref=ref,
            messages=messages,
            metadata=deepcopy(getattr(checkpoint, "metadata", {}) or {}),
        )

    async def _checkpoint_tuple(self, ref: ContextRevisionRef) -> Any | None:
        if not ref.is_runnable:
            return None
        checkpoint = await self._checkpointer.aget_tuple(ref.checkpoint_config())
        if checkpoint is None:
            raise ContextRevisionNotFound(
                f"revision checkpoint 不存在: {ref.revision_id}/{ref.checkpoint_id}"
            )
        actual_id = checkpoint.config.get("configurable", {}).get("checkpoint_id")
        if actual_id != ref.checkpoint_id:
            raise ContextRevisionIdentityMismatch(
                f"checkpoint 不属于 revision: expected={ref.checkpoint_id}, actual={actual_id}"
            )
        return checkpoint

    async def _runtime_message_objects(self, ref: ContextRevisionRef) -> tuple[Any, ...]:
        checkpoint = await self._checkpoint_tuple(ref)
        if checkpoint is None:
            return ()
        values = checkpoint.checkpoint.get("channel_values", {})
        return tuple(values.get("messages", ()))

    async def _runtime_messages(self, ref: ContextRevisionRef) -> list[dict[str, Any]]:
        messages = await self._runtime_message_objects(ref)
        return [serialize_message(message) for message in messages]

    @staticmethod
    def _message_id(message: Any) -> str | None:
        if isinstance(message, dict):
            value = message.get("id")
        else:
            value = getattr(message, "id", None)
        return str(value) if value is not None else None

    @staticmethod
    def _definition_display(
        revision: ContextRevisionContract,
        runtime: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        initial_ids = set(revision.initial_message_ids)
        suffix = [message for message in runtime if message.get("id") not in initial_ids]
        return [*deepcopy(revision.authored_messages), *suffix]

    @staticmethod
    def _short_summary(messages: tuple[dict[str, Any], ...]) -> str:
        for message in reversed(messages):
            content = message.get("content", "")
            if isinstance(content, str) and content.strip():
                return content.strip()[:280]
            if isinstance(content, list):
                text = " ".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and isinstance(block.get("text"), str)
                ).strip()
                if text:
                    return text[:280]
        return ""
