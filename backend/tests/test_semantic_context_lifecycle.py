r"""本文件对外提供 semantic Context derivation lifecycle 的纯状态机测试。

输入为 planning、resolution、dossier omission、compilation、authorization 与 publication 状态序列；输出为合法顺序、终态封闭和
跳阶段拒绝断言。具体工作流为逐步调用 ExpansionLifecycleStateMachine，不连接数据库。示例：
`pytest backend/tests/test_semantic_context_lifecycle.py -q`。
"""

from __future__ import annotations

import pytest

from backend.app.desktop.agent_loop.context_expansion.lifecycle import (
    ExpansionLifecycleStateMachine,
    ExpansionTransitionRejected,
)


@pytest.mark.parametrize("synthesis_state", ("dossier_built", "synthesis_omitted"))
def test_semantic_lifecycle_records_each_causal_stage(synthesis_state: str) -> None:
    machine = ExpansionLifecycleStateMachine()
    sequence = (
        "signals_collected",
        "portfolio_projected",
        "work_planned",
        "admitted",
        "proposed",
        "evidence_resolved",
        synthesis_state,
        "compiled",
        "authorized",
        "committed",
        "dispatched",
    )

    current = sequence[0]
    for target in sequence[1:]:
        current = machine.validate(current, target)

    assert current == "dispatched"


def test_lifecycle_rejects_compilation_before_resolution_and_synthesis() -> None:
    machine = ExpansionLifecycleStateMachine()

    with pytest.raises(ExpansionTransitionRejected):
        machine.validate("proposed", "compiled")

    with pytest.raises(ExpansionTransitionRejected):
        machine.validate("dispatched", "proposed")
