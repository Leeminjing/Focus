r"""本文件对外提供 FactEventMaterializer。

输入为按 sequence 提交的 Run、Tool、Workspace、Artifact、Context revision、Directive 与 verification 规范事件；输出为
确定 identity 的事实修订和 policy 驱动的生命周期状态。具体工作流为把同一领域实体的连续事件归并为同一 Fact，先追加
观察修订，再用类型化证据推进验证；verification 事件只作用于明确引用的 Fact。示例：
`fact = await materializer.materialize(session, event)`。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.event_contract import CanonicalEventEnvelope
from backend.app.desktop.agent_loop.fact_identity import FactIdentityResolver
from backend.app.desktop.agent_loop.fact_lifecycle import FactLifecycleRepository, FactObservation
from backend.app.desktop.agent_loop.fact_models import LoopFact
from backend.app.desktop.agent_loop.fact_verification import FactActor, FactEvidenceReference, FactVerificationDecision, FactVerificationPolicy
from backend.app.desktop.models import DesktopRun


class FactEventMaterializer:
    _CATEGORIES = {
        "context_run": "run",
        "tool": "tool",
        "workspace_change": "workspace",
        "artifact": "artifact",
        "context_revision": "context_revision",
        "directive": "directive",
    }

    def __init__(self) -> None:
        self._lifecycle = FactLifecycleRepository()
        self._policy = FactVerificationPolicy()

    async def materialize(self, session: AsyncSession, event: CanonicalEventEnvelope) -> LoopFact | None:
        if event.kind.startswith("fact.verification."):
            return await self._apply_verification(session, event)
        if event.kind in {"context.tool.completed", "context.workspace.changed"} and event.causation_id is not None and event.payload.get("source") != "run_stream":
            return None
        fact_type = self._fact_type(event)
        if fact_type is None:
            return None
        payload = event.payload
        context_id = payload.get("context_id") or payload.get("target_context_id")
        run_id = payload.get("run_id")
        persisted_run_id = str(run_id) if run_id and await session.get(DesktopRun, str(run_id)) is not None else None
        evidence = self._evidence(event, fact_type)
        identity = FactIdentityResolver.resolve(
            event.loop_id,
            fact_type,
            self._subject(event, fact_type, context_id),
            f"event:{event.entity_type}:{run_id or ''}:{event.entity_id}",
        )
        if event.kind == "context.run.settled" and await session.get(LoopFact, identity.fact_id) is None:
            return None
        observation = FactObservation(
            loop_id=event.loop_id,
            identity=identity,
            fact_type=fact_type,
            source_context_id=str(context_id) if context_id else None,
            source_run_id=persisted_run_id,
            correlation_id=event.correlation_id,
            presentation=self._presentation(event, fact_type),
            evidence=evidence,
            observer=FactActor(kind="system", actor_id="fact-event-projector"),
            occurred_at=event.occurred_at,
            cause_event_id=event.event_id,
        )
        fact = await self._lifecycle.upsert_observation(session, observation)
        decision = self._policy.evaluate(evidence, observation.observer)
        if fact.state in {"observed", "verifying"}:
            fact = await self._lifecycle.apply_verification(session, fact.fact_id, decision, cause_event_id=event.event_id)
        return fact

    async def _apply_verification(self, session: AsyncSession, event: CanonicalEventEnvelope) -> LoopFact | None:
        fact_id = str(event.payload.get("fact_id") or event.entity_id)
        fact = await session.get(LoopFact, fact_id)
        if fact is None:
            return None
        target = str(event.payload.get("target_state") or "verified")
        if target not in {"observed", "verifying", "verified"}:
            return None
        verifier = FactActor(kind="system", actor_id=str(event.payload.get("verifier_id") or "fact-verification-event"))
        return await self._lifecycle.apply_verification(
            session,
            fact_id,
            FactVerificationDecision(target_state=target, verifier=verifier if target != "observed" else None, reason=str(event.payload.get("reason") or "验证事件已提交")),
            cause_event_id=event.event_id,
        )

    @classmethod
    def _fact_type(cls, event: CanonicalEventEnvelope) -> str | None:
        if event.entity_type in cls._CATEGORIES:
            return cls._CATEGORIES[event.entity_type]
        if event.kind.startswith("context.artifact."):
            return "artifact"
        if event.kind.startswith("context.revision."):
            return "context_revision"
        return None

    @staticmethod
    def _subject(event: CanonicalEventEnvelope, fact_type: str, context_id: object) -> str:
        if fact_type == "tool":
            return f"context:{context_id or 'unknown'}:tool:{event.payload.get('tool_name') or event.entity_id}"
        if fact_type == "workspace":
            return f"context:{context_id or 'unknown'}:workspace"
        if fact_type == "directive":
            return f"directive:{event.entity_id}"
        return f"{fact_type}:{event.entity_id}"

    @staticmethod
    def _presentation(event: CanonicalEventEnvelope, fact_type: str) -> dict:
        payload = event.payload
        status = payload.get("status") or payload.get("state") or event.kind.rsplit(".", 1)[-1]
        return {
            "title": payload.get("title") or payload.get("tool_name") or fact_type.replace("_", " ").title(),
            "summary": payload.get("summary") or payload.get("reason") or f"{event.kind} 已提交",
            "metrics": payload.get("metrics") or {},
            "outcome_status": status,
        }

    @staticmethod
    def _evidence(event: CanonicalEventEnvelope, fact_type: str) -> tuple[FactEvidenceReference, ...]:
        terminal = event.kind.endswith((".completed", ".settled", ".changed", ".published", ".authorized", ".delivered", ".accepted"))
        evidence_kind = {
            "run": "run",
            "tool": "tool",
            "workspace": "workspace",
            "artifact": "artifact",
            "context_revision": "context_revision",
            "directive": "kernel",
        }.get(fact_type, "model_statement")
        if not terminal and fact_type not in {"artifact", "context_revision"}:
            evidence_kind = "model_statement"
        return (
            FactEvidenceReference(
                kind=evidence_kind,
                entity_id=event.entity_id,
                run_id=str(event.payload.get("run_id")) if event.payload.get("run_id") else None,
                context_revision_id=str(event.payload.get("context_revision_id")) if event.payload.get("context_revision_id") else None,
                metadata={"event_id": event.event_id, "kind": event.kind, "sequence": event.sequence},
            ),
        )
