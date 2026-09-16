r"""本文件对外提供 workspace coordination 领域的稳定公共入口。

输入为服务端执行身份、workspace 路径、slot revision 和 adoption 请求；输出为访问意图、lease、
fingerprint、隔离 provider、采用与生命周期服务。具体工作流为只从本入口组合模块，保持工具层不接触
ORM 内部。示例：`from backend.app.desktop.workspace_coordination import WorkspaceLeaseManager`。
"""

from backend.app.desktop.workspace_coordination.adoption import GitWorkspaceResultApplier, WorkspaceAdopter, WorkspaceAdoptionConflict, WorkspaceResultApplier
from backend.app.desktop.workspace_coordination.fingerprints import WorkspaceFingerprinter, WorkspaceRevisionConflict
from backend.app.desktop.workspace_coordination.git_worktree import GitWorktreeIsolationProvider
from backend.app.desktop.workspace_coordination.isolation import IsolationRequest, IsolationResult, WorkspaceIsolationProvider
from backend.app.desktop.workspace_coordination.leases import WorkspaceLeaseConflict, WorkspaceLeaseManager
from backend.app.desktop.workspace_coordination.lifecycle import WorkspaceRunReconciler, WorkspaceSlotLifecycle
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor, WorkspaceAdoption, WorkspaceLease, WorkspaceSlot, WorkspaceSlotTombstone
from backend.app.desktop.workspace_coordination.schemas import WorkspaceAccessMode, WorkspaceAdoptionRequest, WorkspaceAnchorContract, WorkspaceExecutionIntent, WorkspaceFingerprint, WorkspaceIntentDeriver, WorkspaceLeaseGrant, WorkspaceLeaseRequest, WorkspaceSlotKind

__all__ = [
    "GitWorkspaceResultApplier", "GitWorktreeIsolationProvider", "IsolationRequest", "IsolationResult", "RunExecutionAnchor",
    "WorkspaceAccessMode", "WorkspaceAdopter", "WorkspaceAdoption", "WorkspaceAdoptionConflict",
    "WorkspaceAdoptionRequest", "WorkspaceAnchorContract", "WorkspaceExecutionIntent", "WorkspaceFingerprint",
    "WorkspaceFingerprinter", "WorkspaceIntentDeriver", "WorkspaceIsolationProvider", "WorkspaceLease",
    "WorkspaceLeaseConflict", "WorkspaceLeaseGrant", "WorkspaceLeaseManager", "WorkspaceLeaseRequest",
    "WorkspaceResultApplier", "WorkspaceRevisionConflict", "WorkspaceRunReconciler", "WorkspaceSlot",
    "WorkspaceSlotKind", "WorkspaceSlotLifecycle", "WorkspaceSlotTombstone",
]
