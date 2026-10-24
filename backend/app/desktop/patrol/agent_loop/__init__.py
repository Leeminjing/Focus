r"""本文件对外提供 Context-governed Agent Loop 领域的稳定公共入口。

输入为用户 goal/grant、bounded observation、Patrol decision 和 completion evidence；输出为持久 Loop、
唯一 Kernel commit、可审计用户意图、纯 HumanMessage directive、独立发布/Curator/Context 池与可恢复 coordinator。
具体工作流为 User delegates、Patrol judges、Kernel authorizes、各持久队列独立推进。示例：`from ...agent_loop import LoopKernel`。
"""

from backend.app.desktop.patrol.agent_loop.governance.authority import AuthorityViolation, DelegatedAuthorityGuard
from backend.app.desktop.patrol.agent_loop.governance.authority_control import LoopAuthorityService
from backend.app.desktop.patrol.agent_loop.completion.service import CompletionEvidenceService, CompletionGuard, CompletionGuardResult, CompletionVerifierPort
from backend.app.desktop.patrol.agent_loop.coordinator import CoordinatorClaim, LoopCoordinator, LoopCoordinatorRuntime
from backend.app.desktop.patrol.agent_loop.kernel import KernelCommitResult, KernelRejected, LoopKernel
from backend.app.desktop.patrol.agent_loop.observations.builder import LoopObservationBuilder, observation_hash
from backend.app.desktop.patrol.agent_loop.governance.provenance import DelegatedDirectiveFactory, MessageHistoryProjector
from backend.app.desktop.patrol.agent_loop.patrol import PatrolContractViolation, PatrolDecisionModel, PortfolioPatrol
from backend.app.desktop.patrol.agent_loop.patrol_contract import BoundaryReference, CompletionCheckReference, MissionReference, OutcomeReference, PatrolDecisionContract
from backend.app.desktop.patrol.agent_loop.execution.dispatch import DesktopDirectiveLaunchPort, DirectiveLaunchPort, LoopRunWorkspaceBinder, LoopWaveDispatcher
from backend.app.desktop.patrol.agent_loop.governance.gates import PendingDecisionContract, PendingDecisionProjector
from backend.app.desktop.patrol.agent_loop.resources.budgets import BudgetDecision, LoopBudgetGuard, no_progress_fingerprint
from backend.app.desktop.patrol.agent_loop.recovery.service import AgentLoopRecovery, LoopRecoveryReport
from backend.app.desktop.patrol.agent_loop.rounds.round_orchestration import LoopObservationService, LoopRoundOrchestrator, StructuredPatrolDecisionModel
from backend.app.desktop.patrol.agent_loop.workers.runtime import LoopWorkerRuntime, StructuredCompletionVerifier, StructuredLaneAdvisor
from backend.app.desktop.patrol.agent_loop.curation.portfolio_publication import LoopPortfolioPublicationService
from backend.app.desktop.patrol.agent_loop.curation.publication_queue import LoopPortfolioPublicationQueue
from backend.app.desktop.patrol.agent_loop.resources.context_run_pool import ContextRunPool
from backend.app.desktop.patrol.agent_loop.workspaces.workspace_adoption import LoopWorkspaceAdoptionService
from backend.app.desktop.patrol.agent_loop.schemas import CompletionVerificationContract, CriterionVerification, LoopBudgetContract, LoopCreateRequest, LoopObservationEnvelope, PatrolAction, PatrolDecisionIntent
from backend.app.desktop.patrol.agent_loop.service import AgentLoopService
from backend.app.desktop.patrol.agent_loop.governance.interventions import LoopInterventionService

__all__ = [
    "AgentLoopService", "LoopInterventionService", "LoopAuthorityService", "AuthorityViolation", "CompletionEvidenceService", "CompletionGuard", "CompletionGuardResult",
    "CompletionVerificationContract", "CompletionVerifierPort", "CoordinatorClaim", "CriterionVerification",
    "DelegatedAuthorityGuard", "DelegatedDirectiveFactory", "KernelCommitResult", "KernelRejected",
    "DesktopDirectiveLaunchPort", "DirectiveLaunchPort", "LoopRunWorkspaceBinder", "LoopBudgetContract", "LoopCoordinator", "LoopCoordinatorRuntime", "LoopCreateRequest", "LoopKernel", "LoopObservationBuilder",
    "LoopObservationEnvelope", "MessageHistoryProjector", "PatrolAction", "PatrolDecisionIntent", "observation_hash",
    "PatrolDecisionModel", "PortfolioPatrol", "PatrolContractViolation", "LoopWaveDispatcher",
    "BoundaryReference", "CompletionCheckReference", "MissionReference", "OutcomeReference", "PatrolDecisionContract",
    "PendingDecisionContract", "PendingDecisionProjector",
    "BudgetDecision", "LoopBudgetGuard", "no_progress_fingerprint",
    "AgentLoopRecovery", "LoopRecoveryReport",
    "LoopObservationService", "LoopRoundOrchestrator", "StructuredPatrolDecisionModel",
    "LoopWorkerRuntime", "StructuredCompletionVerifier", "StructuredLaneAdvisor",
    "LoopPortfolioPublicationService", "LoopPortfolioPublicationQueue", "ContextRunPool",
    "LoopWorkspaceAdoptionService",
]
