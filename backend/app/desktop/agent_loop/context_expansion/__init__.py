r"""本文件对外提供 semantic Context derivation 的稳定公共 interface。

输入为冻结 Loop observation、WorkContextSpec、typed evidence 与 identity-only Patrol intent；输出为 manifests、resolution、
assessment、compiled Context 和 lifecycle 合同。具体工作流为调用方从本入口导入 coordinator 与不可变公共合同，
signal/projector/planner/resolver/compiler helper 保持分模块实现。示例：`assessment = await ContextExpansionCoordinator().assess(observation)`。
"""

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    CompiledExpansion,
    DeclineExpansionIntent,
    EvidenceRequirement,
    ExpansionAssessment,
    ExpansionBlocker,
    ExpansionOpportunity,
    ExpansionOutcome,
    ResolvedEvidenceBundle,
    SpawnContextIntent,
    WorkContextSpec,
)
from backend.app.desktop.agent_loop.context_expansion.coordinator import (
    ContextExpansionCoordinator,
)

__all__ = [
    "CompiledExpansion",
    "ContextExpansionCoordinator",
    "DeclineExpansionIntent",
    "EvidenceRequirement",
    "ExpansionAssessment",
    "ExpansionBlocker",
    "ExpansionOpportunity",
    "ExpansionOutcome",
    "ResolvedEvidenceBundle",
    "SpawnContextIntent",
    "WorkContextSpec",
]
