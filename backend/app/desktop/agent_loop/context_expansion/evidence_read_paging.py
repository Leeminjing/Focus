r"""本文件对外提供 EvidenceReadPager 与 EvidenceReadPage。

输入为同一冻结 planning session 的精读证据、固定规划上下文和 provider 单请求窗口；输出为带稳定身份的有序证据批次。
具体工作流为对 UTF-8 JSON 字节数作保守 Token 上界，扣除输出预留后逐项装页；单项不可容纳时明确报告 work-spec 阶段的窗口阻断，不裁剪证据。
示例：`pages = EvidenceReadPager().pages(session, reads, planning_input, context_window_tokens=128000, max_output_tokens=8192)`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from backend.app.desktop.agent_loop.resource_limits import exceeds_limit
from backend.app.desktop.agent_loop.context_expansion.candidate_paging import (
    ProviderRequestWindowError,
    provider_request_limit,
)
from backend.app.desktop.agent_loop.context_expansion.contracts import stable_expansion_hash
from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import ExactEvidenceRead, PlanningRetrievalSession


@dataclass(frozen=True)
class EvidenceReadPage:
    page_id: str
    ordinal: int
    reads: tuple[ExactEvidenceRead, ...]


class EvidenceReadPager:
    def pages(
        self,
        session: PlanningRetrievalSession,
        reads: tuple[ExactEvidenceRead, ...],
        planning_input: dict,
        *,
        context_window_tokens: int | None,
        max_output_tokens: int,
    ) -> tuple[EvidenceReadPage, ...]:
        request_limit = provider_request_limit(session, context_window_tokens, max_output_tokens)
        base_size = self._size(planning_input) + 4096
        if exceeds_limit(base_size, request_limit, inclusive=True):
            raise ProviderRequestWindowError("固定规划上下文已超过 provider 单次请求窗口", stage="work_spec")
        page_budget = None if request_limit is None else request_limit - base_size
        pages: list[EvidenceReadPage] = []
        current: list[ExactEvidenceRead] = []
        size = 0
        for read in reads:
            read_size = self._size(read.model_dump(mode="json")) + len(read.entry_id) + 8
            if exceeds_limit(read_size, page_budget):
                raise ProviderRequestWindowError("单个 exact evidence read 已超过 provider 单次请求窗口", stage="work_spec")
            if current and (exceeds_limit(size + read_size, page_budget) or len(current) >= 64):
                pages.append(self._page(session, len(pages), current))
                current, size = [], 0
            current.append(read)
            size += read_size
        if current:
            pages.append(self._page(session, len(pages), current))
        return tuple(pages)

    @staticmethod
    def _size(value: object) -> int:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    @staticmethod
    def _page(session: PlanningRetrievalSession, ordinal: int, reads: list[ExactEvidenceRead]) -> EvidenceReadPage:
        identities = tuple(item.read_id for item in reads)
        return EvidenceReadPage(
            page_id=stable_expansion_hash("exact-evidence-page", session.session_id, ordinal, identities),
            ordinal=ordinal,
            reads=tuple(reads),
        )
