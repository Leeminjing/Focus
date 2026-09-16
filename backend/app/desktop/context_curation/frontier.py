r"""本文件对外提供有界 ContextFrontierManifest、展开句柄与 ContextFrontierBuilder。

输入为 Curation Program 的来源订阅、活动 Lane、当前 Context revision 和最近 Run；输出为只含
purpose、revision、freshness、state、fingerprint、短摘要、source frontier、最近结果及只读句柄的
有界清单。具体工作流为合并重复 Context membership、比较上一 Portfolio frontier、拒绝超预算清单，
仅在显式 expand 时读取精确 revision 内容。示例：`manifest = await builder.build(session, program_id)`。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.context_curation.models import (
    CurationLane,
    CurationLaneLifecycle,
    CurationProgram,
    CurationSourceSubscription,
    PortfolioRevision,
)
from backend.app.desktop.context_evolution import (
    ContextRevisionReadResult,
    ContextRevisionReader,
    ContextRevisionRef,
    ContextRevisionRepository,
    ContextRevisionViewKind,
)
from backend.app.desktop.models import DesktopRun, DesktopThread


class ContextFrontierError(RuntimeError):
    pass


class ContextFrontierBudgetExceeded(ContextFrontierError):
    pass


class ContextExpansionDenied(ContextFrontierError):
    pass


class ContextFrontierFreshness(StrEnum):
    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    UNMATERIALIZED = "unmaterialized"


class ContextFrontierState(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"
    UNMATERIALIZED = "unmaterialized"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContextExpansionHandle(_FrozenModel):
    handle_id: str = Field(min_length=64, max_length=64)
    program_id: str
    context_id: str
    revision: ContextRevisionRef
    fingerprint: str = Field(min_length=64, max_length=64)
    allowed_views: tuple[ContextRevisionViewKind, ...]


class ContextRecentRunResult(_FrozenModel):
    run_id: str
    status: str
    error: str | None = None
    updated_at: datetime


class ContextFrontierEntry(_FrozenModel):
    context_id: str
    title: str
    purposes: tuple[str, ...]
    membership_ids: tuple[str, ...]
    revision: ContextRevisionRef | None
    freshness: ContextFrontierFreshness
    state: ContextFrontierState
    projection_status: str | None
    fingerprint: str | None
    summary: str = Field(max_length=280)
    source_frontier: tuple[ContextRevisionRef, ...]
    recent_result: ContextRecentRunResult | None
    blocked: bool
    expansion: ContextExpansionHandle | None


class ContextFrontierManifest(_FrozenModel):
    program_id: str
    program_revision: int = Field(ge=0)
    portfolio_revision_id: str | None
    frontier_hash: str = Field(min_length=64, max_length=64)
    entries: tuple[ContextFrontierEntry, ...]
    generated_at: datetime


class ContextFrontierBuilder:
    _allowed_views: tuple[ContextRevisionViewKind, ...] = (
        "authored",
        "execution",
        "display",
        "checkpoint",
        "historical",
        "deleted-source",
    )

    def __init__(
        self,
        repository: ContextRevisionRepository,
        reader: ContextRevisionReader,
    ) -> None:
        self._repository = repository
        self._reader = reader

    async def build(
        self,
        session: AsyncSession,
        program_id: str,
        *,
        max_entries: int = 64,
        max_chars: int = 32_000,
    ) -> ContextFrontierManifest:
        program = await session.get(CurationProgram, program_id)
        if program is None:
            raise ContextFrontierError(f"Curation Program 不存在: {program_id}")
        memberships = await self._memberships(session, program_id)
        if len(memberships) > max_entries:
            raise ContextFrontierBudgetExceeded(
                f"Context Frontier 条目数 {len(memberships)} 超过预算 {max_entries}"
            )
        previous = await self._previous_frontier(session, program)
        recent_runs = await self._recent_runs(session, set(memberships))
        entry_list: list[ContextFrontierEntry] = []
        for context_id, membership in memberships.items():
            entry_list.append(
                await self._entry(
                    session,
                    program_id,
                    context_id,
                    membership,
                    previous,
                    recent_runs.get(context_id),
                )
            )
        entries = tuple(entry_list)
        frontier_hash = self._hash([entry.model_dump(mode="json") for entry in entries])
        manifest = ContextFrontierManifest(
            program_id=program_id,
            program_revision=program.revision,
            portfolio_revision_id=program.current_portfolio_revision_id,
            frontier_hash=frontier_hash,
            entries=entries,
            generated_at=datetime.now().astimezone(),
        )
        self._require_char_budget(manifest.model_dump(mode="json"), max_chars, "manifest")
        return manifest

    async def expand(
        self,
        session: AsyncSession,
        handle: ContextExpansionHandle,
        view: ContextRevisionViewKind,
        *,
        max_messages: int = 500,
        max_chars: int = 80_000,
    ) -> ContextRevisionReadResult:
        if view not in handle.allowed_views:
            raise ContextExpansionDenied(f"展开视图未授权: {view}")
        await self._require_membership(session, handle.program_id, handle.context_id)
        revision = await self._repository.get(session, handle.revision)
        expected = self._handle_id(
            handle.program_id,
            handle.revision,
            revision.content_hash,
        )
        if expected != handle.handle_id or revision.content_hash != handle.fingerprint:
            raise ContextExpansionDenied("Context 展开句柄已被篡改")
        result = await self._reader.read(session, handle.revision, view)
        payload = result.model_dump(mode="json")
        message_count = self._message_count(payload)
        if message_count > max_messages:
            raise ContextFrontierBudgetExceeded(
                f"Context 展开消息数 {message_count} 超过预算 {max_messages}"
            )
        self._require_char_budget(payload, max_chars, "expansion")
        return result

    async def _memberships(
        self,
        session: AsyncSession,
        program_id: str,
    ) -> dict[str, dict[str, list[str]]]:
        subscriptions = list(
            (
                await session.scalars(
                    select(CurationSourceSubscription)
                    .where(CurationSourceSubscription.program_id == program_id)
                    .order_by(CurationSourceSubscription.position)
                )
            ).all()
        )
        lanes = list(
            (
                await session.scalars(
                    select(CurationLane)
                    .where(
                        CurationLane.program_id == program_id,
                        CurationLane.lifecycle != CurationLaneLifecycle.RETIRED.value,
                        CurationLane.managed_context_id.is_not(None),
                    )
                    .order_by(CurationLane.created_at, CurationLane.lane_id)
                )
            ).all()
        )
        result: dict[str, dict[str, list[str]]] = {}
        for subscription in subscriptions:
            item = result.setdefault(
                subscription.source_context_id,
                {"purposes": [], "membership_ids": []},
            )
            item["purposes"].append(subscription.source_role)
            item["membership_ids"].append(subscription.subscription_id)
        for lane in lanes:
            item = result.setdefault(
                lane.managed_context_id,
                {"purposes": [], "membership_ids": []},
            )
            item["purposes"].append(lane.purpose)
            item["membership_ids"].append(lane.lane_id)
        return result

    async def _entry(
        self,
        session: AsyncSession,
        program_id: str,
        context_id: str,
        membership: dict[str, list[str]],
        previous: dict[str, str],
        recent_run: DesktopRun | None,
    ) -> ContextFrontierEntry:
        identity = await session.get(DesktopThread, context_id)
        current = await self._repository.current(session, context_id)
        if identity is None or current is None:
            return ContextFrontierEntry(
                context_id=context_id,
                title=identity.title if identity is not None else context_id,
                purposes=tuple(membership["purposes"]),
                membership_ids=tuple(membership["membership_ids"]),
                revision=None,
                freshness=ContextFrontierFreshness.UNMATERIALIZED,
                state=ContextFrontierState.UNMATERIALIZED,
                projection_status=None,
                fingerprint=None,
                summary="",
                source_frontier=(),
                recent_result=self._run_result(recent_run),
                blocked=True,
                expansion=None,
            )
        summary = await self._reader.read(session, current.ref, "frontier-summary")
        state = self._state(identity)
        fingerprint = current.content_hash
        return ContextFrontierEntry(
            context_id=context_id,
            title=identity.title,
            purposes=tuple(membership["purposes"]),
            membership_ids=tuple(membership["membership_ids"]),
            revision=current.ref,
            freshness=self._freshness(previous.get(context_id), current.ref.revision_id),
            state=state,
            projection_status=current.projection_status.value,
            fingerprint=fingerprint,
            summary=summary.summary,
            source_frontier=summary.source_frontier,
            recent_result=self._run_result(recent_run),
            blocked=state is not ContextFrontierState.ACTIVE or not current.ref.is_runnable,
            expansion=ContextExpansionHandle(
                handle_id=self._handle_id(program_id, current.ref, fingerprint),
                program_id=program_id,
                context_id=context_id,
                revision=current.ref,
                fingerprint=fingerprint,
                allowed_views=self._allowed_views,
            ),
        )

    async def _previous_frontier(
        self,
        session: AsyncSession,
        program: CurationProgram,
    ) -> dict[str, str]:
        if program.current_portfolio_revision_id is None:
            return {}
        portfolio = await session.get(
            PortfolioRevision,
            program.current_portfolio_revision_id,
        )
        if portfolio is None:
            raise ContextFrontierError("Program current Portfolio revision 不存在")
        return {
            item["context_id"]: item["revision_id"]
            for item in portfolio.source_frontier
            if isinstance(item, dict)
            and isinstance(item.get("context_id"), str)
            and isinstance(item.get("revision_id"), str)
        }

    async def _recent_runs(
        self,
        session: AsyncSession,
        context_ids: set[str],
    ) -> dict[str, DesktopRun]:
        if not context_ids:
            return {}
        runs = list(
            (
                await session.scalars(
                    select(DesktopRun)
                    .where(DesktopRun.task_id.in_(context_ids))
                    .order_by(DesktopRun.task_id, DesktopRun.updated_at.desc(), DesktopRun.run_id)
                )
            ).all()
        )
        result: dict[str, DesktopRun] = {}
        for run in runs:
            result.setdefault(run.task_id, run)
        return result

    async def _require_membership(
        self,
        session: AsyncSession,
        program_id: str,
        context_id: str,
    ) -> None:
        source = await session.scalar(
            select(CurationSourceSubscription.subscription_id).where(
                CurationSourceSubscription.program_id == program_id,
                CurationSourceSubscription.source_context_id == context_id,
            )
        )
        lane = await session.scalar(
            select(CurationLane.lane_id).where(
                CurationLane.program_id == program_id,
                CurationLane.managed_context_id == context_id,
                CurationLane.lifecycle != CurationLaneLifecycle.RETIRED.value,
            )
        )
        if source is None and lane is None:
            raise ContextExpansionDenied("Context 不属于当前 Curation Program")

    @staticmethod
    def _state(identity: DesktopThread) -> ContextFrontierState:
        if identity.deleted_at is not None:
            return ContextFrontierState.DELETED
        if identity.archived_at is not None:
            return ContextFrontierState.ARCHIVED
        return ContextFrontierState.ACTIVE

    @staticmethod
    def _freshness(previous_revision_id: str | None, revision_id: str) -> ContextFrontierFreshness:
        if previous_revision_id is None:
            return ContextFrontierFreshness.NEW
        if previous_revision_id == revision_id:
            return ContextFrontierFreshness.UNCHANGED
        return ContextFrontierFreshness.CHANGED

    @staticmethod
    def _run_result(run: DesktopRun | None) -> ContextRecentRunResult | None:
        if run is None:
            return None
        return ContextRecentRunResult(
            run_id=run.run_id,
            status=run.status,
            error=run.error,
            updated_at=run.updated_at,
        )

    @classmethod
    def _handle_id(
        cls,
        program_id: str,
        ref: ContextRevisionRef,
        fingerprint: str,
    ) -> str:
        return cls._hash(
            {
                "program_id": program_id,
                "revision": ref.model_dump(mode="json"),
                "fingerprint": fingerprint,
            }
        )

    @staticmethod
    def _message_count(value: Any) -> int:
        if isinstance(value, dict):
            return sum(
                len(item)
                if key == "messages" and isinstance(item, list)
                else ContextFrontierBuilder._message_count(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return sum(ContextFrontierBuilder._message_count(item) for item in value)
        return 0

    @classmethod
    def _require_char_budget(cls, value: Any, max_chars: int, label: str) -> None:
        size = len(cls._json(value))
        if size > max_chars:
            raise ContextFrontierBudgetExceeded(
                f"Context {label} 字符数 {size} 超过预算 {max_chars}"
            )

    @classmethod
    def _hash(cls, value: Any) -> str:
        return hashlib.sha256(cls._json(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
