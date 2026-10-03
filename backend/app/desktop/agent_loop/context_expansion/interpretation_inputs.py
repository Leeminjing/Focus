r"""本文件对外提供 FrozenInterpretationInputs。

输入为完整冻结 Index 和依次覆盖库存的局部 records；输出为全局发现目录及按任意合法 segment 请求展开的原文。
具体工作流为保存全部有序目录，按冻结消息组装协议闭合原文，验证 read scope／上限并记录实际提供与请求身份。
示例：inputs.read((early_segment_id, late_segment_id))；不以关键词、相邻窗口或当前 Context pointer 筛选来源。
"""

from backend.app.desktop.agent_loop.resource_limits import exceeds_limit

from .index_model_budget import IndexBudgetExceeded
from .interpretation_record import (
    InterpretationHint,
    InterpretationInventoryEntry,
    InterpretationOriginalSegment,
)


class FrozenInterpretationInputs:
    def __init__(self, index, records, *, max_reads):
        if len(index.segments) != len(records):
            raise ValueError("interpretation local inventory incomplete")
        self._segments = {}
        self._provided = {}
        self._requests = []
        self._max_reads = max_reads
        self.inventory = tuple(
            self._entry(segment, record)
            for segment, record in zip(index.segments, records, strict=True)
        )
        for segment in index.segments:
            ids = set(segment.message_ids)
            self._segments[segment.segment_id] = InterpretationOriginalSegment(
                segment_id=segment.segment_id,
                messages=tuple(
                    m.model_dump(mode="json")
                    for m in index.messages
                    if m.message_id in ids
                ),
            )

    @staticmethod
    def _entry(segment, record):
        accepted = set(record.accepted_claim_keys)
        return InterpretationInventoryEntry(
            segment_id=segment.segment_id,
            ordinal=segment.ordinal,
            message_ids=segment.message_ids,
            content_hash=segment.content_hash,
            descriptor=segment.descriptor,
            record_id=record.record_id,
            claims=tuple(
                InterpretationHint(
                    authority=d.authority,
                    statement=d.statement,
                    message_ids=tuple(s.message_id for s in d.supports),
                )
                for d in record.drafts
                if d.claim_key in accepted
            ),
            rejections=record.rejections,
            fallback=record.fallback,
        )

    @property
    def provided(self):
        return tuple(
            item for key, item in self._segments.items() if key in self._provided
        )

    @property
    def requests(self):
        return tuple(self._requests)

    def all_originals(self):
        return tuple(self._segments.values())

    def read(self, segment_ids, *, requested=True):
        if not set(segment_ids).issubset(self._segments):
            raise ValueError("interpretation read outside frozen scope")
        combined = set(self._provided) | set(segment_ids)
        if exceeds_limit(len(combined), self._max_reads):
            raise IndexBudgetExceeded("interpretation authorized exact reads exhausted")
        if requested:
            self._requests.append(tuple(segment_ids))
        self._provided.update({key: self._segments[key] for key in segment_ids})

    def payload(self, *, originals=None):
        return {
            "inventory": tuple(e.model_dump(mode="json") for e in self.inventory),
            "segments": tuple(
                p.model_dump(mode="json")
                for p in (self.provided if originals is None else originals)
            ),
            "read_requests": self.requests,
        }
