"""本文件对外提供提交到可执行的只读观测与分位数统计。

输入为提交函数、耐久回执及已提交的决策、发布和执行事实；输出为累计里程碑与缺失样本统计。
具体工作流为从提交前的单调时钟计时，轮询独立事务，保留截止窗口内首次观测值及阶段诊断。
示例：`measure_submission(submit, probe, scenario="cold", repeat=0)`。

The probe is read-only. Stage durations remain diagnostic and are never summed
into wall time. Poll observations are upper bounds including observer latency.
No prompts, model responses, credentials, or transaction timestamps are exported.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from math import ceil, isfinite
import re
from time import perf_counter

from sqlalchemy import cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import JSONPATH

from backend.app.desktop.patrol.agent_loop.models import (
    AgentLoop, LoopAction, LoopDecision, LoopDirective, LoopObservation,
)
from backend.app.desktop.patrol.agent_loop.journal.journal_models import LoopJournalEvent
from backend.app.desktop.patrol.agent_loop.context_expansion.models import LoopContextDerivationArtifact
from backend.app.desktop.contexts.evolution.models import ContextPublicationReceipt, ContextRevision
from backend.app.desktop.persistence.models import DesktopRun

MILESTONES = ("durable_acceptance", "patrol_decision_committed", "derived_context_available",
              "first_run_started", "first_effective_output")
SCENARIOS = ("cold", "exact_cache", "small_append", "long_tool_output")


@dataclass
class SubmissionSample:
    scenario: str
    repeat: int
    milestones_ms: dict[str, float] = field(default_factory=dict)
    identities: dict[str, dict] = field(default_factory=dict)
    stages: list[dict] = field(default_factory=list)
    outcome: str = "timeout"
    failure_code: str | None = None
    observation_ms: float | None = None
    discarded_milestones_ms: dict[str, float] = field(default_factory=dict)

    def observe(self, milestone, elapsed_ms, identities):
        if milestone not in MILESTONES or not isfinite(elapsed_ms) or elapsed_ms < 0:
            raise ValueError("invalid submission milestone")
        if milestone not in self.milestones_ms:
            self.milestones_ms[milestone] = elapsed_ms
            self.identities[milestone] = identities

    def document(self):
        return {**vars(self), "missing_milestones": [m for m in MILESTONES if m not in self.milestones_ms]}


class CommittedSubmissionProbe:
    def __init__(self, sessions, *, preparation_artifact_ids=()):
        self._sessions = sessions
        self._preparation_artifact_ids = frozenset(preparation_artifact_ids)

    async def read(self, receipt):
        """A new read transaction per poll prevents an old snapshot masking commit."""
        loop_id, intent_id = receipt["loop_id"], receipt["intent_id"]
        milestones, stages = {}, []
        async with self._sessions() as session:
            loop = await session.get(AgentLoop, loop_id)
            # Pending/deferred inputs stay in causal_chain. Addressed inputs may
            # leave that chain but remain explicit sources of the frozen Mission.
            # A newer decision in the same Loop alone is insufficient.
            mission_source = func.jsonb_path_exists(
                LoopObservation.envelope["mission"]["input_sources"],
                cast(literal("$.*[*] ? (@.intent_id == $intent)"), JSONPATH),
                func.jsonb_build_object(literal("intent"), literal(intent_id)),
            )
            decisions = (await session.scalars(select(LoopDecision).join(
                LoopObservation, LoopObservation.observation_id == LoopDecision.observation_id,
            ).where(
                LoopDecision.loop_id == loop_id,
                LoopObservation.loop_id == loop_id,
                LoopDecision.authority_committed_at.is_not(None),
                or_(LoopObservation.envelope["causal_chain"].contains([{"intent_id": intent_id}]), mission_source),
            ).order_by(LoopDecision.authority_committed_at, LoopDecision.decision_id))).all()
            decisions_by_id = {decision.decision_id: decision for decision in decisions}
            decision_ids = tuple(decision.decision_id for decision in decisions)
            created_contexts = {}
            for decision in decisions:
                binding = {"decision_id": decision.decision_id, "observation_id": decision.observation_id,
                           "round_id": decision.round_id}
                milestones.setdefault("patrol_decision_committed", binding)
                actions = (await session.scalars(select(LoopAction).where(
                    LoopAction.decision_id == decision.decision_id, LoopAction.status == "applied",
                    LoopAction.action_type.in_(("create_lane", "merge_contexts")),
                ).order_by(LoopAction.position))).all()
                for action in actions:
                    context_id = action.result.get("context_id")
                    revision_id = action.result.get("context_revision_id")
                    revision = await session.get(ContextRevision, revision_id) if revision_id else None
                    publication = await session.get(ContextPublicationReceipt, revision_id) if revision_id else None
                    if (revision is None or publication is None or revision.context_id != context_id
                            or publication.context_id != context_id or revision.deleted_at is not None
                            or revision.projection_status not in {"valid", "repaired", "approved"}
                            or revision.checkpoint_id is None):
                        continue
                    context_binding = {**binding, "context_id": context_id, "revision_id": revision_id}
                    milestones.setdefault("derived_context_available", context_binding)
                    created_contexts.setdefault((context_id, revision_id), decision)
            # A Primary continuation can make progress before any new Context
            # exists. Run/output milestones therefore have independent gates.
            runs = (await session.execute(select(
                DesktopRun.run_id, DesktopRun.task_id, DesktopRun.context_revision_id,
                LoopDirective.decision_id, LoopJournalEvent.event_id,
            ).join(LoopDirective, LoopDirective.directive_id == DesktopRun.directive_id).join(
                LoopJournalEvent, (LoopJournalEvent.loop_id == DesktopRun.loop_id)
                & (LoopJournalEvent.kind == "context.run.started")
                & (LoopJournalEvent.payload["run_id"].astext == DesktopRun.run_id),
            ).where(
                DesktopRun.loop_id == loop_id, LoopDirective.decision_id.in_(decision_ids),
                LoopDirective.loop_id == loop_id,
                LoopDirective.target_context_id == DesktopRun.task_id,
                LoopDirective.target_context_revision_id == DesktopRun.context_revision_id,
            ).order_by(LoopJournalEvent.sequence))).all()
            run_bindings = {}
            for run_id, context_id, revision_id, run_decision_id, event_id in runs:
                run_decision = decisions_by_id[run_decision_id]
                run_binding = {"decision_id": run_decision_id, "observation_id": run_decision.observation_id,
                               "round_id": run_decision.round_id, "context_id": context_id,
                               "revision_id": revision_id, "run_id": run_id}
                creation = created_contexts.get((context_id, revision_id))
                if creation is not None:
                    run_binding.update(context_decision_id=creation.decision_id,
                        context_observation_id=creation.observation_id, context_round_id=creation.round_id)
                run_bindings[run_id] = run_binding
                milestones.setdefault("first_run_started", {**run_binding, "event_id": event_id})
            if run_bindings:
                output = (await session.execute(select(
                    LoopJournalEvent.event_id, LoopJournalEvent.payload["run_id"].astext,
                ).where(LoopJournalEvent.loop_id == loop_id,
                    LoopJournalEvent.kind == "context.output.available",
                    LoopJournalEvent.payload["run_id"].astext.in_(tuple(run_bindings)),
                ).order_by(LoopJournalEvent.sequence).limit(1))).first()
                if output:
                    milestones["first_effective_output"] = {**run_bindings[output[1]], "event_id": output[0]}
            # Keep stage records separate, including pre-decision blockers. This
            # is diagnostic round scope, not an assertion of causal wall time.
            round_ids = {receipt.get("round_id")}
            round_ids.update(decision.round_id for decision in decisions)
            rows = (await session.scalars(select(LoopContextDerivationArtifact).where(
                LoopContextDerivationArtifact.loop_id == loop_id,
                LoopContextDerivationArtifact.round_id.in_(tuple(r for r in round_ids if r)),
            ).order_by(LoopContextDerivationArtifact.created_at, LoopContextDerivationArtifact.artifact_id))).all()
            for row in rows:
                record = row.payload.get("stage_record", row.payload)
                if "duration_ms" in record:
                    stages.append({"artifact_id": row.artifact_id, "round_id": row.round_id,
                                   "stage": row.stage, "outcome": row.outcome,
                                   "phase": "preparation" if row.artifact_id in self._preparation_artifact_ids else "measurement",
                                   **{k: record[k] for k in ("duration_ms", "version", "failure_code") if k in record}})
                    diagnostics = ("role", "index_phase", "attempt", "model_calls", "input_tokens", "output_tokens",
                                   "queue_wait_ms", "execution_ms", "admission_ms", "mode", "local_cache_hits",
                                   "reused_segments", "recomputed_segments", "local_projection_ms", "interpretation_ms")
                    stages[-1]["attempts"] = [{k: a[k] for k in diagnostics if k in a} for a in row.attempt_records]
            # waiting_reason can be a free-form model/user clarification prompt.
            # Export only identifier-shaped codes, never that prompt's body.
            reason = loop.waiting_reason if loop is not None else None
            safe_reason = reason if reason and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason) else ("loop_waiting_reason" if reason else None)
            state = None if loop is None else {"status": loop.status, "reason": safe_reason}
        return milestones, stages, state


async def measure_submission(submit, probe, *, scenario, repeat, timeout_seconds=300, poll_seconds=0.1,
                             after_acceptance=None):
    """Start immediately before submission; return partial samples on failures."""
    if (scenario not in SCENARIOS or not isfinite(timeout_seconds) or timeout_seconds <= 0
            or not isfinite(poll_seconds) or poll_seconds <= 0):
        raise ValueError("invalid benchmark settings")
    sample = SubmissionSample(scenario, repeat)
    started = perf_counter()
    try:
        async with asyncio.timeout(timeout_seconds):
            receipt = await submit()
            sample.observe("durable_acceptance", (perf_counter() - started) * 1000,
                           {k: receipt[k] for k in ("submission_id", "loop_id", "intent_id", "observation_id", "round_id")})
            if after_acceptance is not None:
                await after_acceptance()
            while True:
                milestones, sample.stages, state = await probe.read(receipt)
                elapsed = (perf_counter() - started) * 1000
                for name, identities in milestones.items():
                    sample.observe(name, elapsed, identities)
                if all(name in sample.milestones_ms for name in MILESTONES):
                    sample.outcome = "complete"
                    break
                if state and state["status"] in {"waiting_user", "paused", "stopped", "completed", "failed", "cancelled"}:
                    sample.outcome = state["status"]
                    sample.failure_code = state["reason"]
                    break
                await asyncio.sleep(poll_seconds)
    except TimeoutError:
        sample.outcome = "timeout"
    except Exception as exc:
        sample.outcome, sample.failure_code = "error", type(exc).__name__
    finally:
        sample.observation_ms = (perf_counter() - started) * 1000
        for name, elapsed in tuple(sample.milestones_ms.items()):
            if elapsed > timeout_seconds * 1000:
                sample.discarded_milestones_ms[name] = sample.milestones_ms.pop(name)
        if sample.discarded_milestones_ms and sample.outcome == "complete":
            sample.outcome, sample.failure_code = "timeout", "observation_deadline_exceeded"
        if sample.observation_ms > (timeout_seconds + 10) * 1000:
            sample.outcome, sample.failure_code = "observer_interrupted", "observation_deadline_exceeded"
    return sample


def summarize(samples):
    """Nearest-rank percentiles, conditional on observing each milestone."""
    summary = {}
    for scenario in SCENARIOS:
        selected = [s for s in samples if s.scenario == scenario]
        metrics = {}
        for name in MILESTONES:
            values = sorted(s.milestones_ms[name] for s in selected if name in s.milestones_ms)
            metrics[name] = {"observed": len(values), "missing": len(selected) - len(values),
                             "p50_ms": values[ceil(len(values) * .5) - 1] if values else None,
                             "p95_ms": values[ceil(len(values) * .95) - 1] if values else None,
                             "p95_sample_sufficient": len(values) >= 20}
        summary[scenario] = {"samples": len(selected), "completed": sum(s.outcome == "complete" for s in selected),
                             "outcomes": {outcome: sum(s.outcome == outcome for s in selected)
                                          for outcome in sorted({s.outcome for s in selected})},
                             "metrics": metrics}
    return {"clock": "submitter perf_counter; post-commit polling upper bounds",
            "percentiles": "nearest rank, observed milestones only; missing samples are not zero",
            "p95_note": "Fewer than 20 observations cannot resolve the upper 5%; 20 is a minimum, not confidence evidence.",
            "scenarios": summary, "raw_samples": [s.document() for s in samples]}
