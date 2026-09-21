r"""本文件对外提供 Context expansion Module 的稳定公共 interface。

输入为冻结 Loop observation、可选 Curator proposal 与语义派生意图；输出为确定性 assessment、内部编译结果和
生命周期合同。具体工作流为调用方只从本入口导入 coordinator 与不可变合同，检测、策略和 helper 保持包内实现。
示例：`assessment = await ContextExpansionCoordinator().assess(observation)`。
"""

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    CompiledExpansion,
    DeclineExpansionIntent,
    ExpansionAssessment,
    ExpansionBlocker,
    ExpansionOpportunity,
    ExpansionOutcome,
    SpawnContextIntent,
)
from backend.app.desktop.agent_loop.context_expansion.coordinator import ContextExpansionCoordinator

__all__ = [
    "CompiledExpansion",
    "ContextExpansionCoordinator",
    "DeclineExpansionIntent",
    "ExpansionAssessment",
    "ExpansionBlocker",
    "ExpansionOpportunity",
    "ExpansionOutcome",
    "SpawnContextIntent",
]
