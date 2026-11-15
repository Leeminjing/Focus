"""Real PostgreSQL host integration for the independent indexing engine."""

import asyncio
import json

import pytest
from sqlalchemy import delete, select

from backend.app.desktop.patrol.agent_loop.context_expansion.independent_portfolio_index import (
    IndependentPortfolioSemanticIndexService,
)
from backend.app.desktop.patrol.agent_loop.context_expansion.models import (
    LoopIndexBudgetReservation,
    LoopSegmentProjectionRecord,
    LoopSemanticIndexArtifact,
)
from backend.app.desktop.patrol.agent_loop.models import (
    LoopBudgetUsage,
    LoopDelegationGrant,
)
from backend.app.desktop.patrol.agent_loop.observations.observation_capture import (
    LoopObservationService,
)
from backend.app.desktop.patrol.agent_loop.sessions.patrol_runtime import (
    CuratorCoordinationStage,
)
from backend.tests.incremental_index_support import (
    Checkpoints,
    ModelProbe,
    messages,
    observation,
    revision,
)
from backend.tests.test_incremental_revision_index import exercise

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def make_service(sessions, probe, **kwargs):
    return IndependentPortfolioSemanticIndexService(
        sessions,
        Checkpoints(),
        semantic_projector_factory=probe.factory("projector"),
        semantic_claim_verifier_factory=probe.factory("verifier"),
        semantic_interpreter_factory=probe.factory("projector"),
        **kwargs,
    )


def test_frozen_revision_independent_engine_publication_and_curator_input(tmp_path):
    async def run(sessions, seed):
        probe = ModelProbe()
        service = make_service(sessions, probe, segment_max_messages=2)
        first = await revision(sessions, seed["context_id"], messages(5))
        built = await service.build(observation(seed, first))
        assert built.blocker_code is None, built.blocker_summary
        assert built.catalog is not None
        assert built.indexes[0].interpretation.completed
        initial_calls = len(probe.calls)
        exact = await service.build(observation(seed, first))
        assert exact.indexes == built.indexes
        assert len(probe.calls) == initial_calls
        second = await revision(sessions, seed["context_id"], messages(6), parent=first)
        appended = await service.build(observation(seed, second))
        assert appended.blocker_code is None, appended.blocker_summary
        assert len(appended.indexes[0].inheritance.reused_segment_ids) == 2
        assert all(
            ref.source == second.ref
            for unit in appended.indexes[0].semantic_units
            for ref in unit.evidence_refs
        )
        frozen = await LoopObservationService(sessions, Checkpoints()).capture(
            seed["loop_id"], seed["round_id"]
        )
        stage = CuratorCoordinationStage(sessions, Checkpoints(), index_service=service)
        prepared = await stage._derivation_input(frozen)
        assert prepared["semantic_index_ids"]
        assert prepared["portfolio_index_catalog"]
        async with sessions() as session:
            usage = await session.get(LoopBudgetUsage, seed["loop_id"])
            assert usage.model_calls == len(probe.calls)
            receipts = (
                await session.scalars(
                    select(LoopIndexBudgetReservation).where(
                        LoopIndexBudgetReservation.loop_id == seed["loop_id"]
                    )
                )
            ).all()
            assert len(receipts) == len(probe.calls)
            assert all(row.settled_at is not None for row in receipts)

    exercise(tmp_path, run)


def test_durable_budget_counts_json_retry_once(tmp_path):
    attempts = 0

    async def gate(role, payload):
        nonlocal attempts
        if role == "projector":
            attempts += 1
            if attempts == 1:
                json.loads('{"units":')

    class ReportedFailureProbe(ModelProbe):
        def factory(self, role):
            parent = super().factory(role)

            class Model(parent):
                async def invoke(self, schema, system, payload):
                    try:
                        return await super().invoke(schema, system, payload)
                    except json.JSONDecodeError:
                        self.last_attempt_records = (
                            {
                                "model_calls": 1,
                                "input_tokens": 100,
                                "output_tokens": 10,
                            },
                        )
                        raise

            return Model

    async def run(sessions, seed):
        probe = ReportedFailureProbe(gate=gate)
        service = make_service(sessions, probe)
        target = await revision(sessions, seed["context_id"], messages(2))
        built = await service.build(observation(seed, target))
        assert built.blocker_code is None, built.blocker_summary
        async with sessions() as session:
            usage = await session.get(LoopBudgetUsage, seed["loop_id"])
            assert usage.model_calls == len(probe.calls) == 4
            assert usage.retries == 1

    exercise(tmp_path, run)


