r"""本文件对外提供 Context revision、来源边和 current pointer 的事务型持久化端口。

输入为一个或一批严格 `ContextRevisionContract`、`ContextRevisionRef` 与调用方 AsyncSession；输出为
不可变历史记录、当前引用或类型化并发冲突。具体工作流为按 Context/revision identity 排序加锁，
验证同 workspace、精确 checkpoint 和批内 revision DAG 后原子插入历史事实，再以单条条件更新执行
current pointer CAS；本端口不提交事务也不提供历史更新/删除能力。示例：`await repository.insert_many(session, revisions)`。
"""

from __future__ import annotations

from copy import deepcopy
import hashlib

from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.context_evolution.models import ContextRevision, ContextRevisionSource
from backend.app.desktop.context_evolution.schemas import (
    ContextRevisionContract,
    ContextRevisionRef,
    ContextRevisionSourceContract,
)
from backend.app.desktop.models import DesktopThread


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
    async def next_generation(self, session: AsyncSession, context_id: str) -> int:
        current_max = await session.scalar(
            select(func.max(ContextRevision.generation)).where(
                ContextRevision.context_id == context_id
            )
        )
        return int(current_max or 0) + 1

    async def insert(
        self,
        session: AsyncSession,
        revision: ContextRevisionContract,
    ) -> ContextRevisionContract:
        return (await self.insert_many(session, (revision,)))[0]

    async def insert_many(
        self,
        session: AsyncSession,
        revisions: tuple[ContextRevisionContract, ...],
    ) -> tuple[ContextRevisionContract, ...]:
        if not revisions:
            return ()
        candidates = self._candidate_map(revisions)
        contexts = await self._lock_contexts(session, revisions)
        self._require_single_workspace(contexts)
        source_rows = await self._lock_source_revisions(session, revisions, candidates)
        self._require_exact_sources(revisions, source_rows, candidates)
        ordered = self._topological_order(revisions, candidates)
        rows = [self._to_row(revision) for revision in ordered]
        edges = [
            self._to_source_row(revision.ref.revision_id, source)
            for revision in ordered
            for source in revision.sources
        ]
        try:
            async with session.begin_nested():
                session.add_all(rows)
                await session.flush()
                session.add_all(edges)
                await session.flush()
        except IntegrityError as exc:
            raise ContextRevisionAlreadyExists(
                "revision batch 已存在或违反不可变唯一约束: "
                + ", ".join(revision.ref.revision_id for revision in revisions)
            ) from exc
        inserted: list[ContextRevisionContract] = []
        for revision in revisions:
            inserted.append(await self.get(session, revision.ref))
        return tuple(inserted)

    async def get(
        self,
        session: AsyncSession,
        ref: ContextRevisionRef,
    ) -> ContextRevisionContract:
        row = await self._revision_by_id(session, ref.revision_id)
        if row is None:
            raise ContextRevisionNotFound(f"revision 不存在: {ref.revision_id}")
        actual = self._row_ref(row)
        if actual != ref:
            raise ContextRevisionIdentityMismatch(
                f"revision 执行身份不匹配: expected={ref.model_dump(mode='json')}, "
                f"actual={actual.model_dump(mode='json')}"
            )
        sources = await self._sources_for(session, row.revision_id)
        return self._contract(row, sources)

    async def get_by_id(
        self,
        session: AsyncSession,
        revision_id: str,
    ) -> ContextRevisionContract:
        row = await self._revision_by_id(session, revision_id)
        if row is None:
            raise ContextRevisionNotFound(f"revision 不存在: {revision_id}")
        return self._contract(row, await self._sources_for(session, revision_id))

    async def current(
        self,
        session: AsyncSession,
        context_id: str,
    ) -> ContextRevisionContract | None:
        current_id = await session.scalar(
            select(DesktopThread.current_revision_id).where(DesktopThread.task_id == context_id)
        )
        if current_id is None:
            exists = await session.scalar(
                select(DesktopThread.task_id).where(DesktopThread.task_id == context_id)
            )
            if exists is None:
                raise ContextIdentityNotFound(f"Context 不存在: {context_id}")
            return None
        row = await self._revision_by_id(session, current_id)
        if row is None:
            raise ContextRevisionNotFound(f"current revision 不存在: {current_id}")
        return self._contract(row, await self._sources_for(session, current_id))

    async def find_by_checkpoint(
        self,
        session: AsyncSession,
        context_id: str,
        checkpoint_id: str,
    ) -> ContextRevisionContract | None:
        row = await session.scalar(
            select(ContextRevision)
            .where(
                ContextRevision.context_id == context_id,
                ContextRevision.checkpoint_id == checkpoint_id,
            )
            .order_by(ContextRevision.generation.desc())
        )
        if row is None:
            return None
        return self._contract(row, await self._sources_for(session, row.revision_id))

    async def list_workspace(
        self,
        session: AsyncSession,
        workspace_id: str,
    ) -> tuple[ContextRevisionContract, ...]:
        rows = list(
            (
                await session.scalars(
                    select(ContextRevision)
                    .join(DesktopThread, DesktopThread.task_id == ContextRevision.context_id)
                    .where(DesktopThread.workspace_id == workspace_id)
                    .order_by(
                        ContextRevision.context_id,
                        ContextRevision.generation,
                        ContextRevision.revision_id,
                    )
                )
            ).all()
        )
        revisions: list[ContextRevisionContract] = []
        for row in rows:
            revisions.append(
                self._contract(row, await self._sources_for(session, row.revision_id))
            )
        return tuple(revisions)

    async def switch_current(
        self,
        session: AsyncSession,
        next_ref: ContextRevisionRef,
        expected_ref: ContextRevisionRef | None,
    ) -> ContextRevisionRef:
        if expected_ref is not None and expected_ref.context_id != next_ref.context_id:
            raise ContextRevisionIdentityMismatch("CAS 的 expected 与 next 必须属于同一 Context")
        next_row = await self._revision_by_id(session, next_ref.revision_id)
        if next_row is None:
            raise ContextRevisionNotFound(f"next revision 不存在: {next_ref.revision_id}")
        if self._row_ref(next_row) != next_ref:
            raise ContextRevisionIdentityMismatch("next revision 执行身份不匹配")
        expected_id = expected_ref.revision_id if expected_ref is not None else None
        current_condition = (
            DesktopThread.current_revision_id.is_(None)
            if expected_id is None
            else DesktopThread.current_revision_id == expected_id
        )
        result = await session.execute(
            update(DesktopThread)
            .where(
                DesktopThread.task_id == next_ref.context_id,
                current_condition,
            )
            .values(current_revision_id=next_ref.revision_id)
        )
        if result.rowcount != 1:
            current_id = await session.scalar(
                select(DesktopThread.current_revision_id).where(
                    DesktopThread.task_id == next_ref.context_id
                )
            )
            raise StaleContextRevision(
                f"current revision 已变化: expected={expected_id}, actual={current_id}"
            )
        return next_ref

    async def _lock_contexts(
        self,
        session: AsyncSession,
        revisions: tuple[ContextRevisionContract, ...],
    ) -> dict[str, DesktopThread]:
        context_ids = sorted(
            {
                context_id
                for revision in revisions
                for context_id in (
                    revision.ref.context_id,
                    *(item.source.context_id for item in revision.sources),
                )
            }
        )
        rows = list(
            (
                await session.scalars(
                    select(DesktopThread)
                    .where(DesktopThread.task_id.in_(context_ids))
                    .order_by(DesktopThread.task_id)
                    .with_for_update()
                )
            ).all()
        )
        by_id = {row.task_id: row for row in rows}
        missing = [context_id for context_id in context_ids if context_id not in by_id]
        if missing:
            raise ContextIdentityNotFound(f"Context 不存在: {', '.join(missing)}")
        return by_id

    @staticmethod
    def _require_single_workspace(contexts: dict[str, DesktopThread]) -> None:
        if len({row.workspace_id for row in contexts.values()}) != 1:
            raise ContextRevisionIdentityMismatch("target 与所有 source Context 必须属于同一 workspace")

    async def _lock_source_revisions(
        self,
        session: AsyncSession,
        revisions: tuple[ContextRevisionContract, ...],
        candidates: dict[str, ContextRevisionContract],
    ) -> dict[str, ContextRevision]:
        revision_ids = sorted(
            {
                source.source.revision_id
                for revision in revisions
                for source in revision.sources
                if source.source.revision_id not in candidates
            }
        )
        if not revision_ids:
            return {}
        rows = list(
            (
                await session.scalars(
                    select(ContextRevision)
                    .where(ContextRevision.revision_id.in_(revision_ids))
                    .order_by(ContextRevision.revision_id)
                    .with_for_update()
                )
            ).all()
        )
        return {row.revision_id: row for row in rows}

    def _require_exact_sources(
        self,
        revisions: tuple[ContextRevisionContract, ...],
        rows: dict[str, ContextRevision],
        candidates: dict[str, ContextRevisionContract],
    ) -> None:
        for revision in revisions:
            for source in revision.sources:
                candidate = candidates.get(source.source.revision_id)
                if candidate is not None:
                    if candidate.ref != source.source:
                        raise ContextRevisionIdentityMismatch(
                            f"batch source revision 执行身份不匹配: {source.source.revision_id}"
                        )
                    continue
                row = rows.get(source.source.revision_id)
                if row is None:
                    raise ContextRevisionNotFound(
                        f"source revision 不存在: {source.source.revision_id}"
                    )
                if self._row_ref(row) != source.source:
                    raise ContextRevisionIdentityMismatch(
                        f"source revision 执行身份不匹配: {source.source.revision_id}"
                    )

    @staticmethod
    def _candidate_map(
        revisions: tuple[ContextRevisionContract, ...],
    ) -> dict[str, ContextRevisionContract]:
        by_revision = {revision.ref.revision_id: revision for revision in revisions}
        if len(by_revision) != len(revisions):
            raise ContextRevisionAlreadyExists("revision batch 包含重复 revision_id")
        context_generations = {
            (revision.ref.context_id, revision.ref.generation) for revision in revisions
        }
        if len(context_generations) != len(revisions):
            raise ContextRevisionAlreadyExists("revision batch 包含重复 Context generation")
        return by_revision

    @staticmethod
    def _topological_order(
        revisions: tuple[ContextRevisionContract, ...],
        candidates: dict[str, ContextRevisionContract],
    ) -> tuple[ContextRevisionContract, ...]:
        dependencies = {
            revision.ref.revision_id: {
                source.source.revision_id
                for source in revision.sources
                if source.source.revision_id in candidates
            }
            for revision in revisions
        }
        ready = sorted(revision_id for revision_id, sources in dependencies.items() if not sources)
        ordered: list[ContextRevisionContract] = []
        while ready:
            revision_id = ready.pop(0)
            ordered.append(candidates[revision_id])
            for target_id in sorted(dependencies):
                if revision_id not in dependencies[target_id]:
                    continue
                dependencies[target_id].remove(revision_id)
                if not dependencies[target_id] and candidates[target_id] not in ordered:
                    ready.append(target_id)
                    ready.sort()
        if len(ordered) != len(revisions):
            cyclic = sorted(
                revision_id for revision_id, sources in dependencies.items() if sources
            )
            raise ContextRevisionCycle(
                "revision batch 来源图存在循环: " + ", ".join(cyclic)
            )
        return tuple(ordered)

    async def _revision_by_id(
        self,
        session: AsyncSession,
        revision_id: str,
    ) -> ContextRevision | None:
        return await session.scalar(
            select(ContextRevision).where(ContextRevision.revision_id == revision_id)
        )

    async def _sources_for(
        self,
        session: AsyncSession,
        target_revision_id: str,
    ) -> tuple[ContextRevisionSourceContract, ...]:
        statement: Select[tuple[ContextRevisionSource, ContextRevision]] = (
            select(ContextRevisionSource, ContextRevision)
            .join(
                ContextRevision,
                ContextRevision.revision_id == ContextRevisionSource.source_revision_id,
            )
            .where(ContextRevisionSource.target_revision_id == target_revision_id)
            .order_by(ContextRevisionSource.position)
        )
        rows = (await session.execute(statement)).all()
        return tuple(
            ContextRevisionSourceContract(source=self._row_ref(source_revision), position=edge.position)
            for edge, source_revision in rows
        )

    def _contract(
        self,
        row: ContextRevision,
        sources: tuple[ContextRevisionSourceContract, ...],
    ) -> ContextRevisionContract:
        return ContextRevisionContract(
            ref=self._row_ref(row),
            sources=sources,
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
            execution_thread_id=ref.execution_thread_id,
            checkpoint_ns=ref.checkpoint_ns,
            checkpoint_id=ref.checkpoint_id,
            payload_mode=ref.payload_mode.value,
            authored_messages=deepcopy(list(contract.authored_messages)),
            execution_messages=deepcopy(list(contract.execution_messages)),
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

    @staticmethod
    def _to_source_row(
        target_revision_id: str,
        source: ContextRevisionSourceContract,
    ) -> ContextRevisionSource:
        identity = f"{target_revision_id}\0{source.source.revision_id}\0{source.position}"
        return ContextRevisionSource(
            source_edge_id=hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32],
            target_revision_id=target_revision_id,
            source_context_id=source.source.context_id,
            source_revision_id=source.source.revision_id,
            source_checkpoint_id=source.source.checkpoint_id,
            position=source.position,
        )
