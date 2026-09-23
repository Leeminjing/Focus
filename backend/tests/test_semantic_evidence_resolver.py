r"""本文件对外提供 multi-source semantic evidence resolver 的纯合同测试。

输入为 R3/R8/R5/F2、重复 message identity、完整或残缺 Tool Exchange 与语义不相关 corpus；输出为最小覆盖、
协议闭包、稳定排序或明确 blocker。具体工作流为直接构造冻结 corpus 并调用 resolver，不连接数据库且不使用关键词或最近消息。
示例：`pytest backend/tests/test_semantic_evidence_resolver.py -q`。
"""

from __future__ import annotations

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    EvidenceRequirement,
    ExpansionBlocker,
    ExpansionOpportunity,
    ResolvedEvidenceBundle,
    WorkContextSpec,
)
from backend.app.desktop.agent_loop.context_expansion.evidence_corpus import (
    CorpusEvidenceItem,
    FrozenEvidenceCorpus,
)
from backend.app.desktop.agent_loop.context_expansion.evidence_resolver import (
    MultiSourceEvidenceResolver,
)
from backend.app.desktop.context_curation import (
    MultiSourceEvidence,
    NamespacedMessageRef,
    SourceMessageEvidence,
    SourceRevisionEvidence,
    evidence_ref_key,
)
from backend.tests._semantic_context_fixtures import (
    duplicate_message_id_fixture,
    failure_analysis_fixture,
    incomplete_tool_exchange_fixture,
)


def _opportunity(spec: WorkContextSpec, evidence: MultiSourceEvidence) -> ExpansionOpportunity:
    return ExpansionOpportunity.create(
        loop_id="loop-semantic",
        round_id="round-semantic",
        observation_hash="f" * 64,
        work_spec=spec,
        manifest_sources=tuple(source.source for source in evidence.sources),
    )


def _corpus(
    evidence: MultiSourceEvidence,
    roles: dict[tuple[str, ...], tuple[str, ...]],
) -> FrozenEvidenceCorpus:
    items = []
    for source in evidence.sources:
        for message in source.messages:
            items.append(
                CorpusEvidenceItem(
                    ref=message.ref,
                    content_hash=source.content_hash,
                    content=message.model_dump(mode="json"),
                    semantic_roles=roles.get(evidence_ref_key(message.ref), ("conversation",)),
                    semantic_unit_ids=(_unit_id(message.ref),),
                    source_context_role=source.source.context_id.removeprefix("context-"),
                )
            )
    for item in evidence.structured:
        items.append(
            CorpusEvidenceItem(
                ref=item.ref,
                content_hash=item.ref.content_hash,
                content=item.content,
                semantic_roles=roles.get(evidence_ref_key(item.ref), ("conversation",)),
                semantic_unit_ids=(_unit_id(item.ref),),
            )
        )
    full = evidence.model_copy(
        update={
            "evidence_frontier": tuple(
                [message.ref for source in evidence.sources for message in source.messages]
                + [item.ref for item in evidence.structured]
            )
        }
    )
    return FrozenEvidenceCorpus.create(evidence=full, items=tuple(items))


def _unit_id(ref) -> str:
    return "unit:" + "/".join(evidence_ref_key(ref))


def _single_requirement_spec(
    *,
    role: str,
    necessity: str = "required",
    candidate_unit_ids: tuple[str, ...] = (),
) -> WorkContextSpec:
    return WorkContextSpec.create(
        planner_version="planner-v1",
        objective="Resolve exact evidence.",
        separation_reason="The evidence requires an independent view.",
        questions=("What does the exact evidence establish?",),
        completion_criteria=("The requirement is covered by an exact reference.",),
        workspace_requirement="read_only",
        evidence_requirements=(
            EvidenceRequirement(
                requirement_id="target",
                role=role,
                question="What is the target evidence?",
                coverage_criterion="An exact semantic-role match is present.",
                necessity=necessity,
                candidate_unit_ids=candidate_unit_ids,
            ),
        ),
    )


def test_resolver_builds_r3_r8_r5_f2_bundle_with_tool_protocol_closure() -> None:
    spec, evidence, resolved_items = failure_analysis_fixture()
    role_by_requirement = {item.requirement_id: item.ref for item in resolved_items}
    spec = WorkContextSpec.create(
        planner_version=spec.planner_version,
        objective=spec.objective,
        separation_reason=spec.separation_reason,
        questions=spec.questions,
        completion_criteria=spec.completion_criteria,
        workspace_requirement=spec.workspace_requirement,
        evidence_requirements=tuple(
            requirement.model_copy(
                update={"candidate_unit_ids": (_unit_id(role_by_requirement[requirement.requirement_id]),)}
            )
            for requirement in spec.evidence_requirements
        ),
    )
    roles = {
        evidence_ref_key(role_by_requirement["R3"]): ("requirement",),
        evidence_ref_key(role_by_requirement["R8"]): ("implementation",),
        evidence_ref_key(role_by_requirement["R5"]): ("test",),
        evidence_ref_key(role_by_requirement["F2"]): ("failure",),
    }
    result = MultiSourceEvidenceResolver().resolve(
        _opportunity(spec, evidence),
        (),
        _corpus(evidence, roles),
    )

    assert isinstance(result, ResolvedEvidenceBundle)
    assert {item.requirement_id for item in result.items} == {"R3", "R8", "R5", "F2"}
    assert {ref.message_id for ref in result.evidence_frontier if hasattr(ref, "message_id")} == {
        "R8",
        "R5-call",
        "R5",
    }
    assert len(result.source_frontier) == 2


