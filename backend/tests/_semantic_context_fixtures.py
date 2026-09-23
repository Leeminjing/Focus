r"""本文件对外提供 semantic context derivation 测试使用的冻结证据与工作规格 fixtures。

输入为可选的 Context 名称与证据覆盖选择；输出为精确 Revision、R3/R8/R5/F2 多来源 evidence、单来源 evidence
和对应 `WorkContextSpec`。具体工作流为用稳定 identity/hash 构造 Mission、Context message 与 Run result 引用，
使测试不依赖 substring、最近消息或数据库。示例：`spec, evidence, items = failure_analysis_fixture()`。
"""

from __future__ import annotations

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    EvidenceRequirement,
    ResolvedEvidenceItem,
    WorkContextSpec,
)
from backend.app.desktop.context_curation import (
    MissionEvidenceRef,
    MultiSourceEvidence,
    NamespacedMessageRef,
    RunResultEvidenceRef,
    SourceMessageEvidence,
    SourceRevisionEvidence,
    StructuredEvidence,
)
from backend.app.desktop.context_evolution import ContextRevisionRef


def revision_ref(name: str, generation: int = 1) -> ContextRevisionRef:
    return ContextRevisionRef(
        context_id=f"context-{name}",
        revision_id=f"revision-{name}-{generation}",
        generation=generation,
        execution_thread_id=f"thread-{name}",
        checkpoint_ns="",
        checkpoint_id=f"checkpoint-{name}-{generation}",
        payload_mode="checkpoint",
    )


def message_ref(source: ContextRevisionRef, message_id: str) -> NamespacedMessageRef:
    return NamespacedMessageRef(source=source, message_id=message_id)


def failure_analysis_fixture() -> tuple[
    WorkContextSpec,
    MultiSourceEvidence,
    tuple[ResolvedEvidenceItem, ...],
]:
    implementation = revision_ref("implementation")
    testing = revision_ref("testing")
    requirement_ref = MissionEvidenceRef(
        loop_id="loop-semantic",
        goal_revision=3,
        item_id="R3",
        content_hash="3" * 64,
    )
    implementation_ref = message_ref(implementation, "R8")
    test_call_ref = message_ref(testing, "R5-call")
    test_ref = message_ref(testing, "R5")
    failure_ref = RunResultEvidenceRef(
        run_id="run-F2",
        context_id=testing.context_id,
        result_id="F2",
        content_hash="2" * 64,
    )
    requirements = (
        EvidenceRequirement(
            requirement_id="R3",
            role="requirement",
            question="What behavior is required?",
            coverage_criterion="The exact mission requirement is available.",
        ),
        EvidenceRequirement(
            requirement_id="R8",
            role="implementation",
            question="What implementation currently enforces the behavior?",
            coverage_criterion="The relevant implementation decision is available.",
        ),
        EvidenceRequirement(
            requirement_id="R5",
            role="test",
            question="What test asserts the required behavior?",
            coverage_criterion="The exact assertion and result are available.",
        ),
        EvidenceRequirement(
            requirement_id="F2",
            role="failure",
            question="How did the latest run fail?",
            coverage_criterion="The stable failure result is available.",
        ),
    )
    spec = WorkContextSpec.create(
        planner_version="planner-v1",
        objective="Determine the falsifiable root cause of the failed behavior.",
        separation_reason="The investigation needs requirement, implementation, test, and runtime evidence together.",
        questions=("Which implementation assumption contradicts the observed failure?",),
        completion_criteria=("A root-cause hypothesis is tied to reproducible evidence.",),
        workspace_requirement="read_only",
        evidence_requirements=requirements,
    )
    evidence = MultiSourceEvidence(
        sources=(
            SourceRevisionEvidence(
                source=implementation,
                projection_hash="8" * 64,
                content_hash="a" * 64,
                messages=(
                    SourceMessageEvidence(
                        ref=implementation_ref,
                        role="ai",
                        content="The implementation increments the counter after persistence.",
                    ),
                ),
            ),
            SourceRevisionEvidence(
                source=testing,
                projection_hash="5" * 64,
                content_hash="b" * 64,
                messages=(
                    SourceMessageEvidence(
                        ref=test_call_ref,
                        role="ai",
                        content="Run the requirement assertion.",
                        tool_calls=(
                            {"id": "call-test-R5", "name": "pytest", "args": {"test": "R5"}},
                        ),
                    ),
                    SourceMessageEvidence(
                        ref=test_ref,
                        role="tool",
                        content="Expected lock after the third failure; observed no lock.",
                        tool_call_id="call-test-R5",
                        name="pytest",
                        status="error",
                    ),
                ),
            ),
        ),
        structured=(
            StructuredEvidence(
                ref=requirement_ref,
                content="R3: lock the account after the third failed attempt.",
            ),
            StructuredEvidence(
                ref=failure_ref,
                content={"error": "account remained unlocked", "attempt": 3},
            ),
        ),
        evidence_frontier=(requirement_ref, implementation_ref, test_call_ref, test_ref, failure_ref),
    )
    refs = {
        "R3": (requirement_ref, "3" * 64),
        "R8": (implementation_ref, "a" * 64),
        "R5": (test_ref, "b" * 64),
        "F2": (failure_ref, "2" * 64),
    }
    items = tuple(
        ResolvedEvidenceItem(
            requirement_id=requirement_id,
            ref=ref,
            content_hash=content_hash,
            relevance_reason=f"Directly satisfies {requirement_id}.",
        )
        for requirement_id, (ref, content_hash) in refs.items()
    )
    return spec, evidence, items


def single_source_fixture() -> MultiSourceEvidence:
    source = revision_ref("single")
    ref = message_ref(source, "continuation")
    return MultiSourceEvidence(
        sources=(
            SourceRevisionEvidence(
                source=source,
                projection_hash="c" * 64,
                content_hash="d" * 64,
                messages=(SourceMessageEvidence(ref=ref, role="human", content="Continue from this verified state."),),
            ),
        ),
        evidence_frontier=(ref,),
    )


def duplicate_message_id_fixture() -> MultiSourceEvidence:
    left = revision_ref("duplicate-left")
    right = revision_ref("duplicate-right")
    left_ref = message_ref(left, "shared")
    right_ref = message_ref(right, "shared")
    return MultiSourceEvidence(
        sources=(
            SourceRevisionEvidence(
                source=left,
                projection_hash="1" * 64,
                content_hash="2" * 64,
                messages=(SourceMessageEvidence(ref=left_ref, role="human", content="Left source."),),
            ),
            SourceRevisionEvidence(
                source=right,
                projection_hash="3" * 64,
                content_hash="4" * 64,
                messages=(SourceMessageEvidence(ref=right_ref, role="human", content="Right source."),),
            ),
        ),
        evidence_frontier=(left_ref, right_ref),
    )


def incomplete_tool_exchange_fixture() -> MultiSourceEvidence:
    source = revision_ref("incomplete-tool")
    caller = message_ref(source, "caller")
    return MultiSourceEvidence(
        sources=(
            SourceRevisionEvidence(
                source=source,
                projection_hash="5" * 64,
                content_hash="6" * 64,
                messages=(
                    SourceMessageEvidence(
                        ref=caller,
                        role="ai",
                        content="Read the file.",
                        tool_calls=({"id": "call-missing", "name": "read_file", "args": {"path": "a.py"}},),
                    ),
                ),
            ),
        ),
        evidence_frontier=(caller,),
    )
