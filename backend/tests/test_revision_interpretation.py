r"""本文件对外提供跨段诊断与冻结原文理解的真实 Portfolio 回归。

输入为隔离 PostgreSQL、四段诊断时间线和观测模型；输出为联合原文调用、独立 verdict 与目标来源断言。
工作流为发布真实 Revision，逐段保留局部证据，综合调用共同读取远距离原文并生成联合 unit。
示例：python -m pytest backend/tests/test_revision_interpretation.py -q。
"""

import pytest

from backend.app.desktop.agent_loop.context_expansion.semantic_grounding import (
    SegmentSemanticUnitDraft,
    SemanticSupportSpan,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_indexer import (
    ProtocolSafeRevisionSegmenter,
    RevisionSemanticIndexer,
)
from backend.tests.incremental_index_support import ModelProbe, observation, revision
from backend.tests.test_incremental_revision_index import exercise

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")

NARRATIVE = [
    {"id": "symptom", "role": "human", "content": "HTTP 500 occurred."},
    {
        "id": "suspect",
        "role": "human",
        "content": "Initially we suspected the connection pool.",
    },
    {
        "id": "reject",
        "role": "human",
        "content": "The investigation ruled out the connection pool.",
    },
    {
        "id": "diagnosis",
        "role": "human",
        "content": "A thread-pool deadlock was confirmed as the cause.",
    },
]
STATEMENT = "The early connection-pool suspicion was ruled out; the confirmed cause was thread-pool deadlock."


class NarrativeProbe(ModelProbe):
    def factory(self, role):
        base = super().factory(role)
        probe = self

        class Model(base):
            async def invoke(self, schema, system, payload):
                if schema.__name__ != "RevisionInterpretationProposal":
                    return await super().invoke(schema, system, payload)
                probe.calls.append(("interpretation", payload))
                self.last_attempt_records = (
                    {
                        "role": "interpretation",
                        "model_calls": 1,
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "attempt": 1,
                    },
                )
                supplied = {
                    m["message_id"] for s in payload["segments"] for m in s["messages"]
                }
                inventory_ids = {
                    m for s in payload["inventory"] for m in s["message_ids"]
                }
                if "diagnosis" not in inventory_ids:
                    return schema(action="complete", units=())
                needed = {m["id"] for m in NARRATIVE[1:]}
                if not needed.issubset(supplied):
                    return schema(
                        action="read",
                        read_segments=tuple(
                            s["segment_id"]
                            for s in payload["inventory"]
                            if set(s["message_ids"]) & needed
                        ),
                    )
                return schema(
                    action="complete",
                    units=(
                        SegmentSemanticUnitDraft(
                            kind="claim",
                            authority="confirmed",
                            statement=STATEMENT,
                            supports=tuple(
                                SemanticSupportSpan(
                                    message_id=m["id"], quote=m["content"]
                                )
                                for m in NARRATIVE[1:]
                            ),
                        ),
                    ),
                )

        return Model


def test_four_segment_diagnosis_is_jointly_projected_and_verified(tmp_path):
    async def run(sessions, seed):
        probe = NarrativeProbe()
        service = probe.service(sessions)
        service._indexer = RevisionSemanticIndexer(
            ProtocolSafeRevisionSegmenter(max_messages=1)
        )
        target = await revision(sessions, seed["context_id"], NARRATIVE)
        result = await service.build(observation(seed, target))
        assert result.blocker_code is None, result.blocker_summary
        index = result.indexes[0]
        joint = [u for u in index.semantic_units if u.statement == STATEMENT]
        assert len(joint) == 1, (
            "Projection lost cross-segment hypothesis/rejection/diagnosis interpretation"
        )
        assert {r.message_id for r in joint[0].evidence_refs} == {
            "suspect",
            "reject",
            "diagnosis",
        }
        assert all(r.source == target.ref for r in joint[0].evidence_refs)
        assert any(
            len(p["segments"]) > 1
            for role, p in probe.calls
            if role == "interpretation"
        )
        assert any(
            c["statement"] == STATEMENT and len(c["supports"]) == 3
            for role, p in probe.calls
            if role == "verifier"
            for c in p["claims"]
        )

    exercise(tmp_path, run)


class ControlledInterpretationProbe(ModelProbe):
    def __init__(self, reply, **kwargs):
        super().__init__(**kwargs)
        self.reply = reply

    def factory(self, role):
        import inspect
        import json

        base = super().factory(role)
        probe = self

        class Model(base):
            async def invoke(self, schema, system, payload):
                if schema.__name__ != "RevisionInterpretationProposal":
                    return await super().invoke(schema, system, payload)
                probe.calls.append(("interpretation", payload))
                self.last_attempt_records = (
                    {
                        "role": "interpretation",
                        "model_calls": 1,
                        "input_tokens": len(json.dumps(payload).encode()) // 4,
                        "output_tokens": 10,
                        "attempt": 1,
                    },
                )
                reply = probe.reply(payload)
                if inspect.isawaitable(reply):
                    reply = await reply
                return schema.model_validate(reply)

        return Model


def narrative_service(probe, sessions, **kwargs):
    service = probe.service(sessions, **kwargs)
    service._indexer = RevisionSemanticIndexer(
        ProtocolSafeRevisionSegmenter(max_messages=1)
    )
    return service


def joint_response(
    payload, *, wanted=NARRATIVE[1:], statement=STATEMENT, authority="confirmed"
):
    supplied = {m["message_id"] for s in payload["segments"] for m in s["messages"]}
    required = {m["id"] for m in wanted}
    if not required.issubset(supplied):
        return {
            "action": "read",
            "read_segments": tuple(
                s["segment_id"]
                for s in payload["inventory"]
                if set(s["message_ids"]) & required
            ),
        }
    return {
        "action": "complete",
        "units": (
            SegmentSemanticUnitDraft(
                kind="claim",
                authority=authority,
                statement=statement,
                supports=tuple(
                    SemanticSupportSpan(message_id=m["id"], quote=m["content"])
                    for m in wanted
                ),
            ),
        ),
    }


@pytest.mark.parametrize("gap", [0, 40])
def test_append_discovery_expands_nonadjacent_originals_and_refreshes_empty_result(
    tmp_path, gap
):
    async def run(sessions, seed):
        probe = NarrativeProbe()
        service = narrative_service(probe, sessions)
        filler = [
            {
                "id": f"unrelated-{i}",
                "role": "human",
                "content": f"UI batch {i} completed.",
            }
            for i in range(gap)
        ]
        first = await revision(sessions, seed["context_id"], NARRATIVE[:2] + filler)
        old = (await service.build(observation(seed, first))).indexes[0]
        assert old.interpretation.completed and not old.interpretation.drafts
        probe.calls.clear()
        target = await revision(
            sessions,
            seed["context_id"],
            NARRATIVE[:2] + filler + NARRATIVE[2:],
            parent=first,
        )
        current = await service.build(observation(seed, target))
        assert current.blocker_code is None, current.blocker_summary
        index = current.indexes[0]
        assert len(index.inheritance.reused_segment_ids) == 2 + gap
        assert len(probe.local_calls) == 5
        assert len(probe.interpretation_calls) == 2
        initial, expanded = [p for _, p in probe.interpretation_calls]
        assert len(initial["inventory"]) == gap + 4
        assert {
            m["message_id"] for s in initial["segments"] for m in s["messages"]
        } == {"reject", "diagnosis"}
        assert {
            m["message_id"] for s in expanded["segments"] for m in s["messages"]
        } >= {"suspect", "reject", "diagnosis"}
        assert any(u.statement == STATEMENT for u in index.semantic_units)
        assert old.interpretation != index.interpretation
        reference_probe = NarrativeProbe(version="full-original-reference")
        reference = await narrative_service(reference_probe, sessions).build(
            observation(seed, target)
        )
        assert reference.blocker_code is None, reference.blocker_summary
        assert [
            u for u in reference.indexes[0].semantic_units if u.statement == STATEMENT
        ] == [u for u in index.semantic_units if u.statement == STATEMENT]
        assert len(reference_probe.interpretation_calls[0][1]["segments"]) == gap + 4
        probe.calls.clear()
        assert (await service.build(observation(seed, target))).indexes[0] == index
        assert probe.calls == []

    exercise(tmp_path, run)


@pytest.mark.parametrize(
    "case", ["omitted_summary", "pronoun", "different_words", "old_old"]
)
def test_discovery_is_not_gated_by_local_summary_or_keywords(tmp_path, case):
    async def run(sessions, seed):
        left = {
            "id": "left",
            "role": "human",
            "content": "Worker Alpha held the scheduler mutex while awaiting completion.",
        }
        right = {
            "id": "right",
            "role": "human",
            "content": "It waited forever because the callback needed the same lock.",
        }
        statement = "Worker Alpha and its callback formed a mutex deadlock."
        if case == "different_words":
            left["content"] = "Connection cache X was suspected of causing HTTP 500."
            right["content"] = (
                "That guess was disproved: executor starvation explains the server failure."
            )
            statement = "The earlier connection-cache suspicion was disproved; executor starvation explains the server failure."
        if case == "omitted_summary":
            left["content"] += " The missing callback was still waiting for Alpha."
        filler = [
            {"id": f"filler-{i}", "role": "human", "content": "Unrelated output."}
            for i in range(20)
        ]
        old_history = [left] + filler + ([right] if case == "old_old" else [])
        ready = False

        def reply(payload):
            if not ready:
                return {"action": "complete", "units": ()}
            return joint_response(
                payload,
                wanted=(left, right),
                statement=statement,
            )

        probe = ControlledInterpretationProbe(
            reply, invalid="empty" if case == "omitted_summary" else None
        )
        service = narrative_service(probe, sessions)
        first = await revision(sessions, seed["context_id"], old_history)
        before = await service.build(observation(seed, first))
        assert before.blocker_code is None, before.blocker_summary
        ready = True
        appended = (
            [
                {
                    "id": "irrelevant-tail",
                    "role": "human",
                    "content": "A new unrelated batch finished.",
                }
            ]
            if case == "old_old"
            else [right]
        )
        target = await revision(
            sessions, seed["context_id"], old_history + appended, parent=first
        )
        probe.calls.clear()
        result = await service.build(observation(seed, target))
        assert result.blocker_code is None, result.blocker_summary
        units = [
            u for u in result.indexes[0].semantic_units if u.statement == statement
        ]
        assert len(units) == 1
        assert {r.message_id for r in units[0].evidence_refs} == {"left", "right"}
        assert result.indexes[0].interpretation.read_requests
        assert len(probe.interpretation_calls[0][1]["inventory"]) == len(
            old_history + appended
        )
        if case == "omitted_summary":
            assert all(not e.claims for e in result.indexes[0].interpretation.inventory)
            assert result.indexes[0].quality_state == "degraded"
        reference_probe = ControlledInterpretationProbe(
            reply,
            invalid="empty" if case == "omitted_summary" else None,
            version="full-original-reference",
        )
        reference = await narrative_service(reference_probe, sessions).build(
            observation(seed, target)
        )
        assert reference.blocker_code is None, reference.blocker_summary
        assert [
            u for u in reference.indexes[0].semantic_units if u.statement == statement
        ] == units
        assert len(reference_probe.interpretation_calls[0][1]["segments"]) == len(
            old_history + appended
        )

    exercise(tmp_path, run)


@pytest.mark.parametrize("verdict", ["unsupported", "unknown", "missing"])
def test_valid_quotes_do_not_automatically_confirm_a_joint_claim(tmp_path, verdict):
    async def run(sessions, seed):
        probe = NarrativeProbe(verdict=verdict if verdict != "missing" else "supported")
        kwargs = (
            {"semantic_claim_verifier_factory": None} if verdict == "missing" else {}
        )
        service = narrative_service(probe, sessions, **kwargs)
        target = await revision(sessions, seed["context_id"], NARRATIVE)
        result = await service.build(observation(seed, target))
        assert result.blocker_code is None, result.blocker_summary
        index = result.indexes[0]
        assert not any(
            u.statement == STATEMENT and u.authority == "confirmed"
            for u in index.semantic_units
        )
        assert index.interpretation.rejections and index.quality_state == "degraded"

    exercise(tmp_path, run)


@pytest.mark.parametrize("invalid", ["invented_quote", "unsupplied_original"])
def test_summaries_and_unread_messages_are_not_original_evidence(tmp_path, invalid):
    async def run(sessions, seed):
        ready = False

        def reply(payload):
            if not ready:
                return {"action": "complete", "units": ()}
            draft = SegmentSemanticUnitDraft(
                kind="claim",
                authority="confirmed",
                statement=STATEMENT,
                supports=(
                    SemanticSupportSpan(
                        message_id="suspect",
                        quote="summary wording"
                        if invalid == "invented_quote"
                        else NARRATIVE[1]["content"],
                    ),
                ),
            )
            return {"action": "complete", "units": (draft,)}

        probe = ControlledInterpretationProbe(reply)
        service = narrative_service(probe, sessions)
        first = None
        if invalid == "unsupplied_original":
            first = await revision(sessions, seed["context_id"], NARRATIVE[:2])
            await service.build(observation(seed, first))
        ready = True
        target = await revision(sessions, seed["context_id"], NARRATIVE, parent=first)
        result = await service.build(observation(seed, target))
        assert result.blocker_code is None, result.blocker_summary
        record = result.indexes[0].interpretation
        assert record.accepted_claim_keys == () and record.rejections[0].code == (
            "semantic_support_error"
            if invalid == "invented_quote"
            else "unknown_message_ref"
        )
        assert result.indexes[0].quality_state == "degraded"

    exercise(tmp_path, run)


@pytest.mark.parametrize(
    "failure",
    ["foreign_read", "round_limit", "read_limit", "shared_budget", "inventory_window"],
)
def test_required_interpretation_failure_cannot_publish_local_only_index(
    tmp_path, failure
):
    async def run(sessions, seed):
        from sqlalchemy import select

        from backend.app.desktop.agent_loop.context_expansion.models import (
            LoopSemanticIndexArtifact,
        )

        def reply(payload):
            return {
                "action": "read",
                "read_segments": ("foreign-revision-segment",)
                if failure == "foreign_read"
                else tuple(s["segment_id"] for s in payload["inventory"]),
            }

        probe = ControlledInterpretationProbe(reply)
        service = narrative_service(probe, sessions)
        history = (
            NARRATIVE
            if failure != "inventory_window"
            else [
                {
                    "id": f"big-{i}",
                    "role": "human",
                    "content": "An unrelated operation completed.",
                }
                for i in range(100)
            ]
        )
        policy = {"max_planner_model_calls": 2}
        if failure == "read_limit":
            policy["max_exact_reads"] = 1
        if failure == "inventory_window":
            policy["max_request_input_tokens"] = 8000
        budget = {"expansion_resources": policy}
        if failure == "shared_budget":
            budget["max_model_calls"] = 8
        target = await revision(sessions, seed["context_id"], history)
        result = await service.build(observation(seed, target, budget=budget))
        assert result.blocker_code == (
            "portfolio_index_failed"
            if failure == "foreign_read"
            else "portfolio_index_budget"
        ), result.blocker_summary
        assert not result.indexes
        async with sessions() as session:
            assert (
                await session.scalar(
                    select(LoopSemanticIndexArtifact.index_id).where(
                        LoopSemanticIndexArtifact.revision_id == target.ref.revision_id
                    )
                )
                is None
            )
        assert probe.local_calls
        if failure == "inventory_window":
            assert not probe.interpretation_calls

    exercise(tmp_path, run)


def test_interpretation_proof_checks_unquoted_read_context_and_units(tmp_path):
    async def run(sessions, seed):
        import copy

        from backend.app.desktop.agent_loop.context_expansion.contracts import (
            stable_expansion_hash,
        )
        from backend.app.desktop.agent_loop.context_expansion.interpretation_record import (
            RevisionInterpretationRecord,
        )
        from backend.app.desktop.agent_loop.context_expansion.semantic_index import (
            RevisionSemanticIndex,
        )

        target = await revision(sessions, seed["context_id"], NARRATIVE)
        index = (
            await narrative_service(NarrativeProbe(), sessions).build(
                observation(seed, target)
            )
        ).indexes[0]
        record = index.interpretation
        from backend.app.desktop.agent_loop.context_expansion.interpretation_inputs import (
            FrozenInterpretationInputs,
        )
        from backend.app.desktop.agent_loop.context_expansion.projection_record_repository import (
            ProjectionRecordRepository,
        )

        async with sessions() as session:
            records = await ProjectionRecordRepository().by_ids(
                session, target.ref.context_id, index.inheritance.record_ids
            )
        inputs = FrozenInterpretationInputs(index, records, max_reads=4)
        requested = (index.segments[0].segment_id,)
        inputs.read(requested)
        original = inputs.provided
        inputs.read(requested)
        assert inputs.provided == original and inputs.requests == (requested, requested)
        tampered = copy.deepcopy(record.model_dump(mode="json"))
        tampered["provided"][0]["messages"][0]["content"] = (
            "The unquoted symptom changed."
        )
        with pytest.raises(ValueError, match="record integrity mismatch"):
            RevisionInterpretationRecord.model_validate(tampered)
        tampered["record_id"] = stable_expansion_hash(
            record.SCHEMA_VERSION,
            {k: v for k, v in tampered.items() if k != "record_id"},
        )
        altered = RevisionInterpretationRecord.model_validate(tampered)
        with pytest.raises(ValueError, match="original content integrity"):
            altered.validate_target(index)
        bad_index = index.model_dump(mode="json")
        joint_ids = {u.unit_id for u in record.accepted_units(target.ref)}
        bad_index["semantic_units"] = [
            u for u in bad_index["semantic_units"] if u["unit_id"] not in joint_ids
        ]
        bad_index["projected_unit_ids"] = [
            u for u in bad_index["projected_unit_ids"] if u not in joint_ids
        ]
        with pytest.raises(ValueError, match="accepted unit inventory"):
            RevisionSemanticIndex.model_validate(bad_index)

    exercise(tmp_path, run)


def test_changed_interpretation_model_reuses_compatible_local_prefix(tmp_path):
    async def run(sessions, seed):
        local = ModelProbe()
        first = await revision(sessions, seed["context_id"], NARRATIVE[:3])
        a = narrative_service(
            local,
            sessions,
            semantic_interpreter_factory=ModelProbe(version="interp-1").factory(
                "interpreter"
            ),
        )
        assert (await a.build(observation(seed, first))).blocker_code is None
        local.calls.clear()
        target = await revision(sessions, seed["context_id"], NARRATIVE, parent=first)
        next_interpreter = ModelProbe(version="interp-2")
        b = narrative_service(
            local,
            sessions,
            semantic_interpreter_factory=next_interpreter.factory("interpreter"),
        )
        result = await b.build(observation(seed, target))
        assert result.blocker_code is None, result.blocker_summary
        assert len(result.indexes[0].inheritance.reused_segment_ids) == 3
        assert (
            len(local.local_calls) == 2
            and len(next_interpreter.interpretation_calls) == 1
        )

    exercise(tmp_path, run)


@pytest.mark.parametrize("changed", ["prompt", "schema", "policy", "verifier"])
def test_interpretation_contract_changes_cannot_hit_old_target(
    tmp_path, monkeypatch, changed
):
    async def run(sessions, seed):
        from backend.app.desktop.agent_loop.context_expansion import (
            portfolio_index,
            revision_interpretation,
        )
        from backend.app.desktop.agent_loop.context_expansion.interpretation_record import (
            RevisionInterpretationProposal,
        )

        probe = ModelProbe()
        service = narrative_service(probe, sessions)
        target = await revision(sessions, seed["context_id"], NARRATIVE)
        old = (await service.build(observation(seed, target))).indexes[0]
        budget = None
        if changed == "prompt":
            prompt = (
                revision_interpretation.INTERPRETATION_PROMPT
                + " Preserve temporal evidence."
            )
            monkeypatch.setattr(
                revision_interpretation, "INTERPRETATION_PROMPT", prompt
            )
            monkeypatch.setattr(portfolio_index, "INTERPRETATION_PROMPT", prompt)
        elif changed == "schema":
            schema = RevisionInterpretationProposal.model_json_schema()
            monkeypatch.setattr(
                RevisionInterpretationProposal,
                "model_json_schema",
                classmethod(
                    lambda cls: {**schema, "title": "UpdatedInterpretationSchema"}
                ),
            )
        elif changed == "policy":
            budget = {"expansion_resources": {"max_exact_reads": 99}}
        else:
            service._semantic_claim_verifier_factory = ModelProbe(
                version="new-verifier"
            ).factory("verifier")
        probe.calls.clear()
        current = await service.build(observation(seed, target, budget=budget))
        assert current.blocker_code is None, current.blocker_summary
        index = current.indexes[0]
        assert index.projector_version != old.projector_version
        assert (
            index.interpretation.contract_fingerprint
            != old.interpretation.contract_fingerprint
        )
        assert probe.interpretation_calls and index.interpretation.completed
        assert (
            index.interpretation.local_contract_fingerprint
            != old.interpretation.local_contract_fingerprint
        ) == (changed == "verifier")
        probe.calls.clear()
        assert (await service.build(observation(seed, target, budget=budget))).indexes[
            0
        ] == index
        assert not probe.calls

    exercise(tmp_path, run)


def test_cancellation_during_synthesis_settles_actual_usage_once(tmp_path):
    async def run(sessions, seed):
        import asyncio

        from sqlalchemy import select

        from backend.app.desktop.agent_loop.context_expansion.models import (
            LoopIndexBudgetReservation,
            LoopSemanticIndexArtifact,
        )
        from backend.app.desktop.agent_loop.models import LoopBudgetUsage

        started = asyncio.Event()

        async def reply(payload):
            started.set()
            await asyncio.Event().wait()

        probe = ControlledInterpretationProbe(reply)
        service = narrative_service(probe, sessions)
        target = await revision(sessions, seed["context_id"], NARRATIVE)
        task = asyncio.create_task(service.build(observation(seed, target)))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with sessions() as session:
            used = await session.get(LoopBudgetUsage, seed["loop_id"])
            assert used.model_calls == 9
            rows = (
                await session.scalars(
                    select(LoopIndexBudgetReservation).where(
                        LoopIndexBudgetReservation.loop_id == seed["loop_id"]
                    )
                )
            ).all()
            assert len(rows) == 1 and rows[0].settled_at is not None
            assert (
                await session.scalar(
                    select(LoopSemanticIndexArtifact.index_id).where(
                        LoopSemanticIndexArtifact.revision_id == target.ref.revision_id
                    )
                )
                is None
            )

    exercise(tmp_path, run)


def test_concurrent_combined_outputs_adopt_one_persisted_winner(tmp_path):
    async def run(sessions, seed):
        import asyncio

        from backend.app.desktop.agent_loop.context_expansion.artifact_repository import (
            SemanticDerivationArtifactRepository,
        )
        from backend.app.desktop.agent_loop.models import LoopBudgetUsage

        entered = 0
        ready = asyncio.Event()

        def make_reply(variant):
            async def reply(payload):
                nonlocal entered
                entered += 1
                if entered == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 5)
                return joint_response(payload, statement=STATEMENT + variant)

            return reply

        a = ControlledInterpretationProbe(make_reply(" A"), variant=" A")
        b = ControlledInterpretationProbe(make_reply(" B"), variant=" B")
        target = await revision(sessions, seed["context_id"], NARRATIVE)
        results = await asyncio.gather(
            narrative_service(a, sessions).build(observation(seed, target)),
            narrative_service(b, sessions).build(observation(seed, target)),
        )
        assert all(r.blocker_code is None for r in results), [
            r.blocker_summary for r in results
        ]
        assert (
            results[0].indexes == results[1].indexes
            and results[0].catalog == results[1].catalog
        )
        async with sessions() as session:
            assert (
                await SemanticDerivationArtifactRepository().indexes_by_ids(
                    session, (results[0].indexes[0].index_id,)
                )
                == results[0].indexes
            )
            assert (
                await session.get(LoopBudgetUsage, seed["loop_id"])
            ).model_calls == len(a.calls) + len(b.calls)

    exercise(tmp_path, run)


