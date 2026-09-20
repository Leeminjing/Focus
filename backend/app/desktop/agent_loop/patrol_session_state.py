r"""本文件对外提供 PatrolPhase、PatrolActivity、PatrolSessionStateMachine 与 PatrolTransitionRejected。

输入为当前 phase、目标 phase 和仅含安全摘要/等待目标/证据引用的活动合同；输出为合法的下一 phase 或确定性拒绝。
具体工作流为显式列出正常边、允许 collecting/awaiting/observing 的真实重复事件，并允许任一非终态收敛为
completed、failed、interrupted 或 superseded。示例：`machine.validate("observing", "proposing")`。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class PatrolPhase(StrEnum):
    CREATED = "created"
    FREEZING_OBSERVATION = "freezing_observation"
    OBSERVING = "observing"
    DISPATCHING_CURATORS = "dispatching_curators"
    COLLECTING_CURATORS = "collecting_curators"
    PROPOSING = "proposing"
    AUTHORIZING = "authorizing"
    DELIVERING = "delivering"
    AWAITING_EVIDENCE = "awaiting_evidence"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    SUPERSEDED = "superseded"


class _ActivityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PatrolWaitTarget(_ActivityModel):
    entity_type: str = Field(min_length=1, max_length=80)
    entity_id: str = Field(min_length=1, max_length=120)
    evidence_category: str | None = Field(default=None, max_length=80)


class PatrolEvidenceReference(_ActivityModel):
    kind: str = Field(min_length=1, max_length=80)
    entity_id: str = Field(min_length=1, max_length=120)


class PatrolActivity(_ActivityModel):
    summary: str = Field(min_length=1, max_length=500)
    wait_reason: str | None = Field(default=None, max_length=500)
    wait_targets: tuple[PatrolWaitTarget, ...] = ()
    evidence_refs: tuple[PatrolEvidenceReference, ...] = ()


class PatrolTransitionRejected(ValueError):
    pass


class PatrolSessionStateMachine:
    _TERMINAL = frozenset({PatrolPhase.COMPLETED, PatrolPhase.FAILED, PatrolPhase.INTERRUPTED, PatrolPhase.SUPERSEDED})
    _REPEATABLE = frozenset({PatrolPhase.OBSERVING, PatrolPhase.COLLECTING_CURATORS, PatrolPhase.AWAITING_EVIDENCE})
    _EDGES = {
        PatrolPhase.CREATED: frozenset({PatrolPhase.FREEZING_OBSERVATION}),
        PatrolPhase.FREEZING_OBSERVATION: frozenset({PatrolPhase.OBSERVING}),
        PatrolPhase.OBSERVING: frozenset({PatrolPhase.DISPATCHING_CURATORS, PatrolPhase.PROPOSING}),
        PatrolPhase.DISPATCHING_CURATORS: frozenset({PatrolPhase.COLLECTING_CURATORS}),
        PatrolPhase.COLLECTING_CURATORS: frozenset({PatrolPhase.PROPOSING, PatrolPhase.AWAITING_EVIDENCE}),
        PatrolPhase.PROPOSING: frozenset({PatrolPhase.AUTHORIZING}),
        PatrolPhase.AUTHORIZING: frozenset({PatrolPhase.DELIVERING, PatrolPhase.PUBLISHING, PatrolPhase.AWAITING_EVIDENCE, PatrolPhase.COMPLETED}),
        PatrolPhase.DELIVERING: frozenset({PatrolPhase.AWAITING_EVIDENCE, PatrolPhase.COMPLETED}),
        PatrolPhase.AWAITING_EVIDENCE: frozenset({PatrolPhase.OBSERVING, PatrolPhase.PUBLISHING, PatrolPhase.COMPLETED}),
        PatrolPhase.PUBLISHING: frozenset({PatrolPhase.COMPLETED}),
    }

    def validate(self, current: str, target: str) -> PatrolPhase:
        current_phase = PatrolPhase(current)
        target_phase = PatrolPhase(target)
        if current_phase in self._TERMINAL:
            raise PatrolTransitionRejected(f"terminal phase {current_phase} 不可继续转换")
        if target_phase in self._TERMINAL:
            return target_phase
        if current_phase == target_phase and current_phase in self._REPEATABLE:
            return target_phase
        if target_phase not in self._EDGES.get(current_phase, frozenset()):
            raise PatrolTransitionRejected(f"非法 Patrol phase: {current_phase} -> {target_phase}")
        return target_phase

    @classmethod
    def is_terminal(cls, phase: str) -> bool:
        return PatrolPhase(phase) in cls._TERMINAL
