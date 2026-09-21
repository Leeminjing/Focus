r"""本文件对外提供 deterministic Context expansion compiler 的协议闭包与失败结果测试。

输入为冻结 opportunity、semantic spawn intent、不可变 Revision 摘要和含 Tool Exchange 的消息；输出为稳定内部
CreateLanePlan、相同 definition hash 或结构化 compiler blocker。具体工作流为只调用纯 compiler，不连接数据库或提交
Portfolio。示例：`pytest backend/tests/test_context_expansion_compiler.py`。
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.app.desktop.agent_loop.context_expansion.compiler import ContextExpansionPlanCompiler, DeterministicExpansionPlanCompiler
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.agent_loop.context_expansion.contracts import ExpansionBlocker, ExpansionOpportunity, SpawnContextIntent
from backend.app.desktop.context_curation import ComposeMessage, ToolExchange
from backend.app.desktop.context_evolution import ContextRevisionPayloadMode, ContextRevisionRef


def _source() -> ContextRevisionRef:
    return ContextRevisionRef(
        context_id="context-source",
        revision_id="revision-source",
        generation=2,
        execution_thread_id="thread-source",
        checkpoint_ns="",
        checkpoint_id="checkpoint-source",
        payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
    )


def _opportunity() -> ExpansionOpportunity:
    return ExpansionOpportunity.create(
        loop_id="loop-compiler",
        round_id="round-compiler",
        source=_source(),
        purpose="Independent test verification",
        work_order="Inspect the test evidence and reproduce failures.",
        completion_check="Report the exact passing and failing tests.",
        workspace_mode="read_only",
        independence_key="verification:tests",
        triggers=("independent_verification",),
        evidence_hints=("tool-result-one",),
        required=True,
    )


def _intent(opportunity: ExpansionOpportunity) -> SpawnContextIntent:
    return SpawnContextIntent(opportunity_id=opportunity.opportunity_id)


def _messages() -> tuple[dict, ...]:
    return (
        {"id": "human-root", "role": "human", "content": "Run both focused checks."},
        {
            "id": "assistant-tools",
            "role": "ai",
            "content": "",
            "tool_calls": (
                {"id": "call-one", "name": "shell", "args": {"command": "pytest one"}},
                {"id": "call-two", "name": "shell", "args": {"command": "pytest two"}},
            ),
        },
        {"id": "tool-result-one", "role": "tool", "content": "1 passed", "tool_call_id": "call-one", "name": "shell", "status": "success"},
        {"id": "tool-result-two", "role": "tool", "content": "1 failed", "tool_call_id": "call-two", "name": "shell", "status": "error"},
    )


def test_compiler_closes_complete_tool_exchange_and_is_deterministic() -> None:
    opportunity = _opportunity()
    compiler = DeterministicExpansionPlanCompiler()
    revision = SimpleNamespace(projection_hash="a" * 64, content_hash="b" * 64)

    first = compiler.compile(opportunity, _intent(opportunity), revision, _messages())
    second = compiler.compile(opportunity, _intent(opportunity), revision, _messages())

    assert not isinstance(first, ExpansionBlocker)
    assert first == second
    exchange = next(item for item in first.plan.items if isinstance(item, ToolExchange))
    assert {reference.message_id for reference in exchange.sources} == {
        "assistant-tools",
        "tool-result-one",
        "tool-result-two",
    }
    assert tuple(call.name for call in exchange.calls) == ("shell", "shell")
    assert first.plan.lane_policy["independence_key"] == "verification:tests"


def test_compiled_plan_carries_the_frozen_work_order() -> None:
    opportunity = _opportunity()
    revision = SimpleNamespace(projection_hash="a" * 64, content_hash="b" * 64)

    compiled = DeterministicExpansionPlanCompiler().compile(opportunity, _intent(opportunity), revision, _messages())

    assert not isinstance(compiled, ExpansionBlocker)
    composed = next(item for item in compiled.plan.items if isinstance(item, ComposeMessage))
    assert opportunity.work_order in composed.content
    assert opportunity.completion_check in composed.content
    assert opportunity.workspace_mode in composed.content
    assert compiled.plan.purpose == opportunity.purpose
    assert compiled.plan.lane_policy["completion_check"] == opportunity.completion_check
    assert compiled.plan.lane_policy["workspace_mode"] == opportunity.workspace_mode


def test_compiler_returns_stable_blocker_for_incomplete_protocol() -> None:
    opportunity = _opportunity()
    compiler = DeterministicExpansionPlanCompiler()
    revision = SimpleNamespace(projection_hash="a" * 64, content_hash="b" * 64)

    outcome = compiler.compile(opportunity, _intent(opportunity), revision, _messages()[:-1])

    assert isinstance(outcome, ExpansionBlocker)
    assert outcome.code == "compiler_failed"
    assert outcome.opportunity_id == opportunity.opportunity_id


def test_source_validation_rejects_stale_and_out_of_scope_without_fallback() -> None:
    opportunity = _opportunity()
    observation = LoopObservationEnvelope(
        loop_id="loop-compiler",
        loop_revision=1,
        round_id="round-compiler",
        goal_revision=1,
        authority_revision=1,
        observed_frontier_hash="c" * 64,
        mission={"outcome": "Verify"},
        grant={"context_scope": [opportunity.source.context_id]},
        portfolio_frontier=(
            {
                "context_id": opportunity.source.context_id,
                "revision_id": "revision-newer",
                "checkpoint_id": "checkpoint-newer",
            },
        ),
        workspace={"revision": 1},
        budget={},
    )

    stale = ContextExpansionPlanCompiler._validate_frozen_source(observation, opportunity, _intent(opportunity))
    scoped = ContextExpansionPlanCompiler._validate_frozen_source(
        observation.model_copy(
            update={
                "portfolio_frontier": (
                    {
                        "context_id": opportunity.source.context_id,
                        "revision_id": opportunity.source.revision_id,
                        "checkpoint_id": opportunity.source.checkpoint_id,
                    },
                ),
                "grant": {"context_scope": ["context-other"]},
            }
        ),
        opportunity,
        _intent(opportunity),
    )

    assert stale is not None and stale.code == "stale_source"
    assert scoped is not None and scoped.code == "source_out_of_scope"
