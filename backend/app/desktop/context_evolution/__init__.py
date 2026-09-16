r"""本文件对外提供 Context Evolution 的合同、权威应用服务、持久化、读取、发布与图查询入口。

输入为 Context identity、不可变 revision 与版本化来源；输出为唯一执行历史引用及 revision/source
合同、类型化仓储错误和只读视图。具体工作流为由 schemas 收束公开寻址类型，repository 执行
不可变插入与 current pointer CAS，reader 按精确 checkpoint 生成各消费面，publisher 只提交完成
shadow 准备的合法候选，retention planner 保护被引用历史，query service 保留完整图并投影兼容树，
ContextEvolutionService 收束唯一 revision-backed 应用读写路径。
示例：`from ...context_evolution import ContextEvolutionService`。
"""

from backend.app.desktop.context_evolution.checkpoint_writer import (
    ContextCheckpointWriter,
    LangGraphContextCheckpointWriter,
)
from backend.app.desktop.context_evolution.graph import ContextEvolutionQueryService
from backend.app.desktop.context_evolution.lifecycle import (
    ContextCleanupPlan,
    ContextRevisionRetentionPlanner,
)
from backend.app.desktop.context_evolution.publisher import (
    ContextRevisionPreparationFailed,
    ContextRevisionPublicationBlocked,
    ContextRevisionPublisher,
)
from backend.app.desktop.context_evolution.repository import (
    ContextIdentityNotFound,
    ContextRevisionAlreadyExists,
    ContextRevisionCycle,
    ContextRevisionIdentityMismatch,
    ContextRevisionNotFound,
    ContextRevisionRepository,
    ContextRevisionRepositoryError,
    StaleContextRevision,
)
from backend.app.desktop.context_evolution.reader import (
    ContextRevisionReadResult,
    ContextRevisionReader,
    ContextRevisionViewKind,
)
from backend.app.desktop.context_evolution.schemas import (
    ContextEvolutionEdge,
    ContextEvolutionGraph,
    ContextEvolutionNode,
    ContextFirstParentNode,
    ContextFirstParentTree,
    ContextFrontierSummary,
    ContextRevisionCheckpointView,
    ContextRevisionContract,
    ContextRevisionHistoricalView,
    ContextRevisionMessageView,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionPrepareRequest,
    ContextRevisionPublicationResult,
    ContextRevisionRef,
    ContextRevisionSourceContract,
    DeletedContextSource,
    DeletedContextSourceView,
    PreparedContextRevision,
)
from backend.app.desktop.context_evolution.service import ContextEvolutionService

__all__ = [
    "ContextCheckpointWriter",
    "ContextCleanupPlan",
    "ContextEvolutionEdge",
    "ContextEvolutionGraph",
    "ContextEvolutionNode",
    "ContextEvolutionQueryService",
    "ContextEvolutionService",
    "ContextFirstParentNode",
    "ContextFirstParentTree",
    "ContextFrontierSummary",
    "ContextRevisionCheckpointView",
    "ContextRevisionContract",
    "ContextIdentityNotFound",
    "ContextRevisionAlreadyExists",
    "ContextRevisionCycle",
    "ContextRevisionHistoricalView",
    "ContextRevisionIdentityMismatch",
    "ContextRevisionMessageView",
    "ContextRevisionNotFound",
    "ContextRevisionOriginKind",
    "ContextRevisionPayloadMode",
    "ContextRevisionProjectionStatus",
    "ContextRevisionPrepareRequest",
    "ContextRevisionPreparationFailed",
    "ContextRevisionPublicationBlocked",
    "ContextRevisionPublicationResult",
    "ContextRevisionPublisher",
    "ContextRevisionReadResult",
    "ContextRevisionReader",
    "ContextRevisionRef",
    "ContextRevisionRepository",
    "ContextRevisionRepositoryError",
    "ContextRevisionRetentionPlanner",
    "ContextRevisionSourceContract",
    "ContextRevisionViewKind",
    "DeletedContextSource",
    "DeletedContextSourceView",
    "LangGraphContextCheckpointWriter",
    "PreparedContextRevision",
    "StaleContextRevision",
]
