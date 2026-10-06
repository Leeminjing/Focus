"""本文件对外提供工作规划角色读面、请求容量与原准入政策的回归测试。
输入为生产精读会话、脚本化模型与冻结证据；输出为模型可见的共享角色合同、分页前计入输入及不自动改写候选的断言。
具体工作流为通过生产工作规划入口读取角色合同，再以同一Corpus分类核对并保留消费/历史身份。
示例：pytest backend/tests/test_evidence_role_read_contract.py；受控模型只替代远端响应，不替代最终证据准入。
"""

import asyncio
from copy import deepcopy

import pytest

from focus.runtime.runs.usage import ModelUsage

from backend.app.desktop.agent_loop.context_expansion.retrieval_planner import RetrievalBackedCognitiveAdvisor
from backend.tests.test_retrieval_work_citation_feedback import _prepared


class _RoleAwareModel:
    last_usage = ModelUsage(model_calls=1, input_tokens=100, output_tokens=20)

    def __init__(self, *, invalid_role=False):
        self.invalid_role = invalid_role
        self.calls = 0
        self.payload = None

    async def invoke(self, schema, system, payload):
        self.calls += 1
        self.payload = deepcopy(payload)
        contract = payload["evidence_role_contract"]
        assert contract["semantic_unit_kinds"]["claim"] == ["conversation"]
        assert contract["structured_sources"]["material"] == "material"
        assert "run_result" not in contract
        assert set(contract["semantic_unit_kinds"]) == {read["content"]["kind"] for read in payload["exact_reads"]}
        kind = payload["exact_reads"][0]["content"]["kind"]
        role = "material" if self.invalid_role else contract["semantic_unit_kinds"][kind][0]
        return schema.model_validate({"rationale": "Independent source review", "work_specs": [{
            "objective": "Review frozen ownership facts", "separation_reason": "Independent causal audit",
            "questions": ["Which ownership conditions are present?"], "completion_criteria": ["Grounded review is delivered"],
            "workspace_requirement": "read_only", "evidence_requirements": [{
                "requirement_id": "source", "role": role, "question": "What does the frozen source establish?",
                "coverage_criterion": "Existing source is read", "candidate_unit_ids": [payload["allowed_candidate_unit_ids"][0]],
            }],
        }]})


def test_work_model_receives_corpus_role_contract_before_creating_candidates():
    async def run():
        model = _RoleAwareModel()
        proposal, session = await RetrievalBackedCognitiveAdvisor(model)._plan_v2_work({}, _prepared())
        assert proposal is not None and session.state != "blocked"
        assert model.calls == session.usage.model_calls == 1
        assert session.usage.input_tokens == 100 and session.usage.output_tokens == 20
        assert proposal.work_specs[0].evidence_requirements[0].candidate_unit_ids == (session.reads[0].entry_id,)
    asyncio.run(run())


@pytest.mark.parametrize("kind,expected", [
    ("decision", ("decision",)), ("claim", ("conversation",)),
    ("hypothesis", ("conversation",)), ("unresolved_question", ("conversation",)),
    ("implementation_effect", ("implementation", "workspace_effect")),
    ("verification_result", ("test",)), ("failure", ("failure",)),
])
def test_projected_roles_match_corpus_without_changing_frozen_input(kind, expected):
    from backend.app.desktop.agent_loop.context_expansion.contracts import ContextSemanticManifest, SemanticEvidenceUnit
    from backend.app.desktop.agent_loop.context_expansion.evidence_corpus import FrozenEvidenceCorpusReader
    from backend.app.desktop.agent_loop.context_expansion.evidence_roles import evidence_role_contract
    from backend.tests._semantic_context_fixtures import single_source_fixture

    evidence = single_source_fixture()
    source, = evidence.sources
    unit = SemanticEvidenceUnit.create(kind=kind, authority="confirmed", statement="Exact frozen fact",
                                       evidence_refs=(source.messages[0].ref,))
    manifest = ContextSemanticManifest.create(source=source.source, source_content_hash=source.content_hash,
        projector_version="role-contract-test", role="primary", active_objective="Source audit", units=(unit,))
    before = (evidence.model_dump_json(), manifest.model_dump_json())
    item, = FrozenEvidenceCorpusReader._items(evidence, (manifest,), {source.source.revision_id: "primary"})
    assert item.semantic_roles == expected
    assert evidence_role_contract()["semantic_unit_kinds"][kind] == list(expected)
    assert item.semantic_unit_ids == (unit.unit_id,)
    assert before == (evidence.model_dump_json(), manifest.model_dump_json())


