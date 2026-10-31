"""Focus publication/authorization adapters for the independent semantic-index engine.

The inherited service owns frozen Revision reads, ancestry, Portfolio admission and
the final all-index publication transaction. The library owns indexing and scheduling.
Only local proofs commit during model work; complete artifacts are staged until the
Portfolio transaction validates and adopts the database winner before Curator dispatch.
"""

from contextlib import contextmanager
from contextvars import ContextVar

from focus_context_index import (
    BuildAuthorization,
    FocusModelAdapter,
    IndexArtifact,
    IndexBuildError,
    IndexEngine,
    IndexPolicy,
    snapshot_from_focus,
)
from focus_context_index.api import artifact_key
from focus_context_index.errors import IndexBudgetExceeded as EngineBudgetExceeded
from focus_context_index.segment_projection import (
    SegmentProjectionRecord as EngineRecord,
)
from focus_context_index.semantic_index import RevisionSemanticIndex as EngineIndex
from sqlalchemy import select

from backend.app.desktop.contexts.evolution.models import ContextPublicationReceipt
from backend.app.desktop.patrol.agent_loop.resources.usage import LoopUsageDelta

from .index_budget_repository import IndexBudgetReservationRepository
from .index_model_budget import IndexBudgetExceeded, _LocalReservation
from .models import LoopIndexBudgetReservation, LoopSegmentProjectionRecord
from .portfolio_index import _BUILD_STATE, PortfolioSemanticIndexService
from .segment_projection import SegmentProjectionRecord
from .semantic_index import RevisionSemanticIndex


class FocusIndexStore:
    """Use existing immutable proof/index tables and the host publication transaction."""

    def __init__(self, service):
        self.service = service
        self.target = ContextVar(f"focus_index_target_{id(self)}", default=None)

    @contextmanager
    def bind(self, snapshot, contracts):
        token = self.target.set((snapshot, contracts))
        try:
            yield
        finally:
            self.target.reset(token)

    @staticmethod
    def _scope(namespace):
        if namespace != "focus":
            raise ValueError("Focus index namespace scope mismatch")

    async def get_segment(self, namespace, context_id, cache_key):
        self._scope(namespace)
        async with self.service._sessions() as session:
            row = await session.scalar(
                select(LoopSegmentProjectionRecord).where(
                    LoopSegmentProjectionRecord.context_id == context_id,
                    LoopSegmentProjectionRecord.cache_key == cache_key,
                )
            )
            if row is None:
                return None
            proof = self.service._records._validated(row)
            return EngineRecord.model_validate(proof.model_dump(mode="json"))

    async def put_segment(self, namespace, record):
        self._scope(namespace)
        proof = SegmentProjectionRecord.model_validate(record.model_dump(mode="json"))
        async with self.service._sessions.begin() as session:
            winner = await self.service._records.put(session, proof.context_id, proof)
        return EngineRecord.model_validate(winner.model_dump(mode="json"))

    async def artifact_from_index(self, index, session=None):
        if session is None:
            async with self.service._sessions() as read_session:
                return await self.artifact_from_index(index, read_session)
        if index.inheritance is None or index.interpretation is None:
            raise ValueError("independent index requires complete projection proofs")
        records = await self.service._records.by_ids(
            session, index.source.context_id, index.inheritance.record_ids
        )
        if records is None:
            raise ValueError("independent index proof dependencies disappeared")
        engine_index = EngineIndex.model_validate(index.model_dump(mode="json"))
        snapshot = self.service._snapshot(index)
        return IndexArtifact(
            namespace=snapshot.namespace,
            snapshot_fingerprint=snapshot.fingerprint,
            index=engine_index,
            records=tuple(
                EngineRecord.model_validate(r.model_dump(mode="json")) for r in records
            ),
        )

    async def get_artifact(self, cache_key):
        snapshot, contracts = self.target.get()
        expected = artifact_key(
            snapshot.fingerprint,
            contracts.segmenter_version,
            contracts.revision,
            contracts.index_schema_version,
        )
        if cache_key != expected:
            raise ValueError("independent artifact cache key scope mismatch")
        async with self.service._sessions() as session:
            index = await self.service._artifacts.cached_index(
                session,
                revision_id=snapshot.source.revision_id,
                source_content_hash=snapshot.source_content_hash,
                index_schema_version=contracts.index_schema_version,
                segmenter_version=contracts.segmenter_version,
                projector_version=contracts.revision,
            )
            if index is None:
                return None
            artifact = await self.artifact_from_index(index, session)
            return artifact if artifact.cache_key == cache_key else None

    async def put_artifact(self, artifact):
        # Complete publication belongs to the existing Portfolio transaction. A
        # cache winner may already have committed while this candidate was built.
        winner = await self.get_artifact(artifact.cache_key)
        if winner is not None:
            return winner
        validated = IndexArtifact.model_validate_json(artifact.model_dump_json())
        async with self.service._sessions() as session:
            records = await self.service._records.by_ids(
                session,
                validated.index.source.context_id,
                tuple(r.record_id for r in validated.records),
            )
            if records is None:
                raise ValueError("staged index dependencies disappeared")
        return validated