def test_curator_receives_an_already_synthesized_joint_diagnosis(tmp_path):
    async def run(sessions, seed):
        from backend.app.desktop.agent_loop.context_expansion.artifact_repository import (
            SemanticDerivationArtifactRepository,
        )
        from backend.app.desktop.agent_loop.patrol_runtime import (
            CuratorCoordinationStage,
        )
        from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope

        target = await revision(sessions, seed["context_id"], NARRATIVE)
        frozen = observation(seed, target)
        envelope = LoopObservationEnvelope(
            **vars(frozen), loop_revision=1, goal_revision=1, workspace={"revision": 1}
        )
        service = narrative_service(NarrativeProbe(), sessions)
        payload = await CuratorCoordinationStage(
            sessions, index_service=service
        )._derivation_input(envelope)
        async with sessions() as session:
            indexes = await SemanticDerivationArtifactRepository().indexes_by_ids(
                session, payload["semantic_index_ids"]
            )
        joint = [u for u in indexes[0].semantic_units if u.statement == STATEMENT]
        assert len(joint) == 1 and len(joint[0].evidence_refs) == 3
        assert indexes[0].interpretation.completed

    exercise(tmp_path, run)


def test_original_expansion_preserves_tool_exchange_and_frozen_pointer(tmp_path):
    async def run(sessions, seed):
        assistant = {
            "id": "call",
            "role": "assistant",
            "content": "Check the mutex.",
            "tool_calls": [{"id": "exchange", "name": "inspect", "args": {}}],
        }
        result_message = {
            "id": "result",
            "role": "tool",
            "content": "A worker held the mutex while awaiting its callback.",
            "tool_call_id": "exchange",
        }
        added = {
            "id": "later",
            "role": "human",
            "content": "That callback could not acquire the mutex, causing deadlock.",
        }
        ready = False
        advanced = False
        target = None

        async def reply(payload):
            nonlocal advanced
            if not ready:
                return {"action": "complete", "units": ()}
            if not advanced:
                await revision(
                    sessions,
                    seed["context_id"],
                    [
                        assistant,
                        result_message,
                        added,
                        {
                            "id": "future",
                            "role": "human",
                            "content": "A later pointer must not leak.",
                        },
                    ],
                    parent=target,
                )
                advanced = True
            return joint_response(
                payload,
                wanted=(result_message, added),
                statement="The worker's mutex dependency deadlocked its callback.",
            )

        probe = ControlledInterpretationProbe(reply)
        service = narrative_service(probe, sessions)
        first = await revision(
            sessions, seed["context_id"], [assistant, result_message]
        )
        assert (await service.build(observation(seed, first))).blocker_code is None
        target = await revision(
            sessions,
            seed["context_id"],
            [assistant, result_message, added],
            parent=first,
        )
        ready = True
        current = await service.build(observation(seed, target))
        assert current.blocker_code is None, current.blocker_summary
        index = current.indexes[0]
        assert index.source == target.ref and len(index.messages) == 3
        assert any(
            tuple(m["message_id"] for m in s.messages) == ("call", "result")
            for s in index.interpretation.provided
        )
        assert all(
            m["message_id"] != "future"
            for s in index.interpretation.provided
            for m in s.messages
        )

    exercise(tmp_path, run)


