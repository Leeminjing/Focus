r"""本文件对外提供 semantic Context derivation lifecycle 的纯状态机测试。

输入为 indexing、retrieval、reconciliation、resolution、synthesis、quality、compilation、authorization 与 publication 状态序列；
输出为合法顺序、缺失 synthesis 阻断、终态封闭和跳阶段拒绝断言。具体工作流为逐步调用
ExpansionLifecycleStateMachine，不连接数据库。示例：
`pytest backend/tests/test_semantic_context_lifecycle.py -q`。
"""

from __future__ import annotations

import pytest

from backend.app.desktop.agent_loop.context_expansion.lifecycle import (
    ExpansionLifecycleStateMachine,
    ExpansionTransitionRejected,
)


def test_semantic_lifecycle_records_each_causal_stage() -> None:
    machine = ExpansionLifecycleStateMachine()
    sequence = (
        "signals_collected",
        "indexes_ready",
        "portfolio_projected",
        "retrieval_planned",
        "work_planned",
        "work_reconciled",
        "admitted",
        "proposed",
        "evidence_resolved",
        "dossier_built",
        "quality_verified",
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

    with pytest.raises(ExpansionTransitionRejected):
        machine.validate("synthesis_omitted", "compiled")

    assert machine.validate("synthesis_omitted", "blocked") == "blocked"
