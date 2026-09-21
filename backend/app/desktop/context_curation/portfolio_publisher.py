r"""本文件对外提供 PortfolioFreezer、PortfolioCandidatePreparer 与 AtomicPortfolioPublisher。

输入为当前 Program、精确 source revision frontier、Lane 意图、compiled candidates、可选稳定 Portfolio identity
和控制版本；输出为可复现冻结记录、隔离 shadow Context revisions 与完整发布结果。具体工作流为短事务
幂等冻结所有输入，逐 Lane 可重入地准备并持久化不可路由 revision，可为持续受管 Lane 保留当前 definition 之后的运行后缀，最后
按可选 authority/loop/round、workspace/program/portfolio/lane/context 稳定锁序重验 CAS，并为每次数据库重试初始化独立
authority attempt，再一次切换全部 Context/Portfolio 指针并写入幂等 outbox；任一失败保留旧 Portfolio。
示例：`published = await publisher.publish(portfolio_id, current_controls)`。
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from typing import Any, Literal, Protocol
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.context_curation.compiler import CompiledLaneCandidate
from backend.app.desktop.context_curation.models import (
    CurationLane,
    CurationProgram,
    PortfolioLaneAction,
    PortfolioLaneCandidate,
    PortfolioLaneCandidateStatus,
    PortfolioPublicationAttempt,
    PortfolioPublicationAttemptStatus,
    PortfolioRevision,
    PortfolioRevisionStatus,
)
from backend.app.desktop.context_curation.publication_outbox import CurationOutboxRepository
from backend.app.desktop.context_curation.repository import PortfolioRepository
from backend.app.desktop.context_evolution import (
    ContextRevisionOriginKind,
    ContextRevisionPrepareRequest,
    ContextRevisionPublisher,
    ContextRevisionRef,
    ContextRevisionRepository,
    ContextRevisionSourceContract,
)
from backend.app.desktop.models import DesktopThread, DesktopWorkspace


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PortfolioControlRevisions(_FrozenModel):
    workspace_revision: str = Field(min_length=1)
    program_revision: int = Field(ge=0)
    current_portfolio_revision_id: str | None = None
    loop_revision: int | None = Field(default=None, ge=0)
    grant_revision: int | None = Field(default=None, ge=0)


class PortfolioLaneIntent(_FrozenModel):
    lane_id: str = Field(min_length=1)
    action: PortfolioLaneAction
    purpose: str = Field(min_length=1)
    source_allocation: tuple[ContextRevisionRef, ...] = ()
    semantic_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)


class PortfolioFreezeRequest(_FrozenModel):
    program_id: str = Field(min_length=1)
    source_frontier: tuple[ContextRevisionRef, ...]
    lane_intents: tuple[PortfolioLaneIntent, ...]
    workspace_revision: str = Field(min_length=1)
    loop_revision: int | None = Field(default=None, ge=0)
    grant_revision: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _unique_inputs(self) -> "PortfolioFreezeRequest":
        if len({item.context_id for item in self.source_frontier}) != len(
            self.source_frontier
        ):
            raise ValueError("source frontier 不得重复 Context")
        if len({item.lane_id for item in self.lane_intents}) != len(self.lane_intents):
            raise ValueError("Portfolio 不得重复目标 Lane")
        return self


class FrozenPortfolio(_FrozenModel):
    portfolio_revision_id: str
    program_id: str
    generation: int
    source_frontier: tuple[ContextRevisionRef, ...]
    controls: PortfolioControlRevisions
    candidate_ids: tuple[str, ...]


class PortfolioPublicationResult(_FrozenModel):
    portfolio_revision_id: str
    program_id: str
    generation: int
    context_revisions: tuple[ContextRevisionRef, ...]
    event_id: str
    idempotent: bool = False


class PublishedPortfolioLane(_FrozenModel):
    lane_id: str
    action: PortfolioLaneAction
    purpose: str
    context_revision: ContextRevisionRef | None


class PublishedPortfolioView(_FrozenModel):
    portfolio_revision_id: str
    program_id: str
    generation: int
    lanes: tuple[PublishedPortfolioLane, ...]


class PortfolioPreparationError(RuntimeError):
    pass


class PortfolioPublicationError(RuntimeError):
    pass


class PortfolioSuperseded(PortfolioPublicationError):
    pass


class PortfolioAuthorityCommitHook(Protocol):
    def begin_attempt(self) -> None: ...

    async def lock_authority(self, session: AsyncSession) -> None: ...

    async def commit(
        self,
        session: AsyncSession,
        portfolio: PortfolioRevision,
        candidates: tuple[PortfolioLaneCandidate, ...],
        revisions: tuple[ContextRevisionRef, ...],
        controls: PortfolioControlRevisions,
    ) -> None: ...


class PortfolioCurrentReader:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        context_revisions: ContextRevisionRepository,
    ) -> None:
        self._sessions = session_factory
        self._contexts = context_revisions

    async def current(self, program_id: str) -> PublishedPortfolioView | None:
        async with self._sessions() as session:
            current_id = await session.scalar(
                select(CurationProgram.current_portfolio_revision_id).where(
                    CurationProgram.program_id == program_id
                )
            )
            if current_id is None:
                return None
            portfolio = await session.get(PortfolioRevision, current_id)
            candidates = list(
                (
                    await session.scalars(
                        select(PortfolioLaneCandidate)
                        .where(
                            PortfolioLaneCandidate.portfolio_revision_id == current_id
                        )
                        .order_by(PortfolioLaneCandidate.lane_id)
                    )
                ).all()
            )
            lanes = []
            for candidate in candidates:
                ref = None
                if candidate.candidate_context_revision_id is not None:
                    ref = (
                        await self._contexts.get_by_id(
                            session, candidate.candidate_context_revision_id
                        )
                    ).ref
                lanes.append(
                    PublishedPortfolioLane(
                        lane_id=candidate.lane_id,
                        action=PortfolioLaneAction(candidate.action),
                        purpose=candidate.purpose,
                        context_revision=ref,
                    )
                )
            return PublishedPortfolioView(
                portfolio_revision_id=portfolio.portfolio_revision_id,
                program_id=portfolio.program_id,
                generation=portfolio.generation,
                lanes=tuple(lanes),
            )


class PortfolioFreezer:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        context_revisions: ContextRevisionRepository,
        portfolios: PortfolioRepository | None = None,
    ) -> None:
        self._sessions = session_factory
        self._contexts = context_revisions
        self._portfolios = portfolios or PortfolioRepository()

    async def freeze(
        self,
        request: PortfolioFreezeRequest,
        *,
        portfolio_revision_id: str | None = None,
    ) -> FrozenPortfolio:
        async with self._sessions.begin() as session:
            program = await self._lock_program(session, request.program_id)
            if portfolio_revision_id is not None:
                existing = await session.get(
                    PortfolioRevision,
                    portfolio_revision_id,
                    with_for_update=True,
                )
                if existing is not None:
                    self._verify_existing_freeze(existing, request)
                    return await self._frozen(session, existing)
            await self._verify_frontier(session, request.source_frontier, program.workspace_id)
            lanes = await self._lock_lanes(session, request)
            generation = await self._next_generation(session, program.program_id)
            portfolio_id = portfolio_revision_id or uuid.uuid4().hex
            controls = self._controls(request, program)
            targets = await self._targets(
                session,
                request,
                program,
                lanes,
                portfolio_id,
            )
            portfolio = await self._portfolios.create_revision(
                session,
                program.program_id,
                generation,
                [item.model_dump(mode="json") for item in request.source_frontier],
                self._hash_refs(request.source_frontier),
                program.revision,
                status=PortfolioRevisionStatus.PREPARING,
                control_revisions=controls.model_dump(mode="json"),
                target_lanes=targets,
                portfolio_revision_id=portfolio_id,
            )
            candidate_ids = await self._create_candidates(
                session,
                portfolio.portfolio_revision_id,
                request.lane_intents,
                targets,
            )
            await self._portfolios.add_publication_attempt(
                session,
                portfolio_id,
                1,
                PortfolioPublicationAttemptStatus.PREPARING,
                "frozen",
                evidence={
                    "source_frontier": portfolio.source_frontier,
                    "controls": portfolio.control_revisions,
                    "target_lanes": portfolio.target_lanes,
                },
            )
            return FrozenPortfolio(
                portfolio_revision_id=portfolio_id,
                program_id=program.program_id,
                generation=generation,
                source_frontier=request.source_frontier,
                controls=controls,
                candidate_ids=tuple(candidate_ids),
            )

    def _verify_existing_freeze(
        self,
        portfolio: PortfolioRevision,
        request: PortfolioFreezeRequest,
    ) -> None:
        controls = PortfolioControlRevisions.model_validate(portfolio.control_revisions)
        if portfolio.program_id != request.program_id:
            raise PortfolioPreparationError("稳定 Portfolio identity 已绑定其他 Program")
        if portfolio.frontier_hash != self._hash_refs(request.source_frontier):
            raise PortfolioPreparationError("稳定 Portfolio identity 的 source frontier 不一致")
        if (
            controls.workspace_revision != request.workspace_revision
            or controls.loop_revision != request.loop_revision
            or controls.grant_revision != request.grant_revision
        ):
            raise PortfolioPreparationError("稳定 Portfolio identity 的控制 revision 不一致")

    @staticmethod
    async def _frozen(
        session: AsyncSession,
        portfolio: PortfolioRevision,
    ) -> FrozenPortfolio:
        candidate_ids = tuple(
            (
                await session.scalars(
                    select(PortfolioLaneCandidate.candidate_id)
                    .where(
                        PortfolioLaneCandidate.portfolio_revision_id
                        == portfolio.portfolio_revision_id
                    )
                    .order_by(PortfolioLaneCandidate.lane_id)
                )
            ).all()
        )
        return FrozenPortfolio(
            portfolio_revision_id=portfolio.portfolio_revision_id,
            program_id=portfolio.program_id,
            generation=portfolio.generation,
            source_frontier=tuple(
                ContextRevisionRef.model_validate(item)
                for item in portfolio.source_frontier
            ),
            controls=PortfolioControlRevisions.model_validate(
                portfolio.control_revisions
            ),
            candidate_ids=candidate_ids,
        )

    async def _lock_program(
        self,
        session: AsyncSession,
        program_id: str,
    ) -> CurationProgram:
        program = await session.scalar(
            select(CurationProgram)
            .where(CurationProgram.program_id == program_id)
            .with_for_update()
        )
        if program is None:
            raise PortfolioPreparationError("Curation Program 不存在")
        if await session.get(DesktopWorkspace, program.workspace_id) is None:
            raise PortfolioPreparationError("Program workspace 不存在")
        return program

    @staticmethod
    def _controls(
        request: PortfolioFreezeRequest,
        program: CurationProgram,
    ) -> PortfolioControlRevisions:
        return PortfolioControlRevisions(
            workspace_revision=request.workspace_revision,
            program_revision=program.revision,
            current_portfolio_revision_id=program.current_portfolio_revision_id,
            loop_revision=request.loop_revision,
            grant_revision=request.grant_revision,
        )

    @staticmethod
    async def _next_generation(session: AsyncSession, program_id: str) -> int:
        current = await session.scalar(
            select(func.max(PortfolioRevision.generation)).where(
                PortfolioRevision.program_id == program_id
            )
        )
        return int(current or 0) + 1

    async def _targets(
        self,
        session: AsyncSession,
        request: PortfolioFreezeRequest,
        program: CurationProgram,
        lanes: dict[str, CurationLane],
        portfolio_id: str,
    ) -> list[dict[str, Any]]:
        targets = []
        for intent in request.lane_intents:
            lane = lanes[intent.lane_id]
            context_id = await self._target_context(
                session,
                program,
                lane,
                intent,
                portfolio_id,
            )
            base = (
                await self._contexts.current(session, context_id)
                if context_id is not None
                else None
            )
            targets.append({
                "lane_id": lane.lane_id,
                "action": intent.action.value,
                "purpose": intent.purpose,
                "target_context_id": context_id,
                "base_context_revision": (
                    base.ref.model_dump(mode="json") if base is not None else None
                ),
                "publisher_epoch": lane.publisher_epoch,
            })
        return targets

    async def _create_candidates(
        self,
        session: AsyncSession,
        portfolio_id: str,
        intents: tuple[PortfolioLaneIntent, ...],
        targets: list[dict[str, Any]],
    ) -> list[str]:
        candidate_ids = []
        for intent, target in zip(intents, targets, strict=True):
            candidate_id = uuid.uuid4().hex
            candidate_ids.append(candidate_id)
            await self._portfolios.add_candidate(
                session,
                portfolio_id,
                intent.lane_id,
                intent.action,
                intent.purpose,
                target_context_id=target["target_context_id"],
                base_context_revision_id=(
                    target["base_context_revision"]["revision_id"]
                    if target["base_context_revision"] is not None
                    else None
                ),
                base_publisher_epoch=target["publisher_epoch"],
                source_allocation=[
                    item.model_dump(mode="json") for item in intent.source_allocation
                ],
                source_frontier_hash=self._hash_refs(intent.source_allocation),
                semantic_fingerprint=intent.semantic_fingerprint,
                candidate_id=candidate_id,
            )
        return candidate_ids

    async def _verify_frontier(
        self,
        session: AsyncSession,
        frontier: tuple[ContextRevisionRef, ...],
        workspace_id: str,
    ) -> None:
        for ref in sorted(frontier, key=lambda item: item.context_id):
            thread = await session.get(DesktopThread, ref.context_id)
            if thread is None or thread.workspace_id != workspace_id:
                raise PortfolioPreparationError("source frontier 必须属于 Program workspace")
            try:
                await self._contexts.get(session, ref)
            except Exception as exc:
                raise PortfolioPreparationError(
                    f"source frontier revision 不存在: {ref.context_id}/{ref.revision_id}"
                ) from exc

    async def _lock_lanes(
        self,
        session: AsyncSession,
        request: PortfolioFreezeRequest,
    ) -> dict[str, CurationLane]:
        ids = sorted(intent.lane_id for intent in request.lane_intents)
        lanes = list(
            (
                await session.scalars(
                    select(CurationLane)
                    .where(CurationLane.lane_id.in_(ids))
                    .order_by(CurationLane.lane_id)
                    .with_for_update()
                )
            ).all()
        )
        by_id = {lane.lane_id: lane for lane in lanes}
        if set(by_id) != set(ids):
            raise PortfolioPreparationError("目标 Lane 不完整")
        if any(lane.program_id != request.program_id for lane in lanes):
            raise PortfolioPreparationError("目标 Lane 不属于 Curation Program")
        return by_id

    async def _target_context(
        self,
        session: AsyncSession,
        program: CurationProgram,
        lane: CurationLane,
        intent: PortfolioLaneIntent,
        portfolio_id: str,
    ) -> str | None:
        if intent.action is PortfolioLaneAction.CREATE:
            if lane.managed_context_id is not None:
                raise PortfolioPreparationError("create Lane 已有 managed Context")
            context_id = uuid.uuid4().hex
            session.add(
                DesktopThread(
                    task_id=context_id,
                    workspace_id=program.workspace_id,
                    thread_id=f"curation:{context_id}",
                    title=intent.purpose,
                    ui_state={"staged_by_portfolio": portfolio_id},
                )
            )
            await session.flush()
            return context_id
        if intent.action in {PortfolioLaneAction.UPDATE, PortfolioLaneAction.KEEP}:
            if lane.managed_context_id is None:
                raise PortfolioPreparationError(f"{intent.action.value} Lane 缺少 managed Context")
            return lane.managed_context_id
        return lane.managed_context_id

    @staticmethod
    def _hash_refs(refs: tuple[ContextRevisionRef, ...]) -> str:
        raw = json.dumps(
            [item.model_dump(mode="json") for item in refs],
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode()).hexdigest()


class PortfolioCandidatePreparer:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        context_publisher: ContextRevisionPublisher,
        context_revisions: ContextRevisionRepository,
    ) -> None:
        self._sessions = session_factory
        self._context_publisher = context_publisher
        self._contexts = context_revisions

    async def prepare(
        self,
        portfolio_revision_id: str,
        compiled_candidates: dict[str, CompiledLaneCandidate],
        suffix_messages: dict[str, tuple[dict[str, Any], ...]] | None = None,
    ) -> tuple[ContextRevisionRef, ...]:
        candidate_ids = await self._candidate_ids(portfolio_revision_id)
        if candidate_ids is None:
            return ()
        prepared: list[ContextRevisionRef] = []
        try:
            for candidate_id in candidate_ids:
                ref = await self._prepare_one(
                    portfolio_revision_id,
                    candidate_id,
                    compiled_candidates.get(candidate_id),
                    (suffix_messages or {}).get(candidate_id, ()),
                )
                if ref is not None:
                    prepared.append(ref)
        except Exception as exc:
            await self._fail(portfolio_revision_id, candidate_id, str(exc))
            raise PortfolioPreparationError(str(exc)) from exc
        await self._ready(portfolio_revision_id)
        return tuple(prepared)

    async def _candidate_ids(self, portfolio_id: str) -> list[str] | None:
        async with self._sessions() as session:
            portfolio = await session.get(PortfolioRevision, portfolio_id)
            if portfolio is None:
                raise PortfolioPreparationError("Portfolio 不存在")
            if portfolio.status in {
                PortfolioRevisionStatus.READY.value,
                PortfolioRevisionStatus.PUBLISHED.value,
            }:
                return None
            if portfolio.status != PortfolioRevisionStatus.PREPARING.value:
                raise PortfolioPreparationError("Portfolio 不处于 preparing")
            return list(
                (
                    await session.scalars(
                        select(PortfolioLaneCandidate.candidate_id)
                        .where(PortfolioLaneCandidate.portfolio_revision_id == portfolio_id)
                        .order_by(PortfolioLaneCandidate.lane_id)
                    )
                ).all()
            )

    async def _prepare_one(
        self,
        portfolio_id: str,
        candidate_id: str,
        compiled: CompiledLaneCandidate | None,
        suffix_messages: tuple[dict[str, Any], ...],
    ) -> ContextRevisionRef | None:
        async with self._sessions.begin() as session:
            candidate = await session.scalar(
                select(PortfolioLaneCandidate)
                .where(PortfolioLaneCandidate.candidate_id == candidate_id)
                .with_for_update()
            )
            if candidate is None or candidate.portfolio_revision_id != portfolio_id:
                raise PortfolioPreparationError("Lane candidate 不存在")
            action = PortfolioLaneAction(candidate.action)
            if candidate.status == PortfolioLaneCandidateStatus.READY.value:
                revision = await self._contexts.get_by_id(
                    session, candidate.candidate_context_revision_id
                )
                return revision.ref
            if candidate.status in {
                PortfolioLaneCandidateStatus.UNCHANGED.value,
                PortfolioLaneCandidateStatus.PUBLISHED.value,
            }:
                return None
            if action is PortfolioLaneAction.KEEP:
                if candidate.base_context_revision_id is None:
                    raise PortfolioPreparationError("keep Lane 缺少 base revision")
                candidate.candidate_context_revision_id = candidate.base_context_revision_id
                candidate.status = PortfolioLaneCandidateStatus.UNCHANGED.value
                return None
            if action in {PortfolioLaneAction.PAUSE, PortfolioLaneAction.RETIRE}:
                candidate.status = PortfolioLaneCandidateStatus.READY.value
                return None
            if compiled is None:
                raise PortfolioPreparationError("changed Lane 缺少 compiled candidate")
            self._verify_compiled(candidate, compiled)
            candidate.status = PortfolioLaneCandidateStatus.PREPARING.value
            current = await self._contexts.current(session, candidate.target_context_id)
            expected = current.ref if current is not None else None
            if self._revision_id(expected) != candidate.base_context_revision_id:
                raise PortfolioPreparationError("Lane base revision 已变化")
            request = ContextRevisionPrepareRequest(
                context_id=candidate.target_context_id,
                expected_base=expected,
                sources=tuple(
                    ContextRevisionSourceContract(source=source, position=index)
                    for index, source in enumerate(compiled.source_frontier)
                ),
                authored_messages=compiled.authored_messages,
                origin_kind=ContextRevisionOriginKind.CURATION,
                origin_id=portfolio_id,
            )
            prepared = await self._context_publisher.prepare(
                session,
                request,
                suffix_messages=suffix_messages,
            )
            if not prepared.publishable or prepared.revision.projection_status.value != "valid":
                raise PortfolioPreparationError("自动策展 revision 必须直接 valid")
            await self._contexts.insert(session, prepared.revision)
            candidate.candidate_context_revision_id = prepared.revision.ref.revision_id
            candidate.semantic_fingerprint = compiled.semantic_fingerprint
            candidate.message_lineage = [
                item.model_dump(mode="json") for item in compiled.message_lineage
            ]
            candidate.source_dispositions = [
                item.model_dump(mode="json") for item in compiled.source_dispositions
            ]
            candidate.status = PortfolioLaneCandidateStatus.READY.value
            candidate.error = None
            return prepared.revision.ref

    @staticmethod
    def _verify_compiled(
        candidate: PortfolioLaneCandidate,
        compiled: CompiledLaneCandidate,
    ) -> None:
        if compiled.action != candidate.action:
            raise PortfolioPreparationError("compiled candidate action 与冻结 Lane 不一致")
        if compiled.lane_id is not None and compiled.lane_id != candidate.lane_id:
            raise PortfolioPreparationError("compiled candidate lane 与冻结 Lane 不一致")
        frozen = tuple(
            ContextRevisionRef.model_validate(item) for item in candidate.source_allocation
        )
        if compiled.source_frontier != frozen:
            raise PortfolioPreparationError("compiled candidate 偷换了 source allocation")

    async def _ready(self, portfolio_id: str) -> None:
        async with self._sessions.begin() as session:
            portfolio = await session.get(
                PortfolioRevision,
                portfolio_id,
                with_for_update=True,
            )
            if portfolio is None:
                raise PortfolioPreparationError("Portfolio 不存在")
            if portfolio.status in {
                PortfolioRevisionStatus.READY.value,
                PortfolioRevisionStatus.PUBLISHED.value,
            }:
                return
            if portfolio.status != PortfolioRevisionStatus.PREPARING.value:
                raise PortfolioPreparationError("Portfolio 不处于 preparing")
            attempt = await self._attempt(session, portfolio_id)
            portfolio.status = PortfolioRevisionStatus.READY.value
            attempt.status = PortfolioPublicationAttemptStatus.READY.value
            attempt.phase = "prepared_all"

    async def _fail(self, portfolio_id: str, candidate_id: str, error: str) -> None:
        async with self._sessions.begin() as session:
            portfolio = await session.get(PortfolioRevision, portfolio_id)
            candidate = await session.get(PortfolioLaneCandidate, candidate_id)
            attempt = await self._attempt(session, portfolio_id)
            if portfolio is not None:
                portfolio.status = PortfolioRevisionStatus.ERROR.value
                portfolio.error = error
                portfolio.completed_at = datetime.now(UTC)
            if candidate is not None:
                candidate.status = PortfolioLaneCandidateStatus.ERROR.value
                candidate.error = error
            attempt.status = PortfolioPublicationAttemptStatus.ERROR.value
            attempt.phase = "prepare_candidate"
            attempt.error = error
            attempt.completed_at = datetime.now(UTC)

    @staticmethod
    async def _attempt(
        session: AsyncSession,
        portfolio_id: str,
    ) -> PortfolioPublicationAttempt:
        attempt = await session.scalar(
            select(PortfolioPublicationAttempt)
            .where(PortfolioPublicationAttempt.portfolio_revision_id == portfolio_id)
            .order_by(PortfolioPublicationAttempt.attempt_number.desc())
            .limit(1)
        )
        if attempt is None:
            raise PortfolioPreparationError("Portfolio publication attempt 不存在")
        return attempt

    @staticmethod
    def _revision_id(ref: ContextRevisionRef | None) -> str | None:
        return ref.revision_id if ref is not None else None


class AtomicPortfolioPublisher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        context_revisions: ContextRevisionRepository,
        outbox: CurationOutboxRepository | None = None,
    ) -> None:
        self._sessions = session_factory
        self._contexts = context_revisions
        self._outbox = outbox or CurationOutboxRepository()

    async def publish(
        self,
        portfolio_revision_id: str,
        current_controls: PortfolioControlRevisions,
        authority_hook: PortfolioAuthorityCommitHook | None = None,
    ) -> PortfolioPublicationResult:
        for attempt in range(3):
            try:
                if authority_hook is not None:
                    authority_hook.begin_attempt()
                async with self._sessions.begin() as session:
                    return await self._commit(
                        session,
                        portfolio_revision_id,
                        current_controls,
                        authority_hook,
                    )
            except PortfolioSuperseded as exc:
                await self._record_failure(portfolio_revision_id, str(exc), superseded=True)
                raise
            except Exception as exc:
                if attempt < 2 and self._retryable_transaction_error(exc):
                    continue
                await self._record_failure(portfolio_revision_id, str(exc), superseded=False)
                raise PortfolioPublicationError(str(exc)) from exc
        raise PortfolioPublicationError("Portfolio publication retry exhausted")

    async def recover(self, portfolio_revision_id: str) -> Literal[
        "published", "retryable", "failed"
    ]:
        async with self._sessions.begin() as session:
            portfolio = await session.scalar(
                select(PortfolioRevision)
                .where(PortfolioRevision.portfolio_revision_id == portfolio_revision_id)
                .with_for_update()
            )
            if portfolio is None:
                raise PortfolioPublicationError("Portfolio 不存在")
            program = await session.get(CurationProgram, portfolio.program_id)
            if program.current_portfolio_revision_id == portfolio_revision_id:
                if not await self._committed_pointers_match(session, portfolio_revision_id):
                    attempt = await PortfolioCandidatePreparer._attempt(
                        session, portfolio_revision_id
                    )
                    attempt.status = PortfolioPublicationAttemptStatus.RECOVERY_REQUIRED.value
                    attempt.phase = "reconcile_unknown"
                    attempt.error = "Program pointer 与 Lane Context pointers 不一致"
                    return "failed"
                await self._finalize_committed(session, program, portfolio)
                return "published"
            if portfolio.status in {
                PortfolioRevisionStatus.READY.value,
                PortfolioRevisionStatus.PUBLISHING.value,
            }:
                portfolio.status = PortfolioRevisionStatus.READY.value
                return "retryable"
            return "failed"

    async def _committed_pointers_match(
        self,
        session: AsyncSession,
        portfolio_id: str,
    ) -> bool:
        candidates = list(
            (
                await session.scalars(
                    select(PortfolioLaneCandidate).where(
                        PortfolioLaneCandidate.portfolio_revision_id == portfolio_id
                    )
                )
            ).all()
        )
        for candidate in candidates:
            if candidate.action not in {"create", "update", "keep"}:
                continue
            context = await session.get(DesktopThread, candidate.target_context_id)
            if (
                context is None
                or context.current_revision_id != candidate.candidate_context_revision_id
            ):
                return False
        return True

    async def _commit(
        self,
        session: AsyncSession,
        portfolio_id: str,
        current_controls: PortfolioControlRevisions,
        authority_hook: PortfolioAuthorityCommitHook | None,
    ) -> PortfolioPublicationResult:
        if authority_hook is not None:
            await authority_hook.lock_authority(session)
        probe = await session.get(PortfolioRevision, portfolio_id)
        if probe is None:
            raise PortfolioPublicationError("Portfolio 不存在")
        workspace = await session.scalar(
            select(DesktopWorkspace)
            .join(CurationProgram, CurationProgram.workspace_id == DesktopWorkspace.workspace_id)
            .where(CurationProgram.program_id == probe.program_id)
            .with_for_update()
        )
        program = await session.scalar(
            select(CurationProgram)
            .where(CurationProgram.program_id == probe.program_id)
            .with_for_update()
        )
        portfolio = await session.scalar(
            select(PortfolioRevision)
            .where(PortfolioRevision.portfolio_revision_id == portfolio_id)
            .with_for_update()
        )
        if workspace is None or program is None or portfolio is None:
            raise PortfolioPublicationError("Portfolio authority identity 不完整")
        if portfolio.status == PortfolioRevisionStatus.PUBLISHED.value:
            return await self._result(session, portfolio, idempotent=True)
        if portfolio.status != PortfolioRevisionStatus.READY.value:
            raise PortfolioPublicationError("Portfolio 不处于 ready")
        frozen = PortfolioControlRevisions.model_validate(portfolio.control_revisions)
        if current_controls != frozen:
            raise PortfolioSuperseded("workspace/loop/grant 控制 revision 已变化")
        if (
            program.revision != frozen.program_revision
            or program.current_portfolio_revision_id != frozen.current_portfolio_revision_id
        ):
            raise PortfolioSuperseded("Curation Program revision 或 current Portfolio 已变化")
        candidates = list(
            (
                await session.scalars(
                    select(PortfolioLaneCandidate)
                    .where(PortfolioLaneCandidate.portfolio_revision_id == portfolio_id)
                    .order_by(PortfolioLaneCandidate.lane_id)
                    .with_for_update()
                )
            ).all()
        )
        lanes = await self._lock_lanes(session, candidates)
        contexts = await self._lock_contexts(session, candidates)
        await self._verify_frontier(session, portfolio)
        self._verify_candidates(candidates, lanes, contexts)
        attempt = await PortfolioCandidatePreparer._attempt(session, portfolio_id)
        attempt.status = PortfolioPublicationAttemptStatus.PUBLISHING.value
        attempt.phase = "authoritative_commit"
        portfolio.status = PortfolioRevisionStatus.PUBLISHING.value
        refs = await self._switch_candidates(session, candidates, lanes, contexts)
        if authority_hook is not None:
            await authority_hook.commit(
                session,
                portfolio,
                tuple(candidates),
                refs,
                frozen,
            )
        program.current_portfolio_revision_id = portfolio_id
        program.revision += 1
        await self._finalize_committed(session, program, portfolio, attempt=attempt)
        return await self._result(session, portfolio, refs=refs)

    @staticmethod
    def _retryable_transaction_error(exc: Exception) -> bool:
        if not isinstance(exc, DBAPIError):
            return False
        original = getattr(exc, "orig", None)
        code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        return code in {"40001", "40P01"}

    async def _lock_lanes(
        self,
        session: AsyncSession,
        candidates: list[PortfolioLaneCandidate],
    ) -> dict[str, CurationLane]:
        ids = sorted(candidate.lane_id for candidate in candidates)
        rows = list(
            (
                await session.scalars(
                    select(CurationLane)
                    .where(CurationLane.lane_id.in_(ids))
                    .order_by(CurationLane.lane_id)
                    .with_for_update()
                )
            ).all()
        )
        return {row.lane_id: row for row in rows}

    async def _lock_contexts(
        self,
        session: AsyncSession,
        candidates: list[PortfolioLaneCandidate],
    ) -> dict[str, DesktopThread]:
        ids = sorted(
            {
                candidate.target_context_id
                for candidate in candidates
                if candidate.target_context_id is not None
            }
        )
        rows = list(
            (
                await session.scalars(
                    select(DesktopThread)
                    .where(DesktopThread.task_id.in_(ids))
                    .order_by(DesktopThread.task_id)
                    .with_for_update()
                )
            ).all()
        )
        return {row.task_id: row for row in rows}

    async def _verify_frontier(
        self,
        session: AsyncSession,
        portfolio: PortfolioRevision,
    ) -> None:
        for raw in portfolio.source_frontier:
            ref = ContextRevisionRef.model_validate(raw)
            try:
                await self._contexts.get(session, ref)
            except Exception as exc:
                raise PortfolioSuperseded(
                    f"source frontier revision 不存在: {ref.context_id}/{ref.revision_id}"
                ) from exc

    @staticmethod
    def _verify_candidates(
        candidates: list[PortfolioLaneCandidate],
        lanes: dict[str, CurationLane],
        contexts: dict[str, DesktopThread],
    ) -> None:
        for candidate in candidates:
            lane = lanes.get(candidate.lane_id)
            if lane is None or lane.publisher_epoch != candidate.base_publisher_epoch:
                raise PortfolioSuperseded(f"Lane publisher epoch 已变化: {candidate.lane_id}")
            action = PortfolioLaneAction(candidate.action)
            expected_status = (
                PortfolioLaneCandidateStatus.UNCHANGED.value
                if action is PortfolioLaneAction.KEEP
                else PortfolioLaneCandidateStatus.READY.value
            )
            if candidate.status != expected_status:
                raise PortfolioPublicationError(f"Lane candidate 未 ready: {candidate.lane_id}")
            if candidate.target_context_id is not None:
                context = contexts.get(candidate.target_context_id)
                if context is None:
                    raise PortfolioSuperseded("目标 Context 已不存在")
                if context.current_revision_id != candidate.base_context_revision_id:
                    raise PortfolioSuperseded(
                        f"Lane base revision 已变化: {candidate.lane_id}"
                    )

    async def _switch_candidates(
        self,
        session: AsyncSession,
        candidates: list[PortfolioLaneCandidate],
        lanes: dict[str, CurationLane],
        contexts: dict[str, DesktopThread],
    ) -> tuple[ContextRevisionRef, ...]:
        refs: list[ContextRevisionRef] = []
        for candidate in candidates:
            lane = lanes[candidate.lane_id]
            action = PortfolioLaneAction(candidate.action)
            if action in {PortfolioLaneAction.CREATE, PortfolioLaneAction.UPDATE}:
                revision = await self._contexts.get_by_id(
                    session, candidate.candidate_context_revision_id
                )
                context = contexts[candidate.target_context_id]
                context.current_revision_id = revision.ref.revision_id
                context.ui_state = {
                    key: value
                    for key, value in (context.ui_state or {}).items()
                    if key != "staged_by_portfolio"
                }
                lane.managed_context_id = context.task_id
                lane.lifecycle = "active"
                lane.current_source_frontier_hash = candidate.source_frontier_hash
                lane.current_semantic_fingerprint = candidate.semantic_fingerprint
                lane.publisher_epoch += 1
                candidate.status = PortfolioLaneCandidateStatus.PUBLISHED.value
                refs.append(revision.ref)
            elif action is PortfolioLaneAction.KEEP:
                revision = await self._contexts.get_by_id(
                    session, candidate.candidate_context_revision_id
                )
                refs.append(revision.ref)
            elif action is PortfolioLaneAction.PAUSE:
                lane.lifecycle = "paused"
                lane.publisher_epoch += 1
                candidate.status = PortfolioLaneCandidateStatus.PUBLISHED.value
            else:
                lane.lifecycle = "retired"
                lane.publisher_epoch += 1
                candidate.status = PortfolioLaneCandidateStatus.PUBLISHED.value
        await session.flush()
        return tuple(refs)

    async def _finalize_committed(
        self,
        session: AsyncSession,
        program: CurationProgram,
        portfolio: PortfolioRevision,
        *,
        attempt: PortfolioPublicationAttempt | None = None,
    ) -> None:
        now = datetime.now(UTC)
        portfolio.status = PortfolioRevisionStatus.PUBLISHED.value
        portfolio.error = None
        portfolio.completed_at = now
        portfolio.published_at = now
        if attempt is None:
            attempt = await PortfolioCandidatePreparer._attempt(
                session, portfolio.portfolio_revision_id
            )
        attempt.status = PortfolioPublicationAttemptStatus.PUBLISHED.value
        attempt.phase = "committed"
        attempt.error = None
        attempt.completed_at = now
        candidates = list(
            (
                await session.scalars(
                    select(PortfolioLaneCandidate).where(
                        PortfolioLaneCandidate.portfolio_revision_id
                        == portfolio.portfolio_revision_id
                    )
                )
            ).all()
        )
        for candidate in candidates:
            if candidate.status == PortfolioLaneCandidateStatus.READY.value:
                candidate.status = PortfolioLaneCandidateStatus.PUBLISHED.value
        await self._outbox.enqueue(
            session,
            portfolio.portfolio_revision_id,
            "PortfolioPublished",
            {
                "program_id": program.program_id,
                "portfolio_revision_id": portfolio.portfolio_revision_id,
                "generation": portfolio.generation,
            },
        )

    async def _result(
        self,
        session: AsyncSession,
        portfolio: PortfolioRevision,
        *,
        refs: tuple[ContextRevisionRef, ...] | None = None,
        idempotent: bool = False,
    ) -> PortfolioPublicationResult:
        if refs is None:
            revision_ids = list(
                (
                    await session.scalars(
                        select(PortfolioLaneCandidate.candidate_context_revision_id).where(
                            PortfolioLaneCandidate.portfolio_revision_id
                            == portfolio.portfolio_revision_id,
                            PortfolioLaneCandidate.candidate_context_revision_id.is_not(None),
                        )
                    )
                ).all()
            )
            resolved = []
            for revision_id in revision_ids:
                resolved.append((await self._contexts.get_by_id(session, revision_id)).ref)
            refs = tuple(resolved)
        return PortfolioPublicationResult(
            portfolio_revision_id=portfolio.portfolio_revision_id,
            program_id=portfolio.program_id,
            generation=portfolio.generation,
            context_revisions=refs,
            event_id=self._outbox.event_id(
                portfolio.portfolio_revision_id, "PortfolioPublished"
            ),
            idempotent=idempotent,
        )

    async def _record_failure(
        self,
        portfolio_id: str,
        error: str,
        *,
        superseded: bool,
    ) -> None:
        async with self._sessions.begin() as session:
            portfolio = await session.get(PortfolioRevision, portfolio_id)
            if portfolio is None or portfolio.status == PortfolioRevisionStatus.PUBLISHED.value:
                return
            portfolio.status = (
                PortfolioRevisionStatus.SUPERSEDED.value
                if superseded
                else PortfolioRevisionStatus.ERROR.value
            )
            portfolio.error = error
            portfolio.completed_at = datetime.now(UTC)
            attempt = await PortfolioCandidatePreparer._attempt(session, portfolio_id)
            attempt.status = (
                PortfolioPublicationAttemptStatus.SUPERSEDED.value
                if superseded
                else PortfolioPublicationAttemptStatus.RECOVERY_REQUIRED.value
            )
            attempt.phase = "authoritative_commit"
            attempt.error = error
            attempt.completed_at = datetime.now(UTC)
            await self._outbox.enqueue(
                session,
                portfolio_id,
                "PortfolioPublicationFailed",
                {
                    "program_id": portfolio.program_id,
                    "portfolio_revision_id": portfolio_id,
                    "generation": portfolio.generation,
                    "superseded": superseded,
                    "error": error,
                },
            )