def test_current_scope_is_rechecked_before_a_complete_cache_hit(tmp_path):
    async def run(sessions, seed):
        probe = ModelProbe()
        service = make_service(sessions, probe)
        target = await revision(sessions, seed["context_id"], messages(2))
        assert (await service.build(observation(seed, target))).blocker_code is None
        calls = len(probe.calls)
        async with sessions.begin() as session:
            grant = await session.scalar(
                select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == seed["loop_id"]
                )
            )
            grant.context_scope = ["foreign-context"]
        denied = await service.build(observation(seed, target))
        assert denied.blocker_code is not None
        assert denied.catalog is None
        assert len(probe.calls) == calls

    exercise(tmp_path, run)


def test_missing_cached_local_proofs_are_an_integrity_blocker(tmp_path):
    async def run(sessions, seed):
        probe = ModelProbe()
        service = make_service(sessions, probe)
        target = await revision(sessions, seed["context_id"], messages(2))
        assert (await service.build(observation(seed, target))).blocker_code is None
        calls = len(probe.calls)
        async with sessions.begin() as session:
            await session.execute(
                delete(LoopSegmentProjectionRecord).where(
                    LoopSegmentProjectionRecord.context_id == seed["context_id"]
                )
            )
        corrupted = await service.build(observation(seed, target))
        assert corrupted.blocker_code is not None
        assert corrupted.catalog is None
        assert len(probe.calls) == calls

    exercise(tmp_path, run)


def test_catalog_rejection_preserves_completed_context_index(tmp_path):
    async def run(sessions, seed):
        probe = ModelProbe()
        service = make_service(sessions, probe, catalog_max_descriptor_chars=1)
        target = await revision(sessions, seed["context_id"], messages(2))
        built = await service.build(observation(seed, target))
        assert built.blocker_code == "portfolio_catalog_overflow"
        async with sessions() as session:
            local = await session.scalars(
                select(LoopSegmentProjectionRecord).where(
                    LoopSegmentProjectionRecord.context_id == seed["context_id"]
                )
            )
            complete = await session.scalars(
                select(LoopSemanticIndexArtifact).where(
                    LoopSemanticIndexArtifact.context_id == seed["context_id"]
                )
            )
            assert local.all()
            assert len(complete.all()) == 1
            assert (
                await session.get(LoopBudgetUsage, seed["loop_id"])
            ).model_calls == len(probe.calls)

    exercise(tmp_path, run)


def test_scoped_independent_build_uses_current_worker_budget_owner(tmp_path):
    import uuid
    from backend.app.desktop.patrol.agent_loop.models import LoopWorkerRequest
    from backend.app.desktop.patrol.agent_loop.workers.runtime import LoopWorkerRuntime

    async def run(sessions, seed):
        probe = ModelProbe()
        service = make_service(sessions, probe)
        await revision(sessions, seed["context_id"], messages(2))
        frozen = await LoopObservationService(sessions, Checkpoints()).refresh(seed["loop_id"], seed["round_id"])
        worker_id = uuid.uuid4().hex
        async with sessions.begin() as session:
            session.add(LoopWorkerRequest(worker_request_id=worker_id, loop_id=seed["loop_id"],
                round_id=seed["round_id"], observation_id=frozen.observation_id,
                observation_generation=frozen.generation, kind="patrol_evidence", scope={}))
        worker, = await LoopWorkerRuntime(sessions, object())._claim_many(1, seed["loop_id"])
        built = await service.build(frozen, required_context_ids=(seed["context_id"],), worker_request=worker)
        assert built.blocker_code is None, built.blocker_summary
        assert len(built.indexes) == 1
        async with sessions() as session:
            receipts = tuple(await session.scalars(select(LoopIndexBudgetReservation).where(
                LoopIndexBudgetReservation.loop_id == seed["loop_id"])))
            assert receipts
            assert all(row.actual_usage["owner_kind"] == "worker" and row.actual_usage["owner_id"] == worker_id
                       and row.actual_usage["retry_identity"] == worker.retry_identity for row in receipts)

    exercise(tmp_path, run)