def test_later_denial_replaces_an_unchanged_earlier_overall_diagnosis(tmp_path):
    async def run(sessions, seed):
        old_message = {
            "id": "old-cause",
            "role": "human",
            "content": "At time one, connection-pool failure was diagnosed as the root cause.",
        }
        new_message = {
            "id": "new-cause",
            "role": "human",
            "content": "That diagnosis was disproved; thread-pool deadlock was instead confirmed.",
        }
        old_statement = "The currently reported root cause is connection-pool failure."
        new_statement = "The initial connection-pool diagnosis was disproved and replaced with thread-pool deadlock."

        def reply(payload):
            ids = {m for e in payload["inventory"] for m in e["message_ids"]}
            if "new-cause" not in ids:
                return joint_response(
                    payload, wanted=(old_message,), statement=old_statement
                )
            return joint_response(
                payload, wanted=(old_message, new_message), statement=new_statement
            )

        service = narrative_service(ControlledInterpretationProbe(reply), sessions)
        first = await revision(sessions, seed["context_id"], [old_message])
        old = (await service.build(observation(seed, first))).indexes[0]
        target = await revision(
            sessions, seed["context_id"], [old_message, new_message], parent=first
        )
        current = (await service.build(observation(seed, target))).indexes[0]
        assert old_statement in {u.statement for u in old.semantic_units}
        assert old_statement not in {u.statement for u in current.semantic_units}
        assert new_statement in {u.statement for u in current.semantic_units}
        assert old.interpretation.record_id != current.interpretation.record_id
        assert len(current.inheritance.reused_segment_ids) == 1

    exercise(tmp_path, run)


