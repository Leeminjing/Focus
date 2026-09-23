r"""本文件对外提供 ExpansionSignal、ExpansionSignalSet 与 ExpansionSignalCollector。

输入为冻结 `LoopObservationEnvelope`；输出为只描述 Mission、failure、Token、用户 authority、预算和 Workspace
事实的稳定 signal 集合。具体工作流为读取结构化字段、规范化事实并生成 identity，不创建工作目标、Context 类型、
workspace mode 或证据选择。示例：`signals = ExpansionSignalCollector().collect(observation)`。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    stable_expansion_hash,
)
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope

SignalKind = Literal[
    "mission_structure",
    "failure_recurrence",
    "token_pressure",
    "user_authority",
    "budget_state",
    "workspace_state",
    "portfolio_shape",
]


class _SignalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExpansionSignal(_SignalModel):
    signal_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: SignalKind
    facts: tuple[tuple[str, str], ...]

    @classmethod
    def create(cls, kind: SignalKind, facts: dict[str, Any]) -> ExpansionSignal:
        normalized = tuple(sorted((str(key), cls._value(value)) for key, value in facts.items()))
        return cls(
            signal_id=stable_expansion_hash("expansion-signal", kind, normalized),
            kind=kind,
            facts=normalized,
        )

    @staticmethod
    def _value(value: Any) -> str:
        if isinstance(value, (list, tuple, set, frozenset)):
            return "|".join(sorted(str(item) for item in value))
        return str(value)


class ExpansionSignalSet(_SignalModel):
    observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    signals: tuple[ExpansionSignal, ...]


class ExpansionSignalCollector:
    VERSION = "semantic-expansion-signals-v1"

    def collect(self, observation: LoopObservationEnvelope) -> ExpansionSignalSet:
        signals = (
            self._mission(observation),
            self._failures(observation),
            self._tokens(observation),
            self._authority(observation),
            self._budget(observation),
            self._workspace(observation),
            self._portfolio(observation),
        )
        present = tuple(signal for signal in signals if signal is not None)
        identity = stable_expansion_hash(
            "signal-observation",
            self.VERSION,
            observation.loop_id,
            observation.round_id,
            observation.observed_frontier_hash,
            tuple(signal.signal_id for signal in present),
        )
        return ExpansionSignalSet(observation_hash=identity, signals=present)

    def _mission(self, observation: LoopObservationEnvelope) -> ExpansionSignal:
        mission = observation.mission or observation.goal or {}
        checks = tuple(mission.get("completion_checks") or ())
        return ExpansionSignal.create(
            "mission_structure",
            {
                "goal_revision": observation.goal_revision,
                "outcome": mission.get("outcome") or mission.get("goal") or "",
                "completion_check_ids": tuple(
                    item.get("check_id") or item.get("criterion_id") or ""
                    for item in checks
                ),
                "completion_check_count": len(checks),
            },
        )

    def _failures(self, observation: LoopObservationEnvelope) -> ExpansionSignal | None:
        failures = tuple(
            item
            for item in observation.stable_results
            if str(item.get("status") or "").casefold() in {"error", "failed", "failure"}
        )
        if not failures:
            return None
        return ExpansionSignal.create(
            "failure_recurrence",
            {
                "count": len(failures),
                "run_ids": tuple(item.get("run_id") or "" for item in failures),
                "context_ids": tuple(item.get("context_id") or "" for item in failures),
                "no_progress_count": (observation.budget.get("usage") or {}).get("no_progress_count", 0),
            },
        )

    def _tokens(self, observation: LoopObservationEnvelope) -> ExpansionSignal | None:
        limits = observation.budget.get("limits") or {}
        usage = observation.budget.get("usage") or {}
        limit = int(limits.get("max_input_tokens", 0) or 0)
        used = int(usage.get("input_tokens", 0) or 0)
        if limit <= 0:
            return None
        return ExpansionSignal.create(
            "token_pressure",
            {"used": used, "limit": limit, "ratio_millis": int(used * 1000 / limit)},
        )

    def _authority(self, observation: LoopObservationEnvelope) -> ExpansionSignal | None:
        intents = tuple(item for item in observation.user_intents if item.get("scope") == "portfolio")
        if not intents:
            return None
        return ExpansionSignal.create(
            "user_authority",
            {
                "intent_ids": tuple(item.get("intent_id") or "" for item in intents),
                "intent_count": len(intents),
            },
        )

    def _workspace(self, observation: LoopObservationEnvelope) -> ExpansionSignal:
        workspace = observation.workspace or {}
        return ExpansionSignal.create(
            "workspace_state",
            {
                "revision": workspace.get("revision") or 0,
                "fingerprint": workspace.get("fingerprint") or "",
                "parallel_write_required": bool(workspace.get("parallel_write_required")),
                "requires_isolation": bool(workspace.get("requires_isolation")),
            },
        )

    def _budget(self, observation: LoopObservationEnvelope) -> ExpansionSignal:
        limits = observation.budget.get("limits") or {}
        usage = observation.budget.get("usage") or {}
        return ExpansionSignal.create(
            "budget_state",
            {
                "max_contexts": limits.get("max_contexts", 0),
                "max_lanes": limits.get("max_lanes", 0),
                "max_new_lanes_per_round": limits.get("max_new_lanes_per_round", 0),
                "max_concurrent_runs": limits.get("max_concurrent_runs", 0),
                "contexts": usage.get("contexts", len(observation.portfolio_frontier)),
                "lanes": usage.get("lanes", len(observation.portfolio_frontier)),
                "rounds": usage.get("rounds", 0),
            },
        )

    def _portfolio(self, observation: LoopObservationEnvelope) -> ExpansionSignal:
        return ExpansionSignal.create(
            "portfolio_shape",
            {
                "context_count": len(observation.portfolio_frontier),
                "roles": tuple(item.get("role") or "" for item in observation.portfolio_frontier),
                "revision_ids": tuple(item.get("revision_id") or "" for item in observation.portfolio_frontier),
            },
        )
