r"""本文件对外提供 Context-governed Agent Loop 领域的稳定公共入口。

输入为用户 goal/grant、bounded observation、Patrol decision 和 completion evidence；输出为持久 Loop、
唯一 Kernel commit、可审计用户意图、纯 HumanMessage directive 与可恢复 coordinator。具体工作流为 User delegates、
Patrol judges、optional Workers return、Kernel commits。示例：`from ...agent_loop import LoopKernel`。
"""

from backend.app.desktop.agent_loop.authority import AuthorityViolation, DelegatedAuthorityGuard
from backend.app.desktop.agent_loop.authority_control import LoopAuthorityService
from backend.app.desktop.agent_loop.completion import CompletionEvidenceService, CompletionGuard, CompletionGuardResult, CompletionVerifierPort
from backend.app.desktop.agent_loop.coordinator import CoordinatorClaim, LoopCoordinator, LoopCoordinatorRuntime
from backend.app.desktop.agent_loop.kernel import KernelCommitResult, KernelRejected, LoopKernel
from backend.app.desktop.agent_loop.observation import LoopObservationBuilder, observation_hash
from backend.app.desktop.agent_loop.provenance import DelegatedDirectiveFactory, MessageHistoryProjector
from backend.app.desktop.agent_loop.patrol import PatrolContractViolation, PatrolDecisionModel, PortfolioPatrol
from backend.app.desktop.agent_loop.dispatch import DesktopDirectiveLaunchPort, DirectiveLaunchPort, LoopRunWorkspaceBinder, LoopWaveDispatcher
from backend.app.desktop.agent_loop.gates import PendingDecisionContract, PendingDecisionProjector
from backend.app.desktop.agent_loop.budgets import BudgetDecision, LoopBudgetGuard, no_progress_fingerprint
from backend.app.desktop.agent_loop.recovery import AgentLoopRecovery, LoopRecoveryReport
from backend.app.desktop.agent_loop.round_orchestration import LoopObservationService, LoopRoundOrchestrator, StructuredPatrolDecisionModel
from backend.app.desktop.agent_loop.workers import LoopWorkerRuntime, StructuredCompletionVerifier, StructuredLaneAdvisor
from backend.app.desktop.agent_loop.portfolio_publication import LoopPortfolioPublicationService
from backend.app.desktop.agent_loop.workspace_adoption import LoopWorkspaceAdoptionService
from backend.app.desktop.agent_loop.schemas import CompletionVerificationContract, CriterionVerification, LoopBudgetContract, LoopCreateRequest, LoopObservationEnvelope, PatrolAction, PatrolDecisionIntent
from backend.app.desktop.agent_loop.service import AgentLoopService
from backend.app.desktop.agent_loop.interventions import LoopInterventionService

__all__ = [
    "AgentLoopService", "LoopInterventionService", "LoopAuthorityService", "AuthorityViolation", "CompletionEvidenceService", "CompletionGuard", "CompletionGuardResult",
    "CompletionVerificationContract", "CompletionVerifierPort", "CoordinatorClaim", "CriterionVerification",
    "DelegatedAuthorityGuard", "DelegatedDirectiveFactory", "KernelCommitResult", "KernelRejected",
    "DesktopDirectiveLaunchPort", "DirectiveLaunchPort", "LoopRunWorkspaceBinder", "LoopBudgetContract", "LoopCoordinator", "LoopCoordinatorRuntime", "LoopCreateRequest", "LoopKernel", "LoopObservationBuilder",
    "LoopObservationEnvelope", "MessageHistoryProjector", "PatrolAction", "PatrolDecisionIntent", "observation_hash",
    "PatrolDecisionModel", "PortfolioPatrol", "PatrolContractViolation", "LoopWaveDispatcher",
    "PendingDecisionContract", "PendingDecisionProjector",
    "BudgetDecision", "LoopBudgetGuard", "no_progress_fingerprint",
    "AgentLoopRecovery", "LoopRecoveryReport",
    "LoopObservationService", "LoopRoundOrchestrator", "StructuredPatrolDecisionModel",
    "LoopWorkerRuntime", "StructuredCompletionVerifier", "StructuredLaneAdvisor",
    "LoopPortfolioPublicationService",
    "LoopWorkspaceAdoptionService",
]
