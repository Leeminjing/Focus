r"""本文件对外提供统一 Run 编排领域的公共入口。

输入为服务端准备的运行请求与持久执行身份；输出为唯一执行脊柱上的 Run 和终态事件。
具体工作流为 launcher 启动、lifecycle 收敛、outbox 持久通知，再由本入口显式导出。
示例：`from backend.app.desktop.run_orchestration import __all__`。
"""

from backend.app.desktop.run_orchestration.executor import (
    RunExecutionResources,
    execute_prepared_run,
)
from backend.app.desktop.run_orchestration.launcher import (
    PreparedRunLike,
    RunLauncher,
    RunRegistrar,
    RunRegistrationConflict,
    RunRegistrationRequest,
    RunStarter,
)
from backend.app.desktop.run_orchestration.models import RunOutboxDelivery, RunOutboxEvent
from backend.app.desktop.run_orchestration.models import RunDispatch
from backend.app.desktop.run_orchestration.admission import RunAdmissionConflict, RunAdmissionResult, RunAdmissionService
from backend.app.desktop.run_orchestration.assembler import RunExecutionAssembler, RunExecutionAssembly
from backend.app.desktop.run_orchestration.dispatch import DurableRunDispatchWorker, RunDispatchRecovery, RunDispatchRecoveryReport, RunDispatchRepository, StaleDispatchFence
from backend.app.desktop.run_orchestration.lifecycle import (
    RunLifecycleFinalizer,
    RunSettlement,
)
from backend.app.desktop.run_orchestration.outbox import (
    RunEventHandler,
    RunOutboxConsumer,
    RunOutboxRepository,
)

__all__ = [
    "RunExecutionResources",
    "execute_prepared_run",
    "PreparedRunLike",
    "RunLauncher",
    "RunLifecycleFinalizer",
    "RunSettlement",
    "RunEventHandler",
    "RunOutboxConsumer",
    "RunOutboxDelivery",
    "RunOutboxEvent",
    "RunOutboxRepository",
    "RunDispatch",
    "RunAdmissionConflict",
    "RunAdmissionResult",
    "RunAdmissionService",
    "RunExecutionAssembler",
    "RunExecutionAssembly",
    "DurableRunDispatchWorker",
    "RunDispatchRecovery",
    "RunDispatchRecoveryReport",
    "RunDispatchRepository",
    "StaleDispatchFence",
    "RunRegistrar",
    "RunRegistrationConflict",
    "RunRegistrationRequest",
    "RunStarter",
]
