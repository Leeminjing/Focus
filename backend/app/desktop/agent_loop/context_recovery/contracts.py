r"""本文件对外提供 ContextRecoveryOpportunityContract 与 ContextRecoveryResolution 不可变恢复合同。

输入为来源 revision/frontier、goal、workspace、authority、grant、Run 因果、编译器版本、到期时间和可信 Lane plan；输出为
可持久化、可哈希且只公开 identity/摘要的恢复机会。具体工作流为按全部 authority 输入计算稳定 opportunity identity，读取时严格
校验完整 payload，决策解析只返回 intent 或明确等待原因。示例：`opportunity = ContextRecoveryOpportunityContract.create(...)`。
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.desktop.context_curation.contracts import CreateLanePlan
from backend.app.desktop.context_evolution import ContextRevisionRef

CONTEXT_RECOVERY_COMPILER_VERSION = "single-source-recovery-v2"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContextRecoveryOpportunityContract(_FrozenModel):
    opportunity_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    loop_id: str
    source: ContextRevisionRef
    source_frontier_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    goal_revision: int = Field(gt=0)
    workspace_revision: int = Field(gt=0)
    authority_revision: int = Field(gt=0)
    grant_id: str
    grant_revision: int = Field(gt=0)
    source_run_id: str
    compiler_version: str
    expires_at: datetime
    status: Literal["pending", "consumed", "stale", "blocked"] = "pending"
    safe_summary: str = Field(min_length=1, max_length=2000)
    plan: CreateLanePlan

    @classmethod
    def create(
        cls,
        *,
        loop_id: str,
        source: ContextRevisionRef,
        source_frontier_hash: str,
        goal_revision: int,
        workspace_revision: int,
        authority_revision: int,
        grant_id: str,
        grant_revision: int,
        source_run_id: str,
        expires_at: datetime,
        safe_summary: str,
        plan: CreateLanePlan,
    ) -> "ContextRecoveryOpportunityContract":
        identity_payload = {
            "loop_id": loop_id,
            "source": source.model_dump(mode="json"),
            "source_frontier_hash": source_frontier_hash,
            "goal_revision": goal_revision,
            "workspace_revision": workspace_revision,
            "authority_revision": authority_revision,
            "grant_id": grant_id,
            "grant_revision": grant_revision,
            "source_run_id": source_run_id,
            "compiler_version": CONTEXT_RECOVERY_COMPILER_VERSION,
            "plan": plan.model_dump(mode="json"),
        }
        encoded = json.dumps(identity_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return cls(
            opportunity_id=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            expires_at=expires_at,
            safe_summary=safe_summary,
            status="pending",
            **identity_payload,
        )

    def public_payload(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "source_context_id": self.source.context_id,
            "source_revision_id": self.source.revision_id,
            "source_run_id": self.source_run_id,
            "compiler_version": self.compiler_version,
            "expires_at": self.expires_at.isoformat(),
            "safe_summary": self.safe_summary,
        }


class ContextRecoveryResolution(_FrozenModel):
    intent: Any | None = None
    waiting_reason: str | None = None
