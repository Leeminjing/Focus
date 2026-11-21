r"""本文件对外提供 SemanticDerivationArtifactRepository 与 install_context_index_lifecycle。

输入为独立 AsyncSession、完整 Index、planning session 或 stage；输出为校验后的持久缓存、幂等 session 和不可变 artifact。
具体工作流为校验数据库列与 payload 身份，唯一键竞争采用首个提交赢家；一般 stage 与旧索引仍拒绝改写。
局部 proof 按 Context 与段合同独立复用；完整 artifact 只按本次精确输入读取。
示例：index = await repository.cached_index(...)；综合模型配置改变时仍可复用符合原局部合同的段缓存。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.contexts.evolution.input_lifecycle import (
    set_context_input_release_handler,
)
from backend.app.desktop.patrol.agent_loop.context_expansion.contracts import (
    stable_expansion_hash,
)
from backend.app.desktop.patrol.agent_loop.context_expansion.models import (
    LoopContextDerivationArtifact,
    LoopPlanningRetrievalSession,
    LoopSegmentProjectionRecord,
    LoopSemanticIndexArtifact,
)
from backend.app.desktop.patrol.agent_loop.context_expansion.semantic_index import (
    RevisionSemanticIndex,
)
from backend.app.desktop.patrol.agent_loop.context_expansion.semantic_retrieval import (
    PlanningRetrievalSession,
)


class SemanticDerivationArtifactRepository:
    async def prune_indexes(self, session: AsyncSession, context_id: str) -> int:
        """Keep current/owned input indexes and active planning references only.

        Completed index payloads contain the whole frozen input. They are caches,
        not a replacement Context history archive. Local proofs stay cached for
        living Contexts; deleted sources retain only proofs of owned indexes.
        """
        from backend.app.desktop.contexts.evolution.models import (
            ContextInputSnapshot,
            ContextRevision,
        )
        from backend.app.desktop.patrol.agent_loop.models import LoopRound
        from backend.app.desktop.persistence.models import DesktopThread

        # Serialize against current publication and owner snapshot release.
        context = await session.get(DesktopThread, context_id, with_for_update=True)
        protected_tokens = set(await session.scalars(
            select(ContextRevision.revision_id)
            .join(DesktopThread, DesktopThread.current_revision_id == ContextRevision.revision_id)
            .where(DesktopThread.task_id == context_id)
        ))
        protected_tokens.update(await session.scalars(
            select(ContextInputSnapshot.revision_id)
            .join(ContextRevision, ContextRevision.revision_id == ContextInputSnapshot.revision_id)
            .where(
                ContextRevision.context_id == context_id,
                ContextRevision.is_published.is_(True),
            )
        ))
        planning_payloads = await session.scalars(
            select(LoopPlanningRetrievalSession.payload)
            .join(LoopRound, LoopRound.round_id == LoopPlanningRetrievalSession.round_id)
            .where(
                LoopRound.status.not_in(("settled", "error", "superseded")),
            )
        )
        protected_indexes = {
            identity for payload in planning_payloads
            for identity in payload.get("authorized_index_ids", ())
        }
        query = delete(LoopSemanticIndexArtifact).where(
            LoopSemanticIndexArtifact.context_id == context_id,
        )
        if protected_tokens:
            query = query.where(LoopSemanticIndexArtifact.revision_id.not_in(protected_tokens))
        if protected_indexes:
            query = query.where(LoopSemanticIndexArtifact.index_id.not_in(protected_indexes))
        result = await session.execute(query.execution_options(synchronize_session=False))
        if context is None:
            # A surviving owner may still replay a complete index after its
            # source conversation is deleted. Keep precisely its local proofs.
            retained_payloads = await session.scalars(
                select(LoopSemanticIndexArtifact.payload).where(
                    LoopSemanticIndexArtifact.context_id == context_id
                )
            )
            proof_ids = {
                identity for payload in retained_payloads
                for identity in (payload.get("inheritance") or {}).get("record_ids", ())
            }
            proof_query = delete(LoopSegmentProjectionRecord).where(
                LoopSegmentProjectionRecord.context_id == context_id
            )
            if proof_ids:
                proof_query = proof_query.where(LoopSegmentProjectionRecord.record_id.not_in(proof_ids))
            await session.execute(proof_query.execution_options(synchronize_session=False))
        return result.rowcount

    async def stage_artifact(
        self,
        session: AsyncSession,
        *,
        loop_id: str,
        round_id: str,
        stage: str,
        input_identities: tuple[str, ...],
        version: str,
    ) -> LoopContextDerivationArtifact | None:
        input_identity = stable_expansion_hash(
            "derivation-stage-input", tuple(sorted(input_identities))
        )
        return await session.scalar(
            select(LoopContextDerivationArtifact).where(
                LoopContextDerivationArtifact.loop_id == loop_id,
                LoopContextDerivationArtifact.round_id == round_id,
                LoopContextDerivationArtifact.stage == stage,
                LoopContextDerivationArtifact.input_identity == input_identity,
                LoopContextDerivationArtifact.version == version,
            )
        )

    async def indexes_by_ids(
        self,
        session: AsyncSession,
        index_ids: tuple[str, ...],
    ) -> tuple[RevisionSemanticIndex, ...]:
        if not index_ids:
            return ()
        rows = tuple(
            (
                await session.scalars(
                    select(LoopSemanticIndexArtifact).where(
                        LoopSemanticIndexArtifact.index_id.in_(index_ids),
                        LoopSemanticIndexArtifact.status == "ready",
                    )
                )
            ).all()
        )
        by_id = {row.index_id: row for row in rows}
        if set(index_ids) != set(by_id):
            raise LookupError("planning session 授权的 semantic index 不完整")
        return tuple(self._validated_index(by_id[index_id]) for index_id in index_ids)

    async def cached_index(
        self,
        session: AsyncSession,
        *,
        revision_id: str,
        source_content_hash: str,
        index_schema_version: str,
        segmenter_version: str,
        projector_version: str,
    ) -> RevisionSemanticIndex | None:
        row = await session.scalar(
            select(LoopSemanticIndexArtifact).where(
                LoopSemanticIndexArtifact.revision_id == revision_id,
                LoopSemanticIndexArtifact.source_content_hash == source_content_hash,
                LoopSemanticIndexArtifact.index_schema_version == index_schema_version,
                LoopSemanticIndexArtifact.segmenter_version == segmenter_version,
                LoopSemanticIndexArtifact.projector_version == projector_version,
                LoopSemanticIndexArtifact.status == "ready",
            )
        )
        return None if row is None else self._validated_index(row)

    @staticmethod
    def _validated_index(row) -> RevisionSemanticIndex:
        index = RevisionSemanticIndex.model_validate(row.payload)
        actual = (
            row.index_id,
            row.context_id,
            row.revision_id,
            row.source_content_hash,
            row.index_schema_version,
            row.segmenter_version,
            row.projector_version,
        )
        expected = (
            index.index_id,
            index.source.context_id,
            index.source.revision_id,
            index.source_content_hash,
            index.index_schema_version,
            index.segmenter_version,
            index.projector_version,
        )
        if actual != expected:
            raise ValueError("semantic index columns integrity mismatch")
        return index

    async def put_index(
        self,
        session: AsyncSession,
        index: RevisionSemanticIndex,
        *,
        attempt_records: tuple[dict[str, Any], ...] = (),
    ) -> LoopSemanticIndexArtifact:
        existing = await self.cached_index(
            session,
            revision_id=index.source.revision_id,
            source_content_hash=index.source_content_hash,
            index_schema_version=index.index_schema_version,
            segmenter_version=index.segmenter_version,
            projector_version=index.projector_version,
        )
        if existing is not None:
            if existing.index_id != index.index_id and index.inheritance is None:
                raise ValueError("semantic index cache key 命中不同 payload identity")
            row = await session.get(LoopSemanticIndexArtifact, existing.index_id)
            if row is None:
                raise LookupError("semantic index cache row disappeared")
            return row
        row = LoopSemanticIndexArtifact(
            index_id=index.index_id,
            context_id=index.source.context_id,
            revision_id=index.source.revision_id,
            source_content_hash=index.source_content_hash,
            index_schema_version=index.index_schema_version,
            segmenter_version=index.segmenter_version,
            projector_version=index.projector_version,
            status="ready",
            payload=index.model_dump(mode="json"),
            attempt_records=list(attempt_records),
        )
        if index.inheritance is None:
            session.add(row)
            await session.flush()
            return row
        await session.execute(
            insert(LoopSemanticIndexArtifact)
            .values(
                index_id=row.index_id,
                context_id=row.context_id,
                revision_id=row.revision_id,
                source_content_hash=row.source_content_hash,
                index_schema_version=row.index_schema_version,
                segmenter_version=row.segmenter_version,
                projector_version=row.projector_version,
                status="ready",
                payload=row.payload,
                attempt_records=row.attempt_records,
            )
            .on_conflict_do_nothing(constraint="uq_loop_semantic_index_cache_key")
        )
        winner = await self.cached_index(
            session,
            revision_id=index.source.revision_id,
            source_content_hash=index.source_content_hash,
            index_schema_version=index.index_schema_version,
            segmenter_version=index.segmenter_version,
            projector_version=index.projector_version,
        )
        if winner is None:
            raise ValueError("semantic index committed winner disappeared")
        return await session.get(LoopSemanticIndexArtifact, winner.index_id)

    async def get_session(
        self,
        session: AsyncSession,
        session_id: str,
    ) -> PlanningRetrievalSession | None:
        row = await session.get(LoopPlanningRetrievalSession, session_id)
        return (
            None
            if row is None
            else PlanningRetrievalSession.model_validate(row.payload)
        )

    async def save_session(
        self,
        session: AsyncSession,
        planning: PlanningRetrievalSession,
        *,
        loop_id: str,
        round_id: str,
        attempt_records: tuple[dict[str, Any], ...] = (),
    ) -> LoopPlanningRetrievalSession:
        row = await session.get(
            LoopPlanningRetrievalSession, planning.session_id, with_for_update=True
        )
        payload = planning.model_dump(mode="json")
        if row is None:
            row = LoopPlanningRetrievalSession(
                session_id=planning.session_id,
                loop_id=loop_id,
                round_id=round_id,
                frontier_hash=planning.frontier_hash,
                catalog_id=planning.catalog_id,
                state=planning.state,
                budget=planning.budget.model_dump(mode="json"),
                usage=planning.usage.model_dump(mode="json"),
                payload=payload,
                attempt_records=list(attempt_records),
            )
            session.add(row)
            await session.flush()
            return row
        if row.loop_id != loop_id or row.round_id != round_id:
            raise ValueError("planning session ownership 不一致")
        if (
            row.frontier_hash != planning.frontier_hash
            or row.catalog_id != planning.catalog_id
            or row.budget != planning.budget.model_dump(mode="json")
        ):
            raise ValueError("planning session 冻结 scope 或 budget 不得改写")
        previous = PlanningRetrievalSession.model_validate(row.payload)
        if not self._usage_monotonic(previous, planning):
            raise ValueError("planning session usage/audit 不得回退")
        row.state = planning.state
        row.usage = planning.usage.model_dump(mode="json")
        row.payload = payload
        row.attempt_records = [*row.attempt_records, *attempt_records]
        return row

    async def put_stage_artifact(
        self,
        session: AsyncSession,
        *,
        loop_id: str,
        round_id: str,
        stage: str,
        input_identities: tuple[str, ...],
        version: str,
        outcome: str,
        payload: dict[str, Any],
        expansion_id: str | None = None,
        attempt_records: tuple[dict[str, Any], ...] = (),
        adopt_winner: bool = False,
    ) -> LoopContextDerivationArtifact:
        input_identity = stable_expansion_hash(
            "derivation-stage-input", tuple(sorted(input_identities))
        )
        existing = await session.scalar(
            select(LoopContextDerivationArtifact).where(
                LoopContextDerivationArtifact.loop_id == loop_id,
                LoopContextDerivationArtifact.round_id == round_id,
                LoopContextDerivationArtifact.stage == stage,
                LoopContextDerivationArtifact.input_identity == input_identity,
                LoopContextDerivationArtifact.version == version,
            )
        )
        artifact_id = stable_expansion_hash(
            "derivation-stage-artifact",
            loop_id,
            round_id,
            stage,
            input_identity,
            version,
            outcome,
            payload,
        )
        if existing is not None:
            if not adopt_winner and (
                existing.artifact_id != artifact_id or existing.payload != payload
            ):
                raise ValueError(
                    "同一 derivation stage input/version 不得改写 artifact"
                )
            return existing
        row = LoopContextDerivationArtifact(
            artifact_id=artifact_id,
            loop_id=loop_id,
            round_id=round_id,
            expansion_id=expansion_id,
            stage=stage,
            input_identity=input_identity,
            version=version,
            outcome=outcome,
            payload=payload,
            attempt_records=list(attempt_records),
        )
        if adopt_winner:
            await session.execute(
                insert(LoopContextDerivationArtifact)
                .values(
                    artifact_id=artifact_id,
                    loop_id=loop_id,
                    round_id=round_id,
                    expansion_id=expansion_id,
                    stage=stage,
                    input_identity=input_identity,
                    version=version,
                    outcome=outcome,
                    payload=payload,
                    attempt_records=list(attempt_records),
                )
                .on_conflict_do_nothing(
                    constraint="uq_loop_context_derivation_stage_input"
                )
            )
            return await self.stage_artifact(
                session,
                loop_id=loop_id,
                round_id=round_id,
                stage=stage,
                input_identities=input_identities,
                version=version,
            )
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    def _usage_monotonic(
        previous: PlanningRetrievalSession,
        current: PlanningRetrievalSession,
    ) -> bool:
        fields = ("queries", "candidates", "exact_reads", "model_calls", "tokens")
        return all(
            getattr(current.usage, field) >= getattr(previous.usage, field)
            for field in fields
        )


async def _prune_released_context_input(session: AsyncSession, context_id: str) -> None:
    await SemanticDerivationArtifactRepository().prune_indexes(session, context_id)


def install_context_index_lifecycle() -> None:
    """Install this consumer's cleanup through the Context-owned release port."""
    set_context_input_release_handler(_prune_released_context_input)
