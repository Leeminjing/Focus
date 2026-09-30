r"""本文件对外提供 assemble_revision_index。

输入为冻结完整 index、依次覆盖 segments 的验证 records 和 BuildPlan；输出为绑定目标 Revision 的完整不可变 index。
工作流为逐段重验依赖，仅组装已接受 drafts，重新生成来源引用及覆盖／fallback，再合并隔离账本与继承凭据。
示例：index = assemble_revision_index(indexer, frozen, records, plan)。旧 index 与 record 不被改写。
"""

from .index_build_contracts import IndexInheritanceReceipt
from .semantic_index import RevisionSemanticIndex


def assemble_revision_index(indexer, frozen, records, plan):
    if len(records) != len(frozen.segments):
        raise ValueError("index assembly record inventory incomplete")
    drafts, assessments, rejections = [], [], []
    for segment, record in zip(frozen.segments, records):
        messages = tuple(
            m for m in frozen.messages if m.message_id in set(segment.message_ids)
        )
        if record.context_id != frozen.source.context_id:
            raise ValueError("index assembly record context scope mismatch")
        record.validate_target(segment, messages, frozen.projector_version)
        accepted = set(record.accepted_claim_keys)
        drafts.extend(d for d in record.drafts if d.claim_key in accepted)
        assessments.extend(record.assessments)
        rejections.extend(record.rejections)
    receipt = IndexInheritanceReceipt.model_validate(
        {**plan.model_dump(mode="json"), "record_ids": [r.record_id for r in records]}
    )
    projected = indexer.index(
        source=frozen.source,
        source_content_hash=frozen.source_content_hash,
        context_role=frozen.context_role,
        active_objective=frozen.active_objective,
        raw_messages=tuple(m.model_dump(mode="json") for m in frozen.messages),
        unit_drafts=tuple(drafts),
        claim_support_assessments=tuple(assessments),
        projector_version=frozen.projector_version,
    )
    excluded = {
        "index_id",
        "coverage",
        "inheritance",
        "rejected_units",
        "quality_state",
    }
    values = {
        name: getattr(projected, name)
        for name in type(projected).model_fields
        if name not in excluded
    }
    return RevisionSemanticIndex.create(
        **values,
        inheritance=receipt,
        rejected_units=tuple(rejections),
        quality_state="degraded"
        if rejections or projected.fallback_segment_ids
        else "complete",
        protocol_exchange_count=projected.coverage.protocol_exchange_count,
    )
