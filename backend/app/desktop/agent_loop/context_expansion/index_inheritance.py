r"""本文件对外提供 RevisionIndexInheritancePlanner。

输入为已提交目标 Revision、完整规范 index 及独立局部／整体 fingerprints；输出为基线 records 和稳定前缀 BuildPlan。
工作流为沿同 Context single-source run_settled 权威链有界查找，证明消息前缀相等，选择相同闭合 segments。
综合合同变化仍可寻找兼容局部 proof；旧整体解释不随前缀直接继承。缺少基线全量回退，完整性错误不隐藏。
示例：plan, records = await planner.plan(session, revision, index)；新增否定复用旧局部引文，整体解释重新生成。
"""

from backend.app.desktop.context_evolution.models import ContextPublicationReceipt

from .contracts import stable_expansion_hash
from .index_build_contracts import IndexBuildPlan


class RevisionIndexInheritancePlanner:
    def __init__(
        self,
        revisions,
        artifacts,
        records,
        contract: str,
        *,
        local_contract: str | None = None,
        search_limit: int = 64,
    ) -> None:
        self._revisions = revisions
        self._artifacts = artifacts
        self._records = records
        self._contract = contract
        self._local_contract = local_contract or contract
        self._search_limit = max(1, search_limit)

    async def plan(self, session, revision, target):
        cursor = revision
        for _ in range(self._search_limit):
            if (
                await session.get(ContextPublicationReceipt, cursor.ref.revision_id)
                is None
            ):
                return self._full(target, "uncommitted_source")
            if cursor.origin_kind != "run_settled" or len(cursor.sources) != 1:
                return self._full(target, "evolution_boundary")
            source = cursor.sources[0].source
            if source.context_id != target.source.context_id:
                return self._full(target, "cross_context_source")
            cursor = await self._revisions.get(session, source)
            if await session.get(ContextPublicationReceipt, source.revision_id) is None:
                return self._full(target, "uncommitted_source")
            baseline = await self._artifacts.cached_index(
                session,
                revision_id=source.revision_id,
                source_content_hash=cursor.content_hash,
                index_schema_version=target.index_schema_version,
                segmenter_version=target.segmenter_version,
                projector_version=self._contract,
            )
            if baseline is None:
                baseline = await self._artifacts.cached_local_index(
                    session,
                    revision_id=source.revision_id,
                    source_content_hash=cursor.content_hash,
                    index_schema_version=target.index_schema_version,
                    segmenter_version=target.segmenter_version,
                    local_contract=self._local_contract,
                )
                if baseline is None:
                    continue
            if baseline.inheritance is None:
                return self._full(target, "missing_projection_proofs")
            records = await self._records.by_ids(
                session, source.context_id, baseline.inheritance.record_ids
            )
            if records is None:
                return self._full(target, "missing_projection_records")
            if target.messages[: len(baseline.messages)] != baseline.messages:
                return self._full(target, "non_append_history")
            reused = []
            for old, new, record in zip(baseline.segments, target.segments, records):
                if old != new:
                    break
                messages = tuple(
                    m for m in target.messages if m.message_id in set(new.message_ids)
                )
                record.validate_target(new, messages, self._local_contract)
                reused.append(record)
            count = len(reused)
            return IndexBuildPlan(
                mode="incremental",
                baseline_index_id=baseline.index_id,
                prefix_hash=stable_expansion_hash(
                    "execution-prefix-v1",
                    tuple(m.model_dump(mode="json") for m in baseline.messages),
                ),
                reused_segment_ids=tuple(s.segment_id for s in target.segments[:count]),
                recomputed_segment_ids=tuple(
                    s.segment_id for s in target.segments[count:]
                ),
                full_build_reason=None,
            ), tuple(reused)
        return self._full(target, "ancestry_search_exhausted")

    @staticmethod
    def _full(target, reason):
        return IndexBuildPlan(
            recomputed_segment_ids=tuple(s.segment_id for s in target.segments),
            full_build_reason=reason,
        ), ()