def test_database_rejects_ready_new_contract_without_completed_proof(tmp_path):
    async def run(sessions, seed):
        import uuid

        from sqlalchemy import insert
        from sqlalchemy.exc import IntegrityError

        from backend.app.desktop.agent_loop.context_expansion.models import (
            LoopSemanticIndexArtifact,
        )

        target = await revision(sessions, seed["context_id"], NARRATIVE)
        index = (
            await narrative_service(NarrativeProbe(), sessions).build(
                observation(seed, target)
            )
        ).indexes[0]
        payload = index.model_dump(mode="json")
        payload.pop("interpretation")
        with pytest.raises(
            IntegrityError, match="ck_loop_semantic_interpretation_complete"
        ):
            async with sessions.begin() as session:
                await session.execute(
                    insert(LoopSemanticIndexArtifact).values(
                        index_id=uuid.uuid4().hex * 2,
                        context_id=target.ref.context_id,
                        revision_id=target.ref.revision_id,
                        source_content_hash=target.content_hash,
                        index_schema_version=index.index_schema_version,
                        segmenter_version=index.segmenter_version,
                        projector_version="rev:" + uuid.uuid4().hex,
                        status="ready",
                        payload=payload,
                        attempt_records=[],
                    )
                )

    exercise(tmp_path, run)