class FocusIndexAdmission:
    """Reserve/settle in the original Loop ledger, with original owner and fencing checks."""

    def __init__(self, service):
        self.service = service
        self.pending = {}

    def _repository(self, call):
        state = _BUILD_STATE.get()
        auth = state.budget_authorization
        owner = self.service._request_owner(call.snapshot)
        return IndexBudgetReservationRepository(
            self.service._sessions,
            auth["loop_id"],
            auth["grant_revision"],
            auth["limits"],
            auth["usage"],
            reservation_id=call.call_id,
            owner={
                **owner,
                "index_call_id": call.call_id,
                "index_phase": call.phase,
                "context_id": call.snapshot.source.context_id,
                "source_revision_id": call.snapshot.source.revision_id,
            },
        )

    async def authorize(self, snapshot, authorization):
        auth = _BUILD_STATE.get().budget_authorization
        if authorization != self.service._authorization(snapshot):
            raise ValueError("independent index authority identity mismatch")
        async with self.service._sessions.begin() as session:
            grant = await IndexBudgetReservationRepository._authorized_grant(
                session, auth["loop_id"], auth["grant_revision"]
            )
            if (
                grant.context_scope
                and snapshot.source.context_id not in grant.context_scope
            ):
                raise IndexBudgetExceeded(
                    "independent index source outside current grant scope"
                )
            publication = await session.get(
                ContextPublicationReceipt, snapshot.source.revision_id
            )
            if (
                publication is None
                or publication.context_id != snapshot.source.context_id
            ):
                raise ValueError("independent index source is not published")
            repository = IndexBudgetReservationRepository(
                self.service._sessions,
                auth["loop_id"],
                auth["grant_revision"],
                auth["limits"],
                auth["usage"],
                owner=self.service._request_owner(snapshot),
            )
            await repository._validate_owner(session)

    def can_reserve(self, call):
        budget = _BUILD_STATE.get().budget
        try:
            budget._check_available(
                call.estimated_input_tokens, call.output_token_reserve
            )
            return True
        except IndexBudgetExceeded:
            return False

    async def reserve(self, call):
        await self.authorize(call.snapshot, call.authorization)
        budget = _BUILD_STATE.get().budget
        async with budget._lock:
            budget._check_available(
                call.estimated_input_tokens, call.output_token_reserve
            )
            repository = self._repository(call)
            await repository.reserve(
                call.estimated_input_tokens,
                call.output_token_reserve,
                retry=call.attempt > 1,
            )
            reservation = _LocalReservation(
                [1, call.estimated_input_tokens, call.output_token_reserve], repository
            )
            budget._occupy(reservation.amounts)
            self.pending[call.call_id] = (budget, reservation)

    async def settle(self, call, receipt):
        entry = self.pending.get(call.call_id)
        if entry is None:
            async with self.service._sessions() as session:
                row = await session.get(LoopIndexBudgetReservation, call.call_id)
                if (
                    row is None
                    or (row.actual_usage or {}).get("index_call_id") != call.call_id
                ):
                    raise ValueError("missing independent model reservation")
                repository = IndexBudgetReservationRepository.from_receipt(
                    self.service._sessions, row
                )
            budget, reservation = None, None
        else:
            budget, reservation = entry
            repository = reservation.receipt
        reported = (
            receipt.input_tokens is not None and receipt.output_tokens is not None
        )
        delta = LoopUsageDelta(
            model_calls=receipt.model_calls,
            input_tokens=receipt.input_tokens or 0,
            output_tokens=receipt.output_tokens or 0,
        )
        if budget is None:
            await (repository.settle(delta) if reported else repository.mark_unknown())
        else:
            async with budget._lock:
                await (
                    repository.settle(delta) if reported else repository.mark_unknown()
                )
                if reported:
                    budget._reconcile(reservation, delta)
            self.pending.pop(call.call_id, None)
        state = _BUILD_STATE.get()
        rows = state.attempts.get(call.snapshot.source.revision_id, ())
        if not any(row.get("call_id") == call.call_id for row in rows):
            self.service._append_attempt(
                call.snapshot.source.revision_id,
                {
                    **receipt.model_dump(mode="json"),
                    "role": "independent_index_model",
                    "usage_managed": True,
                    "usage_reported": reported,
                    "index_phase": call.phase,
                },
            )


