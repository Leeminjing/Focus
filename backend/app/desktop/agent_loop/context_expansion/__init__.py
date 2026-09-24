r"""本文件对外提供 semantic Context derivation 的稳定公共 interface。

输入为冻结 Loop observation、完整 Revision indexes、retrieval session、WorkContextSpec、typed evidence 与 identity-only Patrol intent；
输出为 catalog、reconciliation、claim dossier、quality assessment、compiled Context 和 lifecycle 合同。具体工作流为调用方从本入口
导入 coordinator 与跨模块不可变合同，index/retrieval/planning/reconciliation/resolution/synthesis/quality/compiler helper 保持单一职责。
示例：`assessment = await ContextExpansionCoordinator().assess(observation)`。
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
from backend.app.desktop.agent_loop.context_expansion.quality import (
    ContextQualityAssessment,
)
from backend.app.desktop.agent_loop.context_expansion.reconciliation import (
    WorkSpecReconciliation,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_index import (
    RevisionSemanticIndex,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import (
    PlanningRetrievalSession,
)
from backend.app.desktop.agent_loop.context_expansion.shadow import (
    SemanticDerivationShadowReport,
)
from backend.app.desktop.agent_loop.context_expansion.synthesis import (
    ValidatedContextDossier,
)

__all__ = [
    "CompiledExpansion",
    "ContextExpansionCoordinator",
    "ContextQualityAssessment",
    "DeclineExpansionIntent",
    "EvidenceRequirement",
    "ExpansionAssessment",
    "ExpansionBlocker",
    "ExpansionOpportunity",
    "ExpansionOutcome",
    "PlanningRetrievalSession",
    "ResolvedEvidenceBundle",
    "RevisionSemanticIndex",
    "SemanticDerivationShadowReport",
    "SpawnContextIntent",
    "ValidatedContextDossier",
    "WorkContextSpec",
    "WorkSpecReconciliation",
]
