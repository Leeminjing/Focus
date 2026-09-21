r"""本文件对外提供 ExpansionLifecycleStateMachine。

输入为当前 expansion state 与目标 state；输出为合法目标 state 或 ExpansionTransitionRejected。具体工作流为
只允许 detected 到 dispatched 的单向权威路径及 declined、blocked、failed、superseded 终态，重复转换保持幂等。
示例：`state = machine.validate("proposed", "compiled")`。
"""

from __future__ import annotations

from backend.app.desktop.agent_loop.context_expansion.contracts import ExpansionLifecycleState


class ExpansionTransitionRejected(ValueError):
    pass


class ExpansionLifecycleStateMachine:
    _TRANSITIONS: dict[str, frozenset[str]] = {
        "detected": frozenset({"curated", "proposed", "declined", "blocked", "failed", "superseded"}),
        "curated": frozenset({"proposed", "declined", "blocked", "failed", "superseded"}),
        "proposed": frozenset({"compiled", "declined", "blocked", "failed", "superseded"}),
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
