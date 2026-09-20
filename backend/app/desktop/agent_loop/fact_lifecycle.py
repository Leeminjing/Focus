r"""本文件对外提供 FactObservation、FactLifecycleRepository 与 FactTransitionRejected。

输入为确定性事实身份、展示字段、类型化证据、观察/验证主体、原因事件和目标生命周期；输出为安全规范化的当前事实、不可变修订、
关系记录及 `fact.upserted` 规范事件。具体工作流为先持久化 observed，同 identity 的新语义或状态追加同生命周期修订，
再按验证策略推进；新观察可显式 supersede 或
contradict 同 subject 的旧事实，历史从不删除。示例：`fact = await repository.observe(session, observation)`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft, EventVisibility
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.fact_identity import FactIdentity
from backend.app.desktop.agent_loop.fact_models import LoopFact, LoopFactRelationship, LoopFactRevision
from backend.app.desktop.agent_loop.fact_verification import FactActor, FactEvidenceReference, FactVerificationDecision
from backend.app.desktop.persistence_safety import PersistencePayloadNormalizer


class FactTransitionRejected(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FactObservation:
    loop_id: str
    identity: FactIdentity
    fact_type: str
    source_context_id: str | None
    source_run_id: str | None
    correlation_id: str | None
    presentation: dict
    evidence: tuple[FactEvidenceReference, ...]
    observer: FactActor
    occurred_at: datetime
    cause_event_id: str | None = None


class FactLifecycleRepository:
    _EDGES = {
        "observed": frozenset({"verifying", "verified", "contradicted", "superseded"}),
        "verifying": frozenset({"verified", "contradicted", "superseded"}),
        "verified": frozenset({"contradicted", "superseded"}),
        "contradicted": frozenset({"superseded"}),
        "superseded": frozenset(),
    }

    def __init__(self) -> None:
        self._journal = LoopEventJournal()

    async def observe(self, session: AsyncSession, observation: FactObservation) -> LoopFact:
        fact = await session.get(LoopFact, observation.identity.fact_id, with_for_update=True)
        if fact is not None:
            return fact
        presentation, evidence, observer = self._safe_observation(observation)
        fact = LoopFact(
            fact_id=observation.identity.fact_id,
            loop_id=observation.loop_id,
            identity_key=observation.identity.identity_key,
            fact_type=observation.fact_type,
            normalized_subject=observation.identity.normalized_subject,
            state="observed",
            source_context_id=observation.source_context_id,
            source_run_id=observation.source_run_id,
            correlation_id=observation.correlation_id,
            presentation=presentation,
            evidence=evidence,
            observer=observer,
            occurred_at=observation.occurred_at,
        )
        session.add(fact)
        await session.flush()
        await self._append_revision(session, fact, "observed", observation.cause_event_id, None, "事实观察已提交")
        return fact

    async def upsert_observation(self, session: AsyncSession, observation: FactObservation) -> LoopFact:
        fact = await session.get(LoopFact, observation.identity.fact_id, with_for_update=True)
        if fact is None:
            return await self.observe(session, observation)
        presentation, evidence, observer = self._safe_observation(observation)
        if fact.presentation == presentation and fact.evidence == evidence:
            return fact
        fact.presentation = presentation
        fact.evidence = evidence
        fact.source_context_id = observation.source_context_id
        fact.source_run_id = observation.source_run_id
        fact.correlation_id = observation.correlation_id
        fact.observer = observer
        fact.occurred_at = observation.occurred_at
        return await self._append_revision(
            session,
            fact,
            fact.state,
            observation.cause_event_id,
            None,
            "同一事实的展示或证据已更新",
            allow_same_state=True,
        )

    async def apply_verification(
        self,
        session: AsyncSession,
        fact_id: str,
        decision: FactVerificationDecision,
        *,
        cause_event_id: str | None,
    ) -> LoopFact:
        fact = await session.get(LoopFact, fact_id, with_for_update=True)
        if fact is None:
            raise LookupError("Fact 不存在")
        if decision.target_state == "observed" or decision.target_state == fact.state:
            return fact
        if decision.target_state == "verified" and fact.state == "observed":
            await self._append_revision(session, fact, "verifying", cause_event_id, decision.verifier, "正在核验类型化证据")
        return await self._append_revision(session, fact, decision.target_state, cause_event_id, decision.verifier, decision.reason)

    async def reconcile_subject(self, session: AsyncSession, current: LoopFact, cause_event_id: str | None) -> LoopFactRelationship | None:
        previous = await session.scalar(
            select(LoopFact)
            .where(
                LoopFact.loop_id == current.loop_id,
                LoopFact.fact_type == current.fact_type,
                LoopFact.normalized_subject == current.normalized_subject,
                LoopFact.fact_id != current.fact_id,
                LoopFact.state.in_(("observed", "verifying", "verified")),
            )
            .order_by(LoopFact.occurred_at.desc(), LoopFact.fact_id.desc())
            .with_for_update()
            .limit(1)
        )
        if previous is None:
            return None
        relation = "supersedes" if self._meaning(previous.presentation) == self._meaning(current.presentation) else "contradicts"
        target_state = "superseded" if relation == "supersedes" else "contradicted"
        await self._append_revision(session, previous, target_state, cause_event_id, None, f"被事实 {current.fact_id} {relation}")
        relationship = LoopFactRelationship(
            relationship_id=uuid.uuid4().hex,
            loop_id=current.loop_id,
            source_fact_id=current.fact_id,
            target_fact_id=previous.fact_id,
            relation=relation,
            cause_event_id=cause_event_id,
        )
        session.add(relationship)
        return relationship

    async def history(self, session: AsyncSession, fact_id: str) -> tuple[LoopFactRevision, ...]:
        return tuple((await session.scalars(select(LoopFactRevision).where(LoopFactRevision.fact_id == fact_id).order_by(LoopFactRevision.revision))).all())

    async def _append_revision(
        self,
        session: AsyncSession,
        fact: LoopFact,
        target: str,
        cause_event_id: str | None,
        verifier: FactActor | None,
        reason: str,
        *,
        allow_same_state: bool = False,
    ) -> LoopFact:
        if fact.current_revision and target != fact.state and target not in self._EDGES.get(fact.state, frozenset()):
            raise FactTransitionRejected(f"非法 Fact transition: {fact.state} -> {target}")
        if fact.current_revision and target == fact.state and not allow_same_state:
            raise FactTransitionRejected(f"非法 Fact transition: {fact.state} -> {target}")
        fact.current_revision += 1
        fact.state = target
        if verifier is not None:
            fact.verifier = verifier.model_dump(mode="json")
        revision = LoopFactRevision(
            fact_revision_id=uuid.uuid4().hex,
            fact_id=fact.fact_id,
            loop_id=fact.loop_id,
            revision=fact.current_revision,
            state=target,
            cause_event_id=cause_event_id,
            correlation_id=fact.correlation_id,
            presentation=fact.presentation,
            evidence=fact.evidence,
            observer=fact.observer,
            verifier=fact.verifier,
            reason=reason,
            occurred_at=fact.occurred_at,
        )
        session.add(revision)
        await self._journal.append(
            session,
            fact.loop_id,
            CanonicalEventDraft(
                kind="fact.upserted",
                entity_type="fact",
                entity_id=fact.fact_id,
                entity_revision=fact.current_revision,
                correlation_id=fact.correlation_id,
                causation_id=cause_event_id,
                visibility=EventVisibility(evidence_fields=("evidence",)),
                payload=self._payload(fact),
                idempotency_key=f"fact:{fact.fact_id}:revision:{fact.current_revision}",
            ),
        )
        return fact

    @staticmethod
    def _meaning(presentation: dict) -> str:
        comparable = {
            "outcome_status": presentation.get("outcome_status"),
            "summary": presentation.get("summary"),
            "metrics": presentation.get("metrics") or {},
        }
        return json.dumps(comparable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _safe_observation(observation: FactObservation) -> tuple[dict, list[dict], dict]:
        presentation = PersistencePayloadNormalizer.normalize(
            observation.presentation, "loop-fact.presentation"
        ).value
        evidence = PersistencePayloadNormalizer.normalize(
            [item.model_dump(mode="json") for item in observation.evidence], "loop-fact.evidence"
        ).value
        observer = PersistencePayloadNormalizer.normalize(
            observation.observer.model_dump(mode="json"), "loop-fact.observer"
        ).value
        return presentation, evidence, observer

    @staticmethod
    def _payload(fact: LoopFact) -> dict:
        return {
            "fact_id": fact.fact_id,
            "revision": fact.current_revision,
            "fact_type": fact.fact_type,
            "normalized_subject": fact.normalized_subject,
            "state": fact.state,
            "source_context_id": fact.source_context_id,
            "source_run_id": fact.source_run_id,
            "presentation": fact.presentation,
            "evidence": fact.evidence,
            "observer": fact.observer,
            "verifier": fact.verifier,
            "occurred_at": fact.occurred_at.isoformat(),
        }
