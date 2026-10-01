r"""本文件对外提供证据身份合并与逐次模型调用计费的边界回归。

输入为不同合法引文、独立 verdict、预算化模型和隔离 PostgreSQL；输出为完整 proof、唯一最终 unit 与准确持久用量断言。
工作流为先独立验证局部／综合证据，再发布完整 Index；模拟回读后预算或配置拒绝，核对实际发送及幂等结算。
示例：python -m pytest backend/tests/test_index_publication_integrity.py -q；取消和验证失败仍计已发送调用。
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    stable_expansion_hash,
)
from backend.app.desktop.agent_loop.context_expansion.index_assembly import (
    assemble_revision_index,
)
from backend.app.desktop.agent_loop.context_expansion.index_build_contracts import (
    IndexBuildPlan,
)
from backend.app.desktop.agent_loop.context_expansion.index_model_budget import (
    BudgetedIndexModel,
    IndexModelBudget,
)
from backend.app.desktop.agent_loop.context_expansion.interpretation_inputs import (
    FrozenInterpretationInputs,
)
from backend.app.desktop.agent_loop.context_expansion.interpretation_record import (
    RevisionInterpretationRecord,
)
from backend.app.desktop.agent_loop.context_expansion.models import (
    LoopIndexBudgetReservation,
    LoopSemanticIndexArtifact,
)
from backend.app.desktop.agent_loop.context_expansion.projection_record_repository import (
    ProjectionRecordRepository,
)
from backend.app.desktop.agent_loop.context_expansion.segment_projection import (
    SegmentProjectionRecord,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_grounding import (
    SegmentSemanticUnitDraft,
    SemanticClaimSupportAssessment,
    SemanticSupportSpan,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_index import (
    RevisionSemanticIndex,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_indexer import (
    RevisionSemanticIndexer,
)
from backend.app.desktop.agent_loop.models import LoopBudgetUsage
from backend.app.desktop.context_evolution import ContextRevisionRef
from backend.tests.incremental_index_support import observation, revision
from backend.tests.test_incremental_revision_index import exercise
from backend.tests.test_revision_interpretation import ControlledInterpretationProbe

TEXT = "The connection pool was ruled out. Investigation: the connection pool was ruled out."
STATEMENT = "The connection pool was ruled out."


def _draft(quote, *, statement=STATEMENT):
    return SegmentSemanticUnitDraft(
        kind="claim",
        authority="confirmed",
        statement=statement,
        supports=(SemanticSupportSpan(message_id="m1", quote=quote),),
    )


def _assessment(draft, verdict="supported"):
    return SemanticClaimSupportAssessment(
        claim_key=draft.claim_key,
        verdict=verdict,
        reason="independent quote verification",
    )


@pytest.mark.parametrize("verdict", ["supported", "unsupported", "unknown"])
def test_different_quotes_preserve_independent_proof_and_unique_final_unit(verdict):
    local = _draft(STATEMENT)
    joint = _draft("Investigation: the connection pool was ruled out.")
    assert local.claim_key != joint.claim_key
    source = ContextRevisionRef(
        context_id="verify-context",
        revision_id="verify-revision",
        generation=1,
        execution_thread_id="verify-thread",
        checkpoint_ns="",
        checkpoint_id="verify-cp",
        payload_mode="checkpoint",
    )
    contract = (
        "rev:"
        + stable_expansion_hash(
            "revision-projection-contract-v1", "loc:verify", "syn:verify"
        )[:60]
    )
    indexer = RevisionSemanticIndexer()
    frozen = indexer.index(
        source=source,
        source_content_hash="f" * 64,
        context_role="primary",
        active_objective="verify",
        raw_messages=({"id": "m1", "role": "human", "content": TEXT},),
        projector_version=contract,
    )
    record = SegmentProjectionRecord.create(
        context_id=source.context_id,
        contract="loc:verify",
        segment=frozen.segments[0],
        messages=frozen.messages,
        drafts=(local,),
        assessments=(_assessment(local),),
    )
    inputs = FrozenInterpretationInputs(frozen, (record,), max_reads=1)
    inputs.read((frozen.segments[0].segment_id,), requested=False)
    proof = RevisionInterpretationRecord.create(
        context_id=source.context_id,
        source_content_hash=frozen.source_content_hash,
        contract_fingerprint="syn:verify",
        local_contract_fingerprint="loc:verify",
        inventory=inputs.inventory,
        provided=inputs.provided,
        drafts=(joint,),
        assessments=(_assessment(joint, verdict),),
    )
    index = assemble_revision_index(
        indexer,
        frozen,
        (record,),
        IndexBuildPlan(recomputed_segment_ids=(frozen.segments[0].segment_id,)),
        interpretation=proof,
        local_contract="loc:verify",
    )
    assert len(index.semantic_units) == len(index.projected_unit_ids) == 1
    assert index.semantic_units[0].statement == STATEMENT
    assert index.semantic_units[0].authority == "confirmed"
    assert record.drafts == (local,) and index.interpretation.drafts == (joint,)
    assert index.interpretation.assessments == (_assessment(joint, verdict),)
    assert bool(proof.accepted_claim_keys) == (verdict == "supported")
    assert index.quality_state == ("complete" if verdict == "supported" else "degraded")
    assert RevisionSemanticIndex.model_validate(index.model_dump(mode="json")) == index


class _AttemptModel:
    def __init__(self, *, guarded=False, native=False):
        self.cache_identity = "initial"
        self.last_attempt_records = ()
        self.calls = 0
        self.guard = None
        self.started = None
        if guarded:
            self.bind_request_guard = self._bind
        if native:
            self.invoke_validated = self._validated

    def _bind(self, guard):
        self.guard = guard

    async def invoke(self, schema, system, payload):
        self.last_attempt_records = ()
        if self.guard:
            await self.guard(self, schema, system, payload)
        self.calls += 1
        self.last_attempt_records = (
            {"model_calls": 1, "input_tokens": 11, "output_tokens": 7},
        )
        if self.started:
            self.started.set()
            await asyncio.Event().wait()
        return schema.model_validate(payload)

    async def _validated(self, schema, system, payload, validator):
        result = await self.invoke(schema, system, payload)
        validator(result)
        return result


def _budget(calls):
    return IndexModelBudget(
        SimpleNamespace(
            global_model_calls_remaining=calls,
            global_input_tokens_remaining=1000000,
            global_output_tokens_remaining=1000000,
            policy=SimpleNamespace(
                output_token_reserve=100, max_request_input_tokens=1000000
            ),
        )
    )


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("validated", [False, True])
@pytest.mark.parametrize("failure", ["budget", "config"])
def test_rejected_invocation_cannot_replay_previous_attempt(
    guarded, native, validated, failure
):
    async def run():
        model = _AttemptModel(guarded=guarded, native=native)
        wrapper = BudgetedIndexModel(model, _budget(1))

        async def invoke():
            if validated:
                return await wrapper.invoke_validated(
                    IndexBuildPlan, "test", {}, lambda result: None
                )
            return await wrapper.invoke(IndexBuildPlan, "test", {})

        await invoke()
        first = wrapper.last_attempt_records
        assert first and model.calls == 1
        if failure == "config":
            model.cache_identity = "changed"
        with pytest.raises(
            ValueError, match="stale" if failure == "config" else "exhausted"
        ):
            await invoke()
        assert wrapper.last_attempt_records == ()
        assert model.calls == 1 and first[0]["model_calls"] == 1

    asyncio.run(run())


@pytest.mark.parametrize("native", [False, True])
def test_validation_failure_and_identical_real_attempts_remain_billable(native):
    async def run():
        model = _AttemptModel(native=native)
        wrapper = BudgetedIndexModel(model, _budget(3))
        captured = []

        def reject(result):
            raise ValueError("invalid result")

        for validator in (lambda result: None, reject, lambda result: None):
            try:
                await wrapper.invoke_validated(IndexBuildPlan, "test", {}, validator)
            except ValueError as exc:
                assert str(exc) == "invalid result"
            captured.extend(wrapper.last_attempt_records)
        assert model.calls == len(captured) == 3
        assert all(row == captured[0] for row in captured)
        assert sum(row["input_tokens"] for row in captured) == 33

    asyncio.run(run())


def test_cancelled_sent_attempt_is_captured_and_cancellation_propagates():
    async def run():
        model = _AttemptModel()
        model.started = asyncio.Event()
        wrapper = BudgetedIndexModel(model, _budget(1))
        task = asyncio.create_task(wrapper.invoke(IndexBuildPlan, "test", {}))
        await asyncio.wait_for(model.started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert model.calls == 1 and wrapper.last_attempt_records[0]["model_calls"] == 1

    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_portfolio_publishes_colliding_quotes_with_both_original_proofs(tmp_path):
    async def run(sessions, seed):
        joint = _draft(STATEMENT, statement=TEXT)
        probe = ControlledInterpretationProbe(
            lambda payload: {"action": "complete", "units": (joint,)}
        )
        target = await revision(
            sessions,
            seed["context_id"],
            [{"id": "m1", "role": "human", "content": TEXT}],
        )
        result = await probe.service(sessions).build(observation(seed, target))
        assert result.blocker_code is None, result.blocker_summary
        index = result.indexes[0]
        assert index.quality_state == "complete" and len(index.semantic_units) == 1
        async with sessions() as session:
            stored = await session.get(LoopSemanticIndexArtifact, index.index_id)
            records = await ProjectionRecordRepository().by_ids(
                session, target.ref.context_id, index.inheritance.record_ids
            )
            used = await session.get(LoopBudgetUsage, seed["loop_id"])
        assert stored and RevisionSemanticIndex.model_validate(stored.payload) == index
        local = records[0].drafts[0]
        assert local.claim_key != joint.claim_key and local.supports[0].quote == TEXT
        assert index.interpretation.drafts == (joint,)
        assert used.model_calls == len(probe.calls) == 4

    exercise(tmp_path, run)


@pytest.mark.usefixtures("isolated_postgres_database")
@pytest.mark.parametrize("failure", ["budget", "config"])
def test_rejected_second_read_settles_only_actual_calls_and_tokens(tmp_path, failure):
    async def run(sessions, seed):
        def reply(payload):
            if failure == "config":
                probe.version = "changed"
            return {
                "action": "read",
                "read_segments": (payload["inventory"][0]["segment_id"],),
            }

        probe = ControlledInterpretationProbe(reply)
        service = probe.service(sessions)
        target = await revision(
            sessions,
            seed["context_id"],
            [{"id": "m1", "role": "human", "content": TEXT}],
        )
        obs = observation(
            seed, target, budget={"max_model_calls": 3} if failure == "budget" else None
        )
        result = await service.build(obs)
        assert result.blocker_code and len(probe.calls) == 3
        expected_input = sum(
            len(json.dumps(payload).encode()) // 4 for _, payload in probe.calls
        )
        async with sessions() as session:
            used = await session.get(LoopBudgetUsage, seed["loop_id"])
            rows = (
                await session.scalars(
                    select(LoopIndexBudgetReservation).where(
                        LoopIndexBudgetReservation.loop_id == seed["loop_id"]
                    )
                )
            ).all()
            assert (
                await session.scalar(
                    select(LoopSemanticIndexArtifact.index_id).where(
                        LoopSemanticIndexArtifact.revision_id == target.ref.revision_id
                    )
                )
                is None
            )
        assert used.model_calls == 3
        assert used.input_tokens == expected_input and used.output_tokens == 30
        assert len(rows) == 1 and rows[0].settled_at is not None
        assert rows[0].actual_usage["model_calls"] == 3
        assert rows[0].actual_usage["input_tokens"] == expected_input
        if failure == "budget":
            assert (await service.build(obs)).blocker_code
            assert len(probe.calls) == 3
            async with sessions() as session:
                used = await session.get(LoopBudgetUsage, seed["loop_id"])
                assert used.model_calls == 3 and used.input_tokens == expected_input

    exercise(tmp_path, run)
