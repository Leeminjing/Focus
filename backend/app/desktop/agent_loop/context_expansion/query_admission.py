r"""本文件对外提供 QueryPlanAdmission 与 QueryPlanAdmissionError。

输入为冻结 planning session 和模型提出的完整 SemanticRetrievalQuery 集合；输出为通过授权、查询数与候选容量检查的查询计划或带资源边界的类型化阻断。
具体工作流为在任何检索执行前核对全部 query identities、index scope 和请求上界，不通过时保留明确边界而不丢弃已完成工作。
示例：`queries = QueryPlanAdmission().admit(session, proposed_queries)`。
"""

from __future__ import annotations

from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import (
    PlanningRetrievalSession,
    SemanticRetrievalQuery,
)


class QueryPlanAdmissionError(ValueError):
    def __init__(self, code: str, summary: str, *, boundary: str, requested: int, remaining: int) -> None:
        super().__init__(summary)
        self.code = code
        self.boundary = boundary
        self.requested = requested
        self.remaining = remaining


class QueryPlanAdmission:
    def admit(
        self,
        session: PlanningRetrievalSession,
        queries: tuple[SemanticRetrievalQuery, ...],
    ) -> tuple[SemanticRetrievalQuery, ...]:
        if len({item.query_id for item in queries}) != len(queries):
            raise QueryPlanAdmissionError(
                "retrieval_plan_duplicate_query",
                "query plan 包含重复 query identity",
                boundary="query_identity",
                requested=len(queries),
                remaining=max(0, session.budget.max_queries - session.usage.queries),
            )
        available_queries = session.budget.max_queries - session.usage.queries
        if len(queries) > available_queries:
            raise QueryPlanAdmissionError(
                "expansion_policy_limit",
                "query plan 超出冻结查询容量",
                boundary="max_queries",
                requested=len(queries),
                remaining=max(0, available_queries),
            )
        authorized = set(session.authorized_index_ids)
        if any(not set(item.index_ids).issubset(authorized) for item in queries):
            raise QueryPlanAdmissionError(
                "source_out_of_scope",
                "query plan 请求了未授权 index",
                boundary="authorized_index_ids",
                requested=len(queries),
                remaining=max(0, available_queries),
            )
        requested_candidates = sum(item.limit for item in queries)
        available_candidates = session.budget.max_candidates - session.usage.candidates
        if requested_candidates > available_candidates:
            raise QueryPlanAdmissionError(
                "expansion_policy_limit",
                "query plan 请求上界超出冻结唯一候选容量",
                boundary="max_unique_candidates",
                requested=requested_candidates,
                remaining=max(0, available_candidates),
            )
        return queries
