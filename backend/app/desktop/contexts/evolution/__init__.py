"""本文件对外提供当前 Context 状态、原子发布、精确输入快照与直接来源的统一入口。

输入为 Context 身份、执行 checkpoint、内容与 owner；输出为严格引用、仓储和读取发布服务。
具体工作流为每个 Context 维护唯一 current，活动任务固定独立输入，终态释放快照；Revision 命名仅兼容精确版本 token。
示例：await ContextRevisionRepository().freeze(session, ref, owner_kind="run", owner_id=run_id)。
"""

from backend.app.desktop.contexts.evolution.checkpoint_writer import (
    ContextCheckpointWriter,
    LangGraphContextCheckpointWriter,
)
from backend.app.desktop.contexts.evolution.graph import ContextEvolutionQueryService
from backend.app.desktop.contexts.evolution.lineage import (
    ContextLineageEdge,
    ContextLineageResolver,
    ContextLineageSource,
)
from backend.app.desktop.contexts.evolution.publisher import (
    ContextRevisionPreparationFailed,
    ContextRevisionPublicationBlocked,
    ContextRevisionPublisher,
)
from backend.app.desktop.contexts.evolution.reader import (
    ContextRevisionReader,
    ContextRevisionReadResult,
    ContextRevisionViewKind,
)
from backend.app.desktop.contexts.evolution.repository import (
    ContextIdentityNotFound,
    ContextRevisionAlreadyExists,
    ContextRevisionCycle,
    ContextRevisionIdentityMismatch,
    ContextRevisionNotFound,
    ContextRevisionRepository,
    ContextRevisionRepositoryError,
    StaleContextRevision,
)
from backend.app.desktop.contexts.evolution.schemas import (
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
    ContextRevisionPrepareRequest,
    ContextRevisionProjectionStatus,
    ContextRevisionPublicationResult,
    ContextRevisionRef,
    ContextRevisionSourceContract,
    DeletedContextSource,
    DeletedContextSourceView,
    PreparedContextRevision,
)
from backend.app.desktop.contexts.evolution.service import (
    ContextCheckpointBusy,
    ContextEvolutionService,
)

__all__ = [
    "ContextCheckpointBusy",
    "ContextCheckpointWriter",
    "ContextEvolutionEdge",
    "ContextEvolutionGraph",
    "ContextEvolutionNode",
    "ContextEvolutionQueryService",
    "ContextEvolutionService",
    "ContextFirstParentNode",
    "ContextFirstParentTree",
    "ContextFrontierSummary",
    "ContextIdentityNotFound",
    "ContextLineageEdge",
    "ContextLineageResolver",
    "ContextLineageSource",
    "ContextRevisionAlreadyExists",
    "ContextRevisionCheckpointView",
    "ContextRevisionContract",
    "ContextRevisionCycle",
    "ContextRevisionHistoricalView",
    "ContextRevisionIdentityMismatch",
    "ContextRevisionMessageView",
    "ContextRevisionNotFound",
    "ContextRevisionOriginKind",
    "ContextRevisionPayloadMode",
    "ContextRevisionPreparationFailed",
    "ContextRevisionPrepareRequest",
    "ContextRevisionProjectionStatus",
    "ContextRevisionPublicationBlocked",
    "ContextRevisionPublicationResult",
    "ContextRevisionPublisher",
    "ContextRevisionReadResult",
    "ContextRevisionReader",
    "ContextRevisionRef",
    "ContextRevisionRepository",
    "ContextRevisionRepositoryError",
    "ContextRevisionSourceContract",
    "ContextRevisionViewKind",
    "DeletedContextSource",
    "DeletedContextSourceView",
    "LangGraphContextCheckpointWriter",
    "PreparedContextRevision",
    "StaleContextRevision",
]
