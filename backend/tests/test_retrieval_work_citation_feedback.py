"""本文件对外提供精读工作规划引用校验、纠错、缓存与恢复回归。
输入为生产检索会话、真实精读来源和可控结构化模型；输出为严格引用、实际消费及冻结尝试上限断言。
具体工作流为通过生产分页规划入口生成非法引用，核对反馈修正、持续失败、检查点恢复和成功重放。
示例：pytest backend/tests/test_retrieval_work_citation_feedback.py -q。
"""
import asyncio

import pytest
from focus.runtime.runs.usage import ModelUsage

from backend.app.desktop.agent_loop.context_expansion.retrieval_planner import LaneAdviceProposal, RetrievalBackedCognitiveAdvisor
from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import (
    AuthorizedSemanticRetriever, PlanningRetrievalSession, PlanningRetrievalSessionController, SemanticRetrievalQuery,
)
from backend.tests.test_expansion_resource_policy import _new_v2_session
from backend.app.desktop.agent_loop.expansion_resource_policy import ExpansionResourcePolicy


def _prepared():
    index, session = _new_v2_session(policy=ExpansionResourcePolicy(max_model_attempts_per_operation=2))
    controller = PlanningRetrievalSessionController()
    retriever = AuthorizedSemanticRetriever()
    query = SemanticRetrievalQuery.create(text="durable lock", kinds=("semantic_unit",), limit=1)
    session = controller.record_query_plan(session, (query,))
    candidates, coverage = retriever.retrieve_page(session, query, (index,))
    session = controller.record_query(session, query, candidates, coverage)
    session = controller.record_reads(session, (retriever.read(session, candidates[0], (index,)),))
    return session


class _CitationModel:
    def __init__(self, *, always_invalid=False):
        self.always_invalid = always_invalid
        self.calls = 0
        self.payloads = []
        self.last_usage = ModelUsage()

    async def invoke(self, schema, system, payload):
        self.calls += 1
        self.payloads.append(payload)
        self.last_usage = ModelUsage(model_calls=1, input_tokens=100, output_tokens=20)
        invalid = self.always_invalid or self.calls == 1
        return schema.model_validate({
            "rationale": "Independent investigation", "work_specs": [{
                "objective": "Verify locking", "separation_reason": "Independent causal diagnosis",
                "questions": ["Is ownership fenced?"], "completion_criteria": ["Tests pass"],
                "workspace_requirement": "read_only", "evidence_requirements": [{
                    "requirement_id": "lock", "role": "requirement", "question": "What is required?",
                    "coverage_criterion": "Exact source is read",
                    "candidate_unit_ids": ["0" * 64 if invalid else payload["allowed_candidate_unit_ids"][0]],
                }],
            }],
        })


def test_work_citation_failure_is_corrected_before_success_cache():
    async def run():
        model = _CitationModel()
        proposal, session = await RetrievalBackedCognitiveAdvisor(model)._plan_v2_work({}, _prepared())
        assert session.state != "blocked" and proposal is not None
        assert model.calls == 2 and session.usage.model_calls == 2
        assert session.usage.input_tokens == 200 and session.usage.output_tokens == 40
        assert model.payloads[1]["previous_attempt_failure"]["category"] == "planner_evidence_identity_unknown"
        saved = session.model_results["work_spec"]
        assert saved["work_specs"][0]["evidence_requirements"][0]["candidate_unit_ids"] != ["0" * 64]
        replay, restored = await RetrievalBackedCognitiveAdvisor(model)._plan_v2_work({}, session)
        assert replay == proposal and restored == session and model.calls == 2
    asyncio.run(run())


def test_repeated_invalid_citation_preserves_exact_blocker_and_attempt_limit():
    async def run():
        model = _CitationModel(always_invalid=True)
        proposal, session = await RetrievalBackedCognitiveAdvisor(model)._plan_v2_work({}, _prepared())
        assert proposal is None and session.blocker_code == "planner_evidence_identity_unknown"
        assert session.blocker_boundary == "exact_read_identity"
        assert model.calls == 2 and session.usage.model_calls == 2
        assert "work_spec" not in session.model_results
    asyncio.run(run())


def test_invalid_citation_checkpoint_resumes_without_replaying_or_caching_failure():
    async def run():
        model = _CitationModel()
        saved = []
        async def interrupt(current):
            saved.append(PlanningRetrievalSession.model_validate_json(current.model_dump_json()))
            raise RuntimeError("checkpoint interruption")
        with pytest.raises(RuntimeError, match="checkpoint interruption"):
            await RetrievalBackedCognitiveAdvisor(model, checkpoint=interrupt)._plan_v2_work({}, _prepared())
        assert saved[0].usage.model_calls == 1 and "work_spec" not in saved[0].model_results
        proposal, resumed = await RetrievalBackedCognitiveAdvisor(model)._plan_v2_work({}, saved[0])
        assert proposal is not None and resumed.state != "blocked"
        assert model.calls == 2 and resumed.usage.model_calls == 2
        assert model.payloads[1].get("previous_attempt_failure")
    asyncio.run(run())


def test_historical_invalid_success_cache_is_rejected_without_rewriting_or_new_call():
    async def run():
        session = _prepared()
        model = _CitationModel()
        payload = {"allowed_candidate_unit_ids": [session.reads[0].entry_id]}
        bad = await model.invoke(LaneAdviceProposal, "", payload)
        session = PlanningRetrievalSessionController().record_model_usage(session,
            model_calls=1, tokens=120, input_tokens=100, output_tokens=20,
            operation_id="work_spec:attempt:1", result_operation_id="work_spec",
            result_payload=bad.model_dump(mode="json"), stage="work_spec")
        preserved = session.model_results.copy()
        proposal, blocked = await RetrievalBackedCognitiveAdvisor(model)._plan_v2_work({}, session)
        assert proposal is None and blocked.blocker_code == "planner_evidence_identity_unknown"
        assert blocked.model_results == preserved and blocked.usage == session.usage
        assert model.calls == 1
    asyncio.run(run())


def test_citation_retry_cannot_exceed_remaining_global_grant():
    async def run():
        session = _prepared()
        session = session.model_copy(update={"frozen_resources": session.frozen_resources.model_copy(
            update={"global_model_calls_remaining": 1})})
        model = _CitationModel()
        proposal, blocked = await RetrievalBackedCognitiveAdvisor(model)._plan_v2_work({}, session)
        assert proposal is None and blocked.blocker_code == "global_grant_exhausted"
        assert model.calls == 1 and blocked.usage.model_calls == 1
        assert "work_spec" not in blocked.model_results
    asyncio.run(run())
