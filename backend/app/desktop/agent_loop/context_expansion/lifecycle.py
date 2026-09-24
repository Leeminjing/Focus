r"""本文件对外提供 ExpansionLifecycleStateMachine。

输入为当前 expansion state 与目标 state；输出为合法目标 state 或 ExpansionTransitionRejected。具体工作流为
只允许 signals、index、projection、retrieval、planning、reconciliation、admission、resolution、synthesis、quality、compilation 到 dispatched 的单向权威路径及
declined、blocked、failed、superseded 终态，重复转换保持幂等。示例：`state = machine.validate("proposed", "evidence_resolved")`。
"""

from __future__ import annotations

from typing import ClassVar

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    ExpansionLifecycleState,
)


class ExpansionTransitionRejected(ValueError):
    pass


class ExpansionLifecycleStateMachine:
    _TRANSITIONS: ClassVar[dict[str, frozenset[str]]] = {
        "signals_collected": frozenset({"indexes_ready", "blocked", "failed", "superseded"}),
        "indexes_ready": frozenset({"portfolio_projected", "blocked", "failed", "superseded"}),
        "portfolio_projected": frozenset({"retrieval_planned", "blocked", "failed", "superseded"}),
        "retrieval_planned": frozenset({"work_planned", "blocked", "failed", "superseded"}),
        "work_planned": frozenset({"work_reconciled", "declined", "blocked", "failed", "superseded"}),
        "work_reconciled": frozenset({"admitted", "declined", "blocked", "failed", "superseded"}),
        "admitted": frozenset({"proposed", "declined", "blocked", "failed", "superseded"}),
        "proposed": frozenset({"evidence_resolved", "declined", "blocked", "failed", "superseded"}),
        "evidence_resolved": frozenset({"dossier_built", "synthesis_omitted", "blocked", "failed", "superseded"}),
        "dossier_built": frozenset({"quality_verified", "blocked", "failed", "superseded"}),
        "synthesis_omitted": frozenset({"blocked", "failed", "superseded"}),
        "quality_verified": frozenset({"compiled", "blocked", "failed", "superseded"}),
        "compiled": frozenset({"authorized", "blocked", "failed", "superseded"}),
        "authorized": frozenset({"committed", "blocked", "failed", "superseded"}),
        "committed": frozenset({"dispatched", "failed"}),
        "dispatched": frozenset(),
        "declined": frozenset(),
        "blocked": frozenset(),
        "failed": frozenset(),
        "superseded": frozenset(),
    }

    def validate(self, current: str, target: str) -> ExpansionLifecycleState:
        if current == target and target in self._TRANSITIONS:
            return target
        if target not in self._TRANSITIONS.get(current, frozenset()):
            raise ExpansionTransitionRejected(f"非法 expansion transition: {current} -> {target}")
        return target