def test_lexically_related_but_semantically_unrelated_message_cannot_cover_requirement() -> None:
    evidence = duplicate_message_id_fixture()
    spec = _single_requirement_spec(role="failure")
    result = MultiSourceEvidenceResolver().resolve(
        _opportunity(spec, evidence),
        (),
        _corpus(
            evidence,
            {evidence_ref_key(ref): ("failure",) for ref in evidence.evidence_frontier},
        ),
    )

    assert isinstance(result, ExpansionBlocker)
    assert result.code == "required_evidence_unresolved"


def test_optional_unresolved_evidence_is_omitted() -> None:
    evidence = duplicate_message_id_fixture()
    spec = _single_requirement_spec(role="material", necessity="optional")
    result = MultiSourceEvidenceResolver().resolve(
        _opportunity(spec, evidence),
        (),
        _corpus(evidence, {}),
    )

    assert isinstance(result, ResolvedEvidenceBundle)
    assert result.items == ()


def test_duplicate_message_ids_remain_namespaced_and_minimal() -> None:
    evidence = duplicate_message_id_fixture()
    left = evidence.sources[0].messages[0].ref
    spec = _single_requirement_spec(
        role="conversation",
        candidate_unit_ids=(_unit_id(left),),
    )
    result = MultiSourceEvidenceResolver().resolve(
        _opportunity(spec, evidence),
        (),
        _corpus(evidence, {}),
    )

    assert isinstance(result, ResolvedEvidenceBundle)
    assert len(result.items) == 1
    assert len(result.source_frontier) == 1
    assert result.items[0].ref.source.context_id == "context-duplicate-left"


def test_selected_incomplete_tool_exchange_is_blocked() -> None:
    evidence = incomplete_tool_exchange_fixture()
    caller = evidence.sources[0].messages[0].ref
    spec = _single_requirement_spec(role="test", candidate_unit_ids=(_unit_id(caller),))
    result = MultiSourceEvidenceResolver().resolve(
        _opportunity(spec, evidence),
        (),
        _corpus(evidence, {evidence_ref_key(caller): ("test",)}),
    )

    assert isinstance(result, ExpansionBlocker)
    assert result.code == "required_evidence_unresolved"
    assert "sibling Tool Result" in result.summary


def test_protocol_closure_obeys_evidence_budget() -> None:
    _, evidence, resolved_items = failure_analysis_fixture()
    test_ref = next(item.ref for item in resolved_items if item.requirement_id == "R5")
    test_only = _single_requirement_spec(role="test", candidate_unit_ids=(_unit_id(test_ref),))
    result = MultiSourceEvidenceResolver().resolve(
        _opportunity(test_only, evidence),
        (),
        _corpus(evidence, {evidence_ref_key(test_ref): ("test",)}),
        max_items=1,
    )

    assert isinstance(result, ExpansionBlocker)
    assert result.code == "evidence_budget_exhausted"


def test_tool_protocol_closure_is_namespaced_across_multiple_revisions() -> None:
    base = duplicate_message_id_fixture()
    sources = []
    right_result = None
    for index, source in enumerate(base.sources):
        caller = NamespacedMessageRef(source=source.source, message_id="caller")
        result_ref = NamespacedMessageRef(source=source.source, message_id="result")
        sources.append(
            SourceRevisionEvidence(
                source=source.source,
                projection_hash=source.projection_hash,
                content_hash=source.content_hash,
                messages=(
                    SourceMessageEvidence(
                        ref=caller,
                        role="ai",
                        content=f"Call from source {index}.",
                        tool_calls=({"id": "call-shared", "name": "verify", "args": {"source": index}},),
                    ),
                    SourceMessageEvidence(
                        ref=result_ref,
                        role="tool",
                        content=f"Result from source {index}.",
                        tool_call_id="call-shared",
                        name="verify",
                        status="success",
                    ),
                ),
            )
        )
        if index == 1:
            right_result = result_ref
    evidence = MultiSourceEvidence(
        sources=tuple(sources),
        evidence_frontier=tuple(message.ref for source in sources for message in source.messages),
    )
    spec = _single_requirement_spec(role="test", candidate_unit_ids=(_unit_id(right_result),))
    result = MultiSourceEvidenceResolver().resolve(
        _opportunity(spec, evidence),
        (),
        _corpus(evidence, {evidence_ref_key(right_result): ("test",)}),
    )

    assert isinstance(result, ResolvedEvidenceBundle)
    assert result.source_frontier == (sources[1].source,)
    assert {message.ref.message_id for message in result.evidence.sources[0].messages} == {"caller", "result"}
