"""本文件对外提供 ContextRevisionRepository 的当前状态、候选与活动输入持久化端口。

输入为调用方事务、精确 Context 合同、预期 current 和 owner；输出为当前状态、受保护输入或明确的冲突。
具体工作流为候选暂存快照，发布在 Context 锁下比较完整预期引用并覆盖唯一 current；输入释放通知上层清理端口。
同 Context 来源折叠成当前跨 Context 直接关系，不创建历史父链；读取不会用 latest 替代缺失的精确输入。
示例：await repo.switch_current(session, next_ref, expected_ref)；await repo.release(session, "run", run_id)。
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.persistence.models import DesktopThread

from .input_lifecycle import notify_context_inputs_released
from .models import ContextInputSnapshot, ContextRevision
from .schemas import (
    ContextRevisionContract,
    ContextRevisionRef,
    ContextRevisionSourceContract,
)


class ContextRevisionRepositoryError(RuntimeError):
    pass


class ContextIdentityNotFound(ContextRevisionRepositoryError):
    pass


class ContextRevisionNotFound(ContextRevisionRepositoryError):
    pass


class ContextRevisionAlreadyExists(ContextRevisionRepositoryError):
    pass


class ContextRevisionIdentityMismatch(ContextRevisionRepositoryError):
    pass


class ContextRevisionCycle(ContextRevisionRepositoryError):
    pass


class StaleContextRevision(ContextRevisionRepositoryError):
    pass


class ContextRevisionRepository:
    @staticmethod
    def snapshot_key(owner_kind: str, owner_id: str, revision_id: str) -> str:
        return hashlib.sha256(
            json.dumps(
                [owner_kind, owner_id, revision_id],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    async def next_generation(self, session: AsyncSession, context_id: str) -> int:
        current = await self.current(session, context_id)
        return current.ref.generation + 1 if current else 1

    async def insert(
        self, session: AsyncSession, revision: ContextRevisionContract
    ) -> ContextRevisionContract:
        return (await self.insert_many(session, (revision,)))[0]

    async def insert_many(
        self, session: AsyncSession, revisions: tuple[ContextRevisionContract, ...]
    ) -> tuple[ContextRevisionContract, ...]:
        async with session.begin_nested():
            return await self._insert_candidates(session, revisions)

    async def _insert_candidates(
        self, session: AsyncSession, revisions: tuple[ContextRevisionContract, ...]
    ) -> tuple[ContextRevisionContract, ...]:
        if not revisions:
            return ()
        if len({r.ref.revision_id for r in revisions}) != len(revisions):
            raise ContextRevisionAlreadyExists(
                "candidate batch contains duplicate state tokens"
            )
        ids = sorted(
            {r.ref.context_id for r in revisions}
            | {s.source.context_id for r in revisions for s in r.sources}
        )
        tasks = list(
            (
                await session.scalars(
                    select(DesktopThread)
                    .where(DesktopThread.task_id.in_(ids))
                    .order_by(DesktopThread.task_id)
                    .with_for_update()
                )
            ).all()
        )
        if len(tasks) != len(ids):
            raise ContextIdentityNotFound("candidate Context or source does not exist")
        if len({t.workspace_id for t in tasks}) != 1:
            raise ContextRevisionIdentityMismatch(
                "candidate and source Contexts must share a workspace"
            )
        by_id = {r.ref.revision_id: r for r in revisions}
        ordered, visiting, visited = [], set(), set()

        def visit(value):
            token = value.ref.revision_id
            if token in visiting:
                raise ContextRevisionCycle(
                    "candidate input dependencies contain a cycle"
                )
            if token in visited:
                return
            visiting.add(token)
            for edge in value.sources:
                if edge.source.revision_id in by_id:
                    visit(by_id[edge.source.revision_id])
            visiting.remove(token)
            visited.add(token)
            ordered.append(value)

        for value in revisions:
            visit(value)
        prepared = {}
        for revision in ordered:
            direct = []
            for edge in revision.sources:
                source = prepared.get(edge.source.revision_id)
                if source is None:
                    source = await self.get(session, edge.source)
                if source.ref != edge.source:
                    raise ContextRevisionIdentityMismatch(
                        "candidate source identity mismatch"
                    )
                # A continuation inherits current cross-Context provenance, not
                # a permanent same-Context parent chain.
                refs = (
                    [s.source for s in source.sources]
                    if edge.source.context_id == revision.ref.context_id
                    else [edge.source]
                )
                for ref in refs:
                    if ref.context_id != revision.ref.context_id and ref not in direct:
                        direct.append(ref)
            inherited_initial = revision.initial_message_ids
            if not inherited_initial and revision.ref.payload_mode.value == "checkpoint":
                same_context = next(
                    (
                        edge.source
                        for edge in revision.sources
                        if edge.source.context_id == revision.ref.context_id
                    ),
                    None,
                )
                if same_context is not None:
                    inherited_initial = (
                        await self.get(session, same_context)
                    ).initial_message_ids
            revision = revision.model_copy(
                update={
                    "sources": tuple(
                        ContextRevisionSourceContract(source=ref, position=i)
                        for i, ref in enumerate(direct)
                    ),
                    "initial_message_ids": inherited_initial,
                }
            )
            existing = await self._find(session, revision.ref.revision_id)
            if existing is not None:
                if existing != revision:
                    raise ContextRevisionAlreadyExists(
                        "state token already identifies different content"
                    )
            else:
                await self._save_snapshot(
                    session, revision, "candidate", revision.ref.revision_id
                )
            prepared[revision.ref.revision_id] = revision
        return tuple(prepared[value.ref.revision_id] for value in revisions)

    async def get(
        self, session: AsyncSession, ref: ContextRevisionRef
    ) -> ContextRevisionContract:
        value = await self.get_by_id(session, ref.revision_id)
        if value.ref != ref:
            raise ContextRevisionIdentityMismatch(
                "exact Context execution identity mismatch"
            )
        return value

    async def get_input(
        self, session: AsyncSession, ref: ContextRevisionRef
    ) -> ContextRevisionContract:
        """Read published current input or a published state held by an active owner."""
        value = await self.get(session, ref)
        current = await session.scalar(
            select(ContextRevision.revision_id).where(
                ContextRevision.context_id == ref.context_id
            )
        )
        if current != ref.revision_id:
            published = await session.scalar(
                select(ContextInputSnapshot.snapshot_key)
                .where(
                    ContextInputSnapshot.revision_id == ref.revision_id,
                    ContextInputSnapshot.is_published.is_(True),
                )
                .limit(1)
            )
            if published is None:
                raise ContextRevisionNotFound(
                    "unpublished candidates cannot be used as execution input"
                )
        if value.projection_status.value not in {
            "valid",
            "repaired",
            "approved",
            "deleted",
        }:
            raise ContextRevisionIdentityMismatch(
                "Context input is not a published executable state"
            )
        return value

    async def get_by_id(
        self, session: AsyncSession, revision_id: str
    ) -> ContextRevisionContract:
        value = await self._find(session, revision_id)
        if value is None:
            raise ContextRevisionNotFound(
                f"Context state is neither current nor pinned: {revision_id}"
            )
        return value

    async def _find(
        self, session: AsyncSession, revision_id: str
    ) -> ContextRevisionContract | None:
        row = await session.get(ContextRevision, revision_id)
        if row is not None:
            return self._contract(row)
        payload = await session.scalar(
            select(ContextInputSnapshot.payload)
            .where(ContextInputSnapshot.revision_id == revision_id)
            .order_by(ContextInputSnapshot.snapshot_key)
            .limit(1)
        )
        if payload is None:
            return None
        value = ContextRevisionContract.model_validate(payload)
        if value.ref.revision_id != revision_id:
            raise ContextRevisionIdentityMismatch(
                "snapshot payload does not match its state token"
            )
        return value

    async def current(
        self, session: AsyncSession, context_id: str
    ) -> ContextRevisionContract | None:
        task = await session.get(DesktopThread, context_id)
        if task is None:
            raise ContextIdentityNotFound(f"Context does not exist: {context_id}")
        row = await session.scalar(
            select(ContextRevision).where(ContextRevision.context_id == context_id)
        )
        if row is None:
            if task.current_revision_id is not None:
                raise ContextRevisionNotFound("Context current state is missing")
            return None
        if task.current_revision_id != row.revision_id:
            raise ContextRevisionIdentityMismatch(
                "Context current pointer does not match its state"
            )
        return self._contract(row)

    async def find_by_checkpoint(
        self, session: AsyncSession, context_id: str, checkpoint_id: str
    ) -> ContextRevisionContract | None:
        current = await self.current(session, context_id)
        if current is not None and current.ref.checkpoint_id == checkpoint_id:
            return current
        payloads = (
            await session.scalars(
                select(ContextInputSnapshot.payload).where(
                    ContextInputSnapshot.context_id == context_id
                )
            )
        ).all()
        for payload in payloads:
            value = ContextRevisionContract.model_validate(payload)
            if value.ref.checkpoint_id == checkpoint_id:
                return value
        return None

    async def list_workspace(
        self, session: AsyncSession, workspace_id: str
    ) -> tuple[ContextRevisionContract, ...]:
        rows = (
            await session.scalars(
                select(ContextRevision)
                .join(
                    DesktopThread, DesktopThread.task_id == ContextRevision.context_id
                )
                .where(DesktopThread.workspace_id == workspace_id)
                .order_by(ContextRevision.context_id)
            )
        ).all()
        return tuple(self._contract(row) for row in rows)

    async def switch_current(
        self,
        session: AsyncSession,
        next_ref: ContextRevisionRef,
        expected_ref: ContextRevisionRef | None,
    ) -> ContextRevisionRef:
        task = await session.get(
            DesktopThread,
            next_ref.context_id,
            with_for_update=True,
            populate_existing=True,
        )
        if task is None:
            raise ContextIdentityNotFound("publication Context does not exist")
        expected_id = expected_ref.revision_id if expected_ref else None
        if expected_ref and expected_ref.context_id != next_ref.context_id:
            raise ContextRevisionIdentityMismatch(
                "expected and next state belong to different Contexts"
            )
        if task.current_revision_id != expected_id:
            raise StaleContextRevision(
                f"Context changed: expected={expected_id}, actual={task.current_revision_id}"
            )
        candidate = await self.get(session, next_ref)
        if (
            expected_ref
            and next_ref != expected_ref
            and next_ref.generation != expected_ref.generation + 1
        ):
            raise StaleContextRevision(
                "new Context generation must follow the expected current state"
            )
        row = await session.scalar(
            select(ContextRevision)
            .where(ContextRevision.context_id == next_ref.context_id)
            .execution_options(populate_existing=True)
        )
        actual_ref = self._row_ref(row) if row is not None else None
        if actual_ref != expected_ref:
            raise StaleContextRevision(
                "expected execution identity does not match current Context state"
            )
        if row is None:
            session.add(self._to_row(candidate))
        elif row.revision_id != next_ref.revision_id:
            values = self._to_row(candidate)
            for column in ContextRevision.__table__.columns:
                setattr(row, column.name, deepcopy(getattr(values, column.name)))
        task.current_revision_id = next_ref.revision_id
        await session.flush()
        await session.execute(
            update(ContextInputSnapshot)
            .where(ContextInputSnapshot.revision_id == next_ref.revision_id)
            .values(is_published=True)
        )
        await self.release(session, "candidate", next_ref.revision_id)
        return next_ref

    async def freeze(
        self,
        session: AsyncSession,
        ref: ContextRevisionRef,
        *,
        owner_kind: str,
        owner_id: str,
    ) -> ContextRevisionContract:
        if not owner_kind or not owner_id or owner_kind == "candidate":
            raise ValueError("execution snapshots require an explicit owner")
        # The same lock as publication closes the read/current replacement race.
        await session.get(
            DesktopThread, ref.context_id, with_for_update=True, populate_existing=True
        )
        value = await self.get(session, ref)
        current_token = await session.scalar(
            select(ContextRevision.revision_id).where(
                ContextRevision.context_id == ref.context_id
            )
        )
        prior_published = await session.scalar(
            select(ContextInputSnapshot.snapshot_key)
            .where(
                ContextInputSnapshot.revision_id == ref.revision_id,
                ContextInputSnapshot.is_published.is_(True),
            )
            .limit(1)
        )
        await self._save_snapshot(
            session,
            value,
            owner_kind,
            owner_id,
            is_published=current_token == ref.revision_id
            or prior_published is not None,
        )
        await self.discard_candidate(session, ref.revision_id)
        return value

    async def _save_snapshot(
        self,
        session: AsyncSession,
        value: ContextRevisionContract,
        owner_kind: str,
        owner_id: str,
        *,
        is_published: bool = False,
    ) -> None:
        key = self.snapshot_key(owner_kind, owner_id, value.ref.revision_id)
        payload = value.model_dump(mode="json")
        await session.execute(
            insert(ContextInputSnapshot)
            .values(
                snapshot_key=key,
                owner_kind=owner_kind,
                owner_id=owner_id,
                revision_id=value.ref.revision_id,
                context_id=value.ref.context_id,
                payload=payload,
                is_published=is_published,
            )
            .on_conflict_do_nothing(index_elements=["snapshot_key"])
        )
        saved = await session.get(ContextInputSnapshot, key, populate_existing=True)
        if saved.payload != payload:
            raise ContextRevisionIdentityMismatch(
                "snapshot owner already holds different content for this token"
            )
        if is_published:
            saved.is_published = True

    async def release(
        self, session: AsyncSession, owner_kind: str, owner_id: str
    ) -> None:
        owned = (
            ContextInputSnapshot.owner_kind == owner_kind,
            ContextInputSnapshot.owner_id == owner_id,
        )
        contexts = tuple(
            (
                await session.scalars(
                    select(ContextInputSnapshot.context_id)
                    .where(*owned)
                    .distinct()
                    .order_by(ContextInputSnapshot.context_id)
                )
            ).all()
        )
        if not contexts:
            return
        await session.scalars(
            select(DesktopThread)
            .where(DesktopThread.task_id.in_(contexts))
            .order_by(DesktopThread.task_id)
            .with_for_update()
        )
        await session.execute(delete(ContextInputSnapshot).where(*owned))
        for context_id in contexts:
            await notify_context_inputs_released(session, context_id)

    async def discard_candidate(self, session: AsyncSession, revision_id: str) -> None:
        await self.release(session, "candidate", revision_id)

    async def release_owners(
        self, session: AsyncSession, owners: tuple[tuple[str, str], ...]
    ) -> None:
        """Release only owners whose terminal state the caller has established."""
        for kind, identity in set(owners):
            await self.release(session, kind, identity)

    async def record_publication(
        self, session: AsyncSession, ref: ContextRevisionRef
    ) -> None:
        current = await self.current(session, ref.context_id)
        if current is None or current.ref != ref:
            raise StaleContextRevision(
                "publication must atomically replace the current Context state"
            )

    def _contract(
        self,
        row: ContextRevision,
        sources: tuple[ContextRevisionSourceContract, ...] | None = None,
    ) -> ContextRevisionContract:
        return ContextRevisionContract(
            ref=self._row_ref(row),
            history_payload=deepcopy(row.history_payload),
            sources=sources
            if sources is not None
            else tuple(
                ContextRevisionSourceContract.model_validate(item)
                for item in row.sources
            ),
            authored_messages=tuple(deepcopy(row.authored_messages)),
            execution_messages=tuple(deepcopy(row.execution_messages)),
            repair_manifest=tuple(deepcopy(row.repair_manifest)),
            issues=tuple(deepcopy(row.issues)),
            initial_message_ids=tuple(row.initial_message_ids),
            definition_hash=row.definition_hash,
            projection_hash=row.projection_hash,
            content_hash=row.content_hash,
            projection_status=row.projection_status,
            origin_kind=row.origin_kind,
            origin_id=row.origin_id,
            created_at=row.created_at,
            deleted_at=row.deleted_at,
        )

    @staticmethod
    def _row_ref(row: ContextRevision) -> ContextRevisionRef:
        return ContextRevisionRef(
            context_id=row.context_id,
            revision_id=row.revision_id,
            generation=row.generation,
            execution_thread_id=row.execution_thread_id,
            checkpoint_ns=row.checkpoint_ns,
            checkpoint_id=row.checkpoint_id,
            payload_mode=row.payload_mode,
        )

    @staticmethod
    def _to_row(contract: ContextRevisionContract) -> ContextRevision:
        ref = contract.ref
        return ContextRevision(
            revision_id=ref.revision_id,
            context_id=ref.context_id,
            generation=ref.generation,
            sources=[item.model_dump(mode="json") for item in contract.sources],
            execution_thread_id=ref.execution_thread_id,
            checkpoint_ns=ref.checkpoint_ns,
            checkpoint_id=ref.checkpoint_id,
            payload_mode=ref.payload_mode.value,
            history_payload=contract.history_payload.model_dump(mode="json")
            if contract.history_payload is not None
            else None,
            authored_messages=[]
            if contract.history_payload is not None
            else deepcopy(list(contract.authored_messages)),
            execution_messages=[]
            if contract.history_payload is not None
            else deepcopy(list(contract.execution_messages)),
            repair_manifest=deepcopy(list(contract.repair_manifest)),
            issues=deepcopy(list(contract.issues)),
            initial_message_ids=list(contract.initial_message_ids),
            definition_hash=contract.definition_hash,
            projection_hash=contract.projection_hash,
            content_hash=contract.content_hash,
            projection_status=contract.projection_status.value,
            origin_kind=contract.origin_kind.value,
            origin_id=contract.origin_id,
            created_at=contract.created_at,
            deleted_at=contract.deleted_at,
        )
