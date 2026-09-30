r"""本文件对外提供 FactMaterializer，将一个已提交 Run 的候选证据原子物化为版本化事实。

输入为 Run、来源 journal event、correlation 与 RunFactSourceReader；输出为该 Run 创建或复用的领域 LoopFact 集合和 fact
规范事件。具体工作流为解析候选、生成稳定 identity、记录 observed、应用验证 policy、关联旧 subject 结论；本模块不维护
投影游标或调度循环。示例：`facts = await materializer.materialize_run(session, run, event)`。
只物化 Run/Test/Workspace/Artifact 领域结果；独立 activity 保留 Tool 审计，领域结果版本可更新同一事实。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.event_contract import CanonicalEventEnvelope
from backend.app.desktop.agent_loop.fact_identity import FactIdentityResolver
from backend.app.desktop.agent_loop.fact_lifecycle import (
    FactLifecycleRepository,
    FactObservation,
)
from backend.app.desktop.agent_loop.fact_models import LoopFact
from backend.app.desktop.agent_loop.fact_verification import (
    FactActor,
    FactEvidenceReference,
    FactVerificationPolicy,
)
from backend.app.desktop.agent_loop.materialized_fact_sources import RunFactSourceReader
from backend.app.desktop.models import DesktopRun


class FactMaterializer:
    def __init__(self, source_reader: RunFactSourceReader) -> None:
        self._sources = source_reader
        self._lifecycle = FactLifecycleRepository()
        self._policy = FactVerificationPolicy()

    async def materialize_run(
        self,
        session: AsyncSession,
        run: DesktopRun,
        source_event: CanonicalEventEnvelope | None,
    ) -> tuple[LoopFact, ...]:
        if run.loop_id is None:
            return ()
        correlation_id = source_event.correlation_id if source_event is not None else await self._correlation(run)
        cause_event_id = source_event.event_id if source_event is not None else None
        candidates = await self._sources.read(session, (run,))
        materialized: list[LoopFact] = []
        for candidate in candidates:
            if candidate["kind"] == "tool":
                continue
            evidence = self._evidence(candidate)
            identity = FactIdentityResolver.resolve(
                run.loop_id,
                candidate["kind"],
                self._subject(run, candidate),
                self._source_key(run, candidate),
            )
            observation = FactObservation(
                loop_id=run.loop_id,
                identity=identity,
                fact_type=candidate["kind"],
                source_context_id=run.task_id,
                source_run_id=run.run_id,
                correlation_id=correlation_id,
                presentation={
                    "title": candidate.get("title"),
                    "summary": candidate.get("summary"),
                    "metrics": candidate.get("metrics") or {},
                    "outcome_status": candidate.get("status"),
                },
                evidence=evidence,
                observer=FactActor(kind="system", actor_id="fact-projector"),
                occurred_at=datetime.fromisoformat(candidate["occurred_at"]),
                cause_event_id=cause_event_id,
            )
            fact = await self._lifecycle.upsert_observation(session, observation)
            decision = self._policy.evaluate(evidence, observation.observer)
            if fact.state in {"observed", "verifying"}:
                fact = await self._lifecycle.apply_verification(session, fact.fact_id, decision, cause_event_id=cause_event_id)
            if fact.state == "verified":
                await self._lifecycle.reconcile_subject(session, fact, cause_event_id)
            materialized.append(fact)
        return tuple(materialized)

    @staticmethod
    async def _correlation(run: DesktopRun) -> str | None:
        return run.directive_id or run.user_intent_id

    @staticmethod
    def _subject(run: DesktopRun, candidate: dict) -> str:
        evidence = candidate.get("evidence") or {}
        kind = candidate["kind"]
        if kind == "workspace":
            return f"context:{run.task_id}:workspace"
        if kind == "artifact":
            return f"artifact:{evidence.get('artifact') or candidate.get('summary')}"
        if kind == "test":
            return f"context:{run.task_id}:test" if evidence.get("domain_source_id") else f"context:{run.task_id}:test:{evidence.get('tool_name') or 'test'}"
        return f"run:{run.run_id}"

    @staticmethod
    def _source_key(run: DesktopRun, candidate: dict) -> str:
        evidence = candidate.get("evidence") or {}
        if candidate["kind"] == "test" and evidence.get("domain_source_id"):
            return f"domain:test:{evidence['domain_source_id']}"
        return ":".join(
            str(value)
            for value in (
                run.run_id,
                evidence.get("context_revision_id"),
                evidence.get("tool_call_id") or evidence.get("message_id"),
                evidence.get("artifact"),
                candidate["kind"],
            )
            if value is not None
        )

    @staticmethod
    def _evidence(candidate: dict) -> tuple[FactEvidenceReference, ...]:
        evidence = candidate.get("evidence") or {}
        kind = candidate["kind"]
        evidence_kind = "test" if kind == "test" else "workspace" if kind == "workspace" else "artifact" if kind == "artifact" else "run"
        entity_id = (
            evidence.get("domain_source_id")
            or evidence.get("tool_call_id")
            or evidence.get("message_id")
            or evidence.get("artifact")
            or evidence.get("run_id")
            or candidate["fact_id"]
        )
        return (
            FactEvidenceReference(
                kind=evidence_kind,
                entity_id=str(entity_id),
                run_id=evidence.get("run_id"),
                context_revision_id=evidence.get("context_revision_id"),
                metadata={key: value for key, value in evidence.items() if key not in {"workspace_result", "error"}},
            ),
        )
