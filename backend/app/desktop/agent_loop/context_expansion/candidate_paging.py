r"""本文件对外提供 CandidateDescriptorPager、ProviderRequestWindowError 与 provider_request_limit。

输入为冻结 planning session、已授权候选、请求固定上下文和模型窗口/输出预留；输出为稳定、有序且单次请求可容纳的 candidate descriptor 页面及可复用窗口容量。
具体工作流为按可见策略与 provider 窗口取更严的单请求输入容量，以 UTF-8 字节数作为保守 Token 上界逐项装页，超大的单项以带 read-selection 阶段的错误明确阻断。
示例：`pages = pager.pages(session, candidates, planning_input, context_window_tokens=128000)`。
"""

from __future__ import annotations

import json

from backend.app.desktop.agent_loop.resource_limits import exceeds_limit, request_limits
from backend.app.desktop.agent_loop.context_expansion.contracts import stable_expansion_hash
from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import (
    CandidateDescriptorPage,
    PlanningRetrievalSession,
    RetrievalCandidate,
)


class ProviderRequestWindowError(ValueError):
    def __init__(self, summary: str, *, stage: str) -> None:
        super().__init__(summary)
        self.stage = stage


class CandidateDescriptorPager:
    def pages(
        self,
        session: PlanningRetrievalSession,
        candidates: tuple[RetrievalCandidate, ...],
        planning_input: dict,
        *,
        context_window_tokens: int | None,
        max_output_tokens: int,
    ) -> tuple[CandidateDescriptorPage, ...]:
        if session.frozen_resources is None:
            raise ValueError("candidate paging 需要冻结的新版资源策略")
        request_limit = provider_request_limit(session, context_window_tokens, max_output_tokens)
        base_size = len(self._bytes(planning_input)) + 4096
        if exceeds_limit(base_size, request_limit, inclusive=True):
            raise ProviderRequestWindowError("固定规划上下文已超过 provider 单次请求窗口", stage="read_selection")
        page_budget = None if request_limit is None else request_limit - base_size
        pages: list[CandidateDescriptorPage] = []
        current: list[RetrievalCandidate] = []
        size = 0
        for candidate in candidates:
            candidate_size = len(self._bytes(self.descriptor(candidate)))
            if exceeds_limit(candidate_size, page_budget):
                raise ProviderRequestWindowError("单个 candidate descriptor 已超过 provider 单次请求窗口", stage="read_selection")
            if current and (exceeds_limit(size + candidate_size, page_budget) or len(current) >= 256):
                pages.append(self._page(session, len(pages), current, base_size + size))
                current, size = [], 0
            current.append(candidate)
            size += candidate_size
        if current:
            pages.append(self._page(session, len(pages), current, base_size + size))
        return tuple(pages)

    @staticmethod
    def descriptor(candidate: RetrievalCandidate) -> dict:
        return {
            "candidate_id": candidate.candidate_id,
            "index_id": candidate.index_id,
            "entry_id": candidate.entry_id,
            "entry_kind": candidate.entry_kind,
            "descriptor": candidate.descriptor,
            "rank": candidate.rank,
            "score": candidate.score,
        }

    @staticmethod
    def _bytes(value: dict) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _page(
        session: PlanningRetrievalSession,
        ordinal: int,
        candidates: list[RetrievalCandidate],
        estimated_input_tokens: int,
    ) -> CandidateDescriptorPage:
        identities = tuple(item.candidate_id for item in candidates)
        return CandidateDescriptorPage(
            page_id=stable_expansion_hash("candidate-descriptor-page", session.session_id, ordinal, identities),
            ordinal=ordinal,
            candidate_ids=identities,
            estimated_input_tokens=estimated_input_tokens,
        )


def provider_request_limit(
    session: PlanningRetrievalSession,
    context_window_tokens: int | None,
    max_output_tokens: int,
) -> int | None:
    if session.frozen_resources is None:
        raise ValueError("provider request window 需要冻结的新版资源策略")
    policy = session.frozen_resources.policy
    limit, _ = request_limits(
        policy.max_request_input_tokens, policy.output_token_reserve,
        context_window_tokens, max_output_tokens,
    )
    return limit
