r"""本文件对外提供 workspace slot、lease、fingerprint、执行锚点与 adoption 严格合同。

输入为可信 workspace identity、Run 装备、工具 effect、路径与版本；输出为服务端推导的访问意图、
不可变执行锚点和采用结果。具体工作流为拒绝额外字段，统一规范读写模式与版本，再交给协调服务。
示例：`intent = WorkspaceIntentDeriver.derive(equipment, effects)`。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceAccessMode(StrEnum):
    READ = "read"
    WRITE = "write"


class WorkspaceSlotKind(StrEnum):
    AUTHORITATIVE = "authoritative"
    ISOLATED = "isolated"


class WorkspaceExecutionIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: WorkspaceAccessMode
    reasons: tuple[str, ...]


class WorkspaceLeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    mode: WorkspaceAccessMode
    ttl_seconds: int = Field(default=120, ge=10, le=3600)


class WorkspaceLeaseGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lease_id: str
    slot_id: str
    run_id: str
    mode: WorkspaceAccessMode
    fencing_token: int = Field(gt=0)
    expires_at: str


class WorkspaceFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_count: int = Field(ge=0)
    byte_count: int = Field(ge=0)
    vcs_revision: str | None = None
    dirty: bool = False


class WorkspaceAnchorContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_id: str
    workspace_revision: int = Field(gt=0)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    fencing_token: int | None = Field(default=None, gt=0)


class WorkspaceAdoptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    adoption_id: str
    source_slot_id: str
    target_slot_id: str
    source_revision: int = Field(gt=0)
    expected_target_revision: int = Field(gt=0)
    evidence: dict[str, Any] = Field(default_factory=dict)


class WorkspaceIntentDeriver:
    @classmethod
    def derive(
        cls,
        equipment: dict[str, Any],
        tool_effects: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    ) -> WorkspaceExecutionIntent:
        reasons: list[str] = []
        permissions = {str(item) for item in equipment.get("permissions", [])}
        if permissions.intersection({"write", "host_command"}):
            reasons.append("execution_profile_allows_write")
        for effect in tool_effects:
            if cls._effect_may_write(effect):
                reasons.append(f"tool_effect:{effect.get('tool_name', 'unknown')}")
        mode = WorkspaceAccessMode.WRITE if reasons else WorkspaceAccessMode.READ
        return WorkspaceExecutionIntent(
            mode=mode,
            reasons=tuple(dict.fromkeys(reasons)) or ("read_only_capabilities",),
        )

    @staticmethod
    def _effect_may_write(effect: dict[str, Any]) -> bool:
        return bool(
            effect.get("writes_workspace")
            or effect.get("local_effect") in {"write", "process", "unknown"}
            or effect.get("effect") in {"write", "host_command", "delegated_execution"}
        )
