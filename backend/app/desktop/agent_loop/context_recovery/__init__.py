r"""本文件对外提供单来源 Context 恢复域合同、编译器、authority validator、持久化服务与决策解析阶段的统一重导出入口。

输入为冻结 Loop observation、权威 Context revision 与 identity-only Patrol recovery action；输出为持久恢复机会、可信
CreateLanePlan、稳定 freshness 错误或显式等待原因。具体工作流为 service 发现可证明的中断，compiler 生成证据可追溯的最小计划，
stage 仅按 opportunity identity 解析模型选择，validator、Kernel 与 Portfolio publication 再完成权限校验和原子消费。示例：
`resolution = await ContextRecoveryStage(sessions).resolve(observation, intent)`。
"""

from backend.app.desktop.agent_loop.context_recovery.compiler import ContextRecoveryPlanCompiler
from backend.app.desktop.agent_loop.context_recovery.contracts import (
    CONTEXT_RECOVERY_COMPILER_VERSION,
    ContextRecoveryOpportunityContract,
    ContextRecoveryResolution,
)
from backend.app.desktop.agent_loop.context_recovery.repository import ContextRecoveryOpportunityRepository
from backend.app.desktop.agent_loop.context_recovery.service import ContextRecoveryOpportunityService
from backend.app.desktop.agent_loop.context_recovery.stage import ContextRecoveryStage
from backend.app.desktop.agent_loop.context_recovery.validator import (
    ContextRecoveryAuthorityError,
    ContextRecoveryAuthorityValidator,
)

__all__ = [
    "CONTEXT_RECOVERY_COMPILER_VERSION",
    "ContextRecoveryOpportunityContract",
    "ContextRecoveryOpportunityRepository",
    "ContextRecoveryOpportunityService",
    "ContextRecoveryPlanCompiler",
    "ContextRecoveryResolution",
    "ContextRecoveryStage",
    "ContextRecoveryAuthorityError",
    "ContextRecoveryAuthorityValidator",
]