def test_host_repeated_cancel_waits_for_durable_settlement(tmp_path, monkeypatch):
    from backend.app.desktop.patrol.agent_loop.context_expansion.independent_portfolio_index import (
        FocusIndexAdmission,
    )

    async def run(sessions, seed):
        entered, release = asyncio.Event(), asyncio.Event()
        settle = FocusIndexAdmission.settle

        async def slow_settle(self, call, receipt):
            entered.set()
            await release.wait()
            await settle(self, call, receipt)

        monkeypatch.setattr(FocusIndexAdmission, "settle", slow_settle)
        probe = ModelProbe()
        service = make_service(sessions, probe)
        target = await revision(sessions, seed["context_id"], messages(2))
        task = asyncio.create_task(service.build(observation(seed, target)))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            for _ in range(3):
                task.cancel()
                for _ in range(10):
                    await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        async with sessions() as session:
            receipts = (
                await session.scalars(
                    select(LoopIndexBudgetReservation).where(
                        LoopIndexBudgetReservation.loop_id == seed["loop_id"]
                    )
                )
            ).all()
            assert len(receipts) == 1
            assert all(r.settled_at is not None for r in receipts)
            assert (
                await session.get(LoopBudgetUsage, seed["loop_id"])
            ).model_calls == 1
        assert not service._engine_admission.pending

    exercise(tmp_path, run)


def test_independent_prewarm_commits_local_proofs_and_reuses_them_foreground(tmp_path):
    async def run(sessions, seed):
        probe = ModelProbe()
        service = make_service(sessions, probe, segment_max_messages=2)
        target = await revision(sessions, seed["context_id"], messages(5))
        assert await service.prewarm_revision(seed["loop_id"], 1, target)
        warm_calls = len(probe.calls)
        assert warm_calls == 6
        async with sessions() as session:
            complete = await session.scalars(
                select(LoopSemanticIndexArtifact).where(
                    LoopSemanticIndexArtifact.context_id == seed["context_id"]
                )
            )
            assert not complete.all()
        built = await service.build(observation(seed, target))
        assert built.blocker_code is None, built.blocker_summary
        assert len(probe.calls) == warm_calls + 1
        async with sessions() as session:
            assert (
                await session.get(LoopBudgetUsage, seed["loop_id"])
            ).model_calls == len(probe.calls)

    exercise(tmp_path, run)


def test_exhausted_current_budget_blocks_before_any_provider_call(tmp_path):
    async def run(sessions, seed):
        probe = ModelProbe()
        service = make_service(sessions, probe)
        target = await revision(sessions, seed["context_id"], messages(2))
        async with sessions.begin() as session:
            usage = await session.get(LoopBudgetUsage, seed["loop_id"])
            usage.model_calls = 1
        built = await service.build(observation(seed, target))
        assert built.blocker_code == "portfolio_index_budget"
        assert not probe.calls

    exercise(tmp_path, run, budgets={"max_model_calls": 1})


def test_host_does_not_multiply_the_engine_response_retry_limit(tmp_path):
    async def fail_json(role, payload):
        if role == "projector":
            json.loads('{"units":')

    async def run(sessions, seed):
        probe = ModelProbe(gate=fail_json)
        service = make_service(sessions, probe, max_attempts=2)
        target = await revision(sessions, seed["context_id"], messages(2))
        built = await service.build(observation(seed, target))
        assert built.blocker_code is not None
        assert len(probe.calls) == 2

    exercise(tmp_path, run)


def test_host_settlement_error_does_not_resample(tmp_path, monkeypatch):
    from backend.app.desktop.patrol.agent_loop.context_expansion.independent_portfolio_index import (
        FocusIndexAdmission,
    )

    settle = FocusIndexAdmission.settle

    async def failed_settlement(self, call, receipt):
        await settle(self, call, receipt)
        json.loads('{"settlement_ack":')

    monkeypatch.setattr(FocusIndexAdmission, "settle", failed_settlement)

    async def run(sessions, seed):
        probe = ModelProbe()
        service = make_service(sessions, probe, max_attempts=2)
        target = await revision(sessions, seed["context_id"], messages(2))
        built = await service.build(observation(seed, target))
        assert built.blocker_code is not None
        assert len(probe.calls) == 1
        async with sessions() as session:
            assert (
                await session.get(LoopBudgetUsage, seed["loop_id"])
            ).model_calls == 1
        assert not service._engine_admission.pending

    exercise(tmp_path, run)