class IndependentPortfolioSemanticIndexService(PortfolioSemanticIndexService):
    VERSION = "independent-portfolio-semantic-index-service-v1"

    def __init__(self, sessions, checkpointer, **kwargs):
        super().__init__(sessions, checkpointer, **kwargs)
        self._segment_options = (
            kwargs.get("segment_max_messages", 12),
            kwargs.get("segment_max_estimated_tokens"),
        )
        self._engine_store = FocusIndexStore(self)
        self._engine_admission = FocusIndexAdmission(self)
        self._engine = IndexEngine(
            models=FocusModelAdapter(
                projector_factory=self._factory("projector"),
                verifier_factory=self._factory("verifier"),
                interpreter_factory=self._factory("interpreter"),
            ),
            store=self._engine_store,
            admission=self._engine_admission,
            concurrency=self._concurrency,
        )

    def _factory(self, role):
        def create():
            factory = {
                "projector": self._semantic_projector_factory,
                "verifier": self._semantic_claim_verifier_factory,
                "interpreter": self._semantic_interpreter_factory,
            }[role]
            if factory is None:
                raise IndexBudgetExceeded(
                    "independent indexing requires all model roles"
                )
            model = factory()
            if getattr(model, "_max_attempts", 1) != 1:
                raise ValueError(
                    "independent index host factories must configure max_attempts=1"
                )
            if not hasattr(model, "max_output_tokens"):
                model.max_output_tokens = 8192
            # Match the host's conservative full-request bound for parity, rather
            # than silently relaxing admission when replacing the implementation.
            from .index_model_budget import IndexModelBudget

            model.estimate_request_tokens = IndexModelBudget._estimated_input
            return model

        return create

    def _policy(self):
        policy = _BUILD_STATE.get().budget.resources.policy
        return IndexPolicy(
            segment_max_messages=self._segment_options[0],
            segment_max_estimated_tokens=self._segment_options[1],
            max_attempts=policy.max_model_attempts_per_operation or self._max_attempts,
            max_exact_reads=policy.max_exact_reads,
            max_planner_model_calls=policy.max_planner_model_calls,
            max_request_input_tokens=policy.max_request_input_tokens,
            output_token_reserve=policy.output_token_reserve or 1,
        )

    async def _model_budget(
        self, observation, authority_revision=None, *, fencing_token=None
    ):
        budget = await super()._model_budget(
            observation, authority_revision, fencing_token=fencing_token
        )
        _BUILD_STATE.get().budget_authorization["owner"] = {
            "owner_kind": "round",
            "owner_id": observation.round_id,
            "round_id": observation.round_id,
            "observation_id": observation.decision_inputs_ref,
            **({"fencing_token": fencing_token} if fencing_token is not None else {}),
        }
        return budget

    def _request_owner(self, snapshot):
        state = _BUILD_STATE.get()
        if state.background:
            return {
                "owner_kind": "index_prewarm",
                "owner_id": snapshot.source.revision_id,
                "context_id": snapshot.source.context_id,
                "content_hash": snapshot.source_content_hash,
            }
        return state.budget_authorization["owner"]

    def _authorization(self, snapshot):
        from .contracts import stable_expansion_hash

        auth = _BUILD_STATE.get().budget_authorization
        return BuildAuthorization(
            owner_id=auth["loop_id"],
            grant_id=stable_expansion_hash(
                "independent-index-owner-v1", auth, self._request_owner(snapshot)
            ),
            allowed_namespaces=("focus",),
        )

    def _prepare_contract(self):
        contracts = self._engine.contracts(self._policy())
        state = _BUILD_STATE.get()
        state.local_contract, state.interpretation_contract = (
            contracts.local,
            contracts.interpretation,
        )
        state.contract = contracts.revision

    async def _bounded_index(self, observation, frontier_item, semaphore):
        # The engine owns response retries. Repeating the whole build here would
        # multiply its attempt limit and could resample after a settlement error.
        async with semaphore:
            return await self._index_one(observation, frontier_item)

    @staticmethod
    def _snapshot(index):
        return snapshot_from_focus(
            namespace="focus",
            revision_ref=index.source,
            content_hash=index.source_content_hash,
            semantic_messages=index.message_records(),
            context_role=index.context_role,
            active_objective=index.active_objective,
        )

    async def _index_one(self, observation, frontier_item):
        frozen, plan, _inherited = await self._read_and_plan(observation, frontier_item)
        snapshot = self._snapshot(frozen)
        authorization = self._authorization(snapshot)
        await self._engine_admission.authorize(snapshot, authorization)
        if plan is None:
            await self._engine_store.artifact_from_index(frozen)
            return frozen
        baseline = None
        if plan.baseline_index_id:
            async with self._sessions() as session:
                old = (
                    await self._artifacts.indexes_by_ids(
                        session, (plan.baseline_index_id,)
                    )
                )[0]
                baseline = await self._engine_store.artifact_from_index(old, session)
        policy = self._policy()

        async def progress(item):
            await self._progress(
                snapshot.source.revision_id,
                item.phase,
                completed_segments=item.completed_segments,
                total_segments=item.total_segments,
            )

        try:
            with self._engine_store.bind(snapshot, self._engine.contracts(policy)):
                built = await self._engine.build(
                    snapshot=snapshot,
                    baseline=baseline,
                    policy=policy,
                    authorization=authorization,
                    on_progress=progress,
                )
        except IndexBuildError as exc:
            if isinstance(exc.cause, (EngineBudgetExceeded, IndexBudgetExceeded)):
                raise IndexBudgetExceeded(str(exc.cause)) from exc
            raise ValueError(str(exc.cause)) from exc
        state = _BUILD_STATE.get()
        state.records[snapshot.source.revision_id] = tuple(
            SegmentProjectionRecord.model_validate(r.model_dump(mode="json"))
            for r in built.artifact.records
        )
        state.telemetry[snapshot.source.revision_id] = {
            "mode": built.stats.mode,
            "local_cache_hits": built.stats.local_cache_hits,
            "projected_segments": built.stats.projected_segments,
            "total_segments": built.stats.total_segments,
            "completed_segments": built.stats.total_segments,
            "engine_stage_ms": built.stats.stage_ms,
        }
        return RevisionSemanticIndex.model_validate(built.index.model_dump(mode="json"))

    async def _prewarm_frozen(self, frozen):
        snapshot, policy = self._snapshot(frozen), self._policy()
        with self._engine_store.bind(snapshot, self._engine.contracts(policy)):
            await self._engine.prewarm(
                snapshot=snapshot,
                policy=policy,
                authorization=self._authorization(snapshot),
            )
        return True
