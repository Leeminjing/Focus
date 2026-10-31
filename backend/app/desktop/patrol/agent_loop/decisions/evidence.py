"""Action-specific frozen evidence admission and durable Patrol evidence requests.

Requests use the existing Worker queue, cancellation, retry and recovery contracts.
Admission never grants execution authority; Kernel still checks live control facts.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.desktop.patrol.agent_loop.patrol import PatrolContractViolation


EvidenceKind = Literal["curator", "expansion_assessment"]


class PatrolEvidenceNeeds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    needs_evidence: tuple[EvidenceKind, ...] = Field(min_length=1, max_length=2)

    @field_validator("needs_evidence")
    @classmethod
    def unique(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("needs_evidence 不得重复")
        return value


class PatrolEvidencePolicy:
    """Only derivation actions need the complete derivation pipeline.

    A missing assessment is unknown, not a negative assessment. Frozen required
    WorkSpecs force assessment even when the proposal only continues a Context.
    """

    @staticmethod
    def missing(actions, observation) -> tuple[EvidenceKind, ...]:
        if observation.expansion_assessment is not None:
            return ()
        derivation = any(a.action in {"spawn_context", "decline_expansion"} for a in actions)
        required = bool(observation.expansion_obligations) or any(
            spec.get("required")
            for worker in observation.worker_results
            if worker.get("kind") == "lane_curator" and worker.get("round_id") == observation.round_id
            for spec in (worker.get("result") or {}).get("work_specs", ())
        )
        return ("expansion_assessment",) if derivation or required else ()

    @staticmethod
    def validate(actions, observation) -> None:
        if PatrolEvidencePolicy.missing(actions, observation):
            raise PatrolContractViolation("evidence_required: expansion_assessment 尚未准备")
        from backend.app.desktop.patrol.agent_loop.patrol_contract import PatrolDecisionContract

        PatrolDecisionContract()._validate_expansion_decision(actions, observation)
        frontier = {item["context_id"]: item for item in observation.portfolio_frontier}
        capabilities = set(observation.grant.get("capabilities") or ())
        scope = set(observation.grant.get("context_scope") or ())
        cancelled = {a.target_id for a in actions if a.action == "cancel_execution" and a.target_kind == "run"}
        for action in actions:
            if action.action == "continue_context":
                source = frontier.get(action.context_id)
                if source is None or source.get("revision_id") != action.context_revision_id:
                    raise PatrolContractViolation("continue_context 必须引用冻结 Context Revision")
                if action.context_id not in scope or "continue_context" not in capabilities:
                    raise PatrolContractViolation("continue_context 缺少冻结授权")
                if any(item.get("context_id") == action.context_id
                       and item.get("status") in {"pending", "queued", "running"}
                       and item.get("run_id") not in cancelled for item in observation.stable_results):
                    raise PatrolContractViolation("continue_context 存在未取消的运行冲突")
            elif action.action == "merge_contexts":
                if observation.committed_lineage is None:
                    raise PatrolContractViolation("merge_contexts 缺少冻结 Lineage")
                from backend.app.desktop.contexts.evolution.lineage_contracts import LineageSnapshot

                try:
                    lineage = LineageSnapshot.model_validate(observation.committed_lineage)
                except ValueError as exc:
                    raise PatrolContractViolation("merge_contexts 冻结 Lineage 不完整") from exc
                if len(action.plan.source_frontier) < 2:
                    raise PatrolContractViolation("merge_contexts 必须包含至少两个精确来源")
                for ref in action.plan.source_frontier:
                    source = frontier.get(ref.context_id) or {}
                    checkpoint = source.get("checkpoint_id") or (source.get("revision") or {}).get("checkpoint_id")
                    if ((source.get("revision_id"), checkpoint) != (ref.revision_id, ref.checkpoint_id)
                            or lineage.roots.get(ref.context_id) != ref.revision_id):
                        raise PatrolContractViolation("merge_contexts 来源 Revision/checkpoint 不匹配")
                if "merge_contexts" not in capabilities or any(ref.context_id not in scope for ref in action.plan.source_frontier):
                    raise PatrolContractViolation("merge_contexts 缺少来源授权或合并授权")
            elif action.action == "adopt_workspace_result":
                if "adopt_workspace_result" not in capabilities:
                    raise PatrolContractViolation("adopt_workspace_result 缺少冻结授权")
                slots = observation.workspace.get("isolated_slots") or ()
                source = next((s for s in slots if s.get("slot_id") == action.source_slot_id), None)
                if source is None or source.get("revision") != action.source_revision:
                    raise PatrolContractViolation("adopt_workspace_result 工作区版本不匹配")
                # These are frozen database facts, never model assertions.
                admission = source.get("adoption_admission") or {}
                if admission.get("status") != "ready":
                    raise PatrolContractViolation("adopt_workspace_result 缺少冲突检查或独立验证事实")
                if admission.get("target_revision") != observation.workspace.get("revision"):
                    raise PatrolContractViolation("adopt_workspace_result 目标版本证据不匹配")
            elif action.action == "request_completion":
                from backend.app.desktop.patrol.agent_loop.completion.completion_eligibility import CompletionEligibilityPolicy, CompletionEligibilityRejected

                if observation.completion_eligibility is None:
                    raise PatrolContractViolation("request_completion 缺少完成验证资格证据")
                try:
                    CompletionEligibilityPolicy.require_allowed(observation.completion_eligibility, action)
                except CompletionEligibilityRejected as exc:
                    raise PatrolContractViolation(str(exc)) from exc


async def workspace_admission_facts(session, loop_id, slots, target, adoptions):
    """Freeze exact writer/reviewer anchors and already recorded conflicts.

    This only admits a proposal. Git patch checks and current fingerprints remain
    the responsibility of the existing adoption port at Kernel commit.
    """
    from sqlalchemy import select
    from backend.app.desktop.persistence.models import DesktopRun
    from backend.app.desktop.workspaces.coordination.models import RunExecutionAnchor

    rows = (await session.execute(select(DesktopRun, RunExecutionAnchor).join(
        RunExecutionAnchor, RunExecutionAnchor.run_id == DesktopRun.run_id).where(
        DesktopRun.loop_id == loop_id,
        RunExecutionAnchor.slot_id.in_([slot.slot_id for slot in slots])))).all() if slots else ()
    result = {}
    for slot in slots:
        candidates = [(run, anchor) for run, anchor in rows if anchor.slot_id == slot.slot_id]
        settled = [(run, anchor) for run, anchor in candidates
            if run.status == "success" and run.settled_at is not None
            and anchor.settled_at is not None and anchor.resulting_fingerprint == slot.current_fingerprint]
        writers = [(run, anchor) for run, anchor in settled
            if (run.workspace_anchor or {}).get("mode") == "write"
            and anchor.resulting_workspace_revision == slot.revision]
        reviews = [(run, anchor) for run, anchor in settled
            if (run.workspace_anchor or {}).get("mode") == "read"
            and anchor.observed_workspace_revision == slot.revision
            and anchor.observed_fingerprint == slot.current_fingerprint
            and any(writer.task_id != run.task_id for writer, _ in writers)]
        conflicts = [item.adoption_id for item in adoptions if item.source_slot_id == slot.slot_id
            and item.source_revision == slot.revision and item.status in {"conflict", "waiting_user", "adopting", "adopted"}]
        active = any(run.status in {"pending", "running"} for run, _ in candidates)
        ready = bool(target and slot.base_revision and writers and reviews and not conflicts and not active)
        result[slot.slot_id] = {"status": "ready" if ready else "blocked",
            "target_revision": target.revision if target else None,
            "writer_run_ids": [run.run_id for run, _ in writers],
            "review_run_ids": [run.run_id for run, _ in reviews], "conflict_ids": conflicts}
    return result