def test_message_roles_union_context_authority_and_keep_material_separate():
    from backend.app.desktop.agent_loop.context_expansion.contracts import ContextSemanticManifest, SemanticEvidenceUnit
    from backend.app.desktop.agent_loop.context_expansion.evidence_corpus import FrozenEvidenceCorpusReader
    from backend.app.desktop.agent_loop.context_expansion.evidence_roles import evidence_role_contract
    from backend.tests._semantic_context_fixtures import single_source_fixture

    evidence = single_source_fixture()
    source, = evidence.sources
    units = tuple(SemanticEvidenceUnit.create(kind=kind, authority="confirmed", statement=kind,
        evidence_refs=(source.messages[0].ref,)) for kind in ("claim", "verification_result"))
    manifest = ContextSemanticManifest.create(source=source.source, source_content_hash=source.content_hash,
        projector_version="role-contract-test", role="implementation", active_objective="Source audit", units=units)
    item, = FrozenEvidenceCorpusReader._items(evidence, (manifest,), {source.source.revision_id: "IMPLEMENTATION"})
    assert item.semantic_roles == ("conversation", "implementation", "test")
    assert "material" not in item.semantic_roles
    contract = evidence_role_contract()
    for role, expected in contract["source_context_roles"].items():
        projected, = FrozenEvidenceCorpusReader._items(evidence, (), {source.source.revision_id: role.upper()})
        assert projected.semantic_roles == (expected,)
    fallback, = FrozenEvidenceCorpusReader._items(evidence, (), {source.source.revision_id: "primary"})
    assert fallback.semantic_roles == (contract["message_fallback_role"],)


def test_projected_run_status_and_typed_material_roles_are_isolated():
    from backend.app.desktop.agent_loop.context_expansion.evidence_corpus import FrozenEvidenceCorpusReader
    from backend.app.desktop.agent_loop.context_expansion.evidence_roles import evidence_role_contract
    from backend.app.desktop.context_curation import MaterialEvidenceRef, MultiSourceEvidence, RunResultEvidenceRef, StructuredEvidence
    from backend.tests._semantic_context_fixtures import single_source_fixture

    contract = evidence_role_contract()
    statuses = (*contract["run_result"]["failure_statuses"], "success", "unknown")
    structured = tuple(StructuredEvidence(ref=RunResultEvidenceRef(run_id="run", context_id="context",
        result_id=status, content_hash="a" * 64), content={"status": status.upper()}) for status in statuses)
    material = StructuredEvidence(ref=MaterialEvidenceRef(material_id="material", version_id="v1", content_hash="b" * 64), content="Source file")
    source = single_source_fixture()
    evidence = MultiSourceEvidence(sources=source.sources, structured=(*structured, material),
        evidence_frontier=(*source.evidence_frontier, *(item.ref for item in (*structured, material))))
    items = FrozenEvidenceCorpusReader._items(evidence, (), {})
    assert [item.semantic_roles for item in items] == [("conversation",)] + [("failure",)] * 3 + [("test",)] * 2 + [("material",)]
    contract["semantic_unit_kinds"]["claim"].append("material")
    contract["source_context_roles"].clear()
    contract["run_result"]["failure_statuses"].clear()
    assert evidence_role_contract()["semantic_unit_kinds"]["claim"] == ["conversation"]
    assert evidence_role_contract()["source_context_roles"]["testing"] == "test"
    assert evidence_role_contract()["run_result"]["failure_statuses"] == ["error", "failed", "failure"]


def test_role_contract_is_in_input_before_evidence_page_capacity_check(monkeypatch):
    from backend.app.desktop.agent_loop.context_expansion.evidence_read_paging import EvidenceReadPager

    original = EvidenceReadPager.pages
    observed = []

    def measured(self, session, reads, planning_input, **kwargs):
        observed.append(deepcopy(planning_input["evidence_role_contract"]))
        return original(self, session, reads, planning_input, **kwargs)

    monkeypatch.setattr(EvidenceReadPager, "pages", measured)

    async def run():
        model = _RoleAwareModel()
        proposal, _ = await RetrievalBackedCognitiveAdvisor(model)._plan_v2_work({}, _prepared())
        assert proposal is not None
        assert observed == [model.payload["evidence_role_contract"]]
    asyncio.run(run())


def test_role_projection_uses_only_frozen_read_contexts_and_keeps_catalog():
    from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import PortfolioIndexCatalog
    from backend.tests.test_complete_semantic_context_derivation import _long_index

    index = _long_index()
    catalog = PortfolioIndexCatalog.create(frontier_hash="f" * 64, indexes=(index,))
    payload = {"scope": {"derivation_input": {"portfolio_index_catalog": catalog.model_dump(mode="json")}}}
    before = deepcopy(payload)
    projected = RetrievalBackedCognitiveAdvisor._work_input(payload, _prepared())
    assert projected["evidence_role_contract"]["source_context_roles"] == {"requirements": "requirement"}
    assert projected["derivation"]["portfolio_index_catalog"]["descriptors"] == before["scope"]["derivation_input"]["portfolio_index_catalog"]["descriptors"]
    assert payload == before


def test_role_read_contract_adds_no_semantic_retry_or_candidate_rewrite():
    async def run():
        model = _RoleAwareModel(invalid_role=True)
        proposal, session = await RetrievalBackedCognitiveAdvisor(model)._plan_v2_work({}, _prepared())
        assert proposal.work_specs[0].evidence_requirements[0].role == "material"
        assert model.calls == session.usage.model_calls == 1
        assert session.model_results["work_spec"]["work_specs"][0]["evidence_requirements"][0]["role"] == "material"
    asyncio.run(run())
