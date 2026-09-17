r"""本文件对外提供自主压缩 policy、候选请求与 closed action 合同。

输入为用户授予的压缩范围、Patrol 选择的 pending decision 与已持久化 candidate identity；输出为
拒绝未知字段、不可变且可版本化的 Pydantic 值。具体工作流为用户创建 grant 时固化 policy，Patrol
可请求生成候选，但最终 action 只能引用候选，不能携带任意摘要或 ranges。示例：
`ApplyContextCompressionAction(action="apply_context_compression", candidate_id="c1", ...)`。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CompressionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AutonomousCompressionPolicy(CompressionContract):
    version: Literal[1] = 1
    allow_replace: bool = True
    allow_delete: bool = False
    protected_anchors: tuple[str, ...] = (
        "task_contract",
        "acceptance_criteria",
        "current_direct_user_message",
        "latest_direct_user_message",
        "unconsumed_material",
        "active_tool_protocol",
        "system_safety",
    )
    max_source_messages: int = Field(default=64, ge=2, le=256)
    max_source_tokens: int = Field(default=60_000, ge=256, le=500_000)
    max_attempts_per_gate: int = Field(default=3, ge=1, le=12)
    candidate_ttl_seconds: int = Field(default=900, ge=30, le=86_400)
    min_reduction_tokens: int = Field(default=256, ge=1, le=100_000)

    @model_validator(mode="after")
    def _replacement_required(self):
        if not self.allow_replace:
            raise ValueError("自主压缩必须允许可恢复 replacement")
        if self.allow_delete:
            raise ValueError("自主压缩不允许永久删除来源")
        return self


class CompressionCandidateRequest(CompressionContract):
    pending_decision_id: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    context_revision_id: str = Field(min_length=1)
    source_message_ids: tuple[str, ...] = Field(default=(), max_length=256)


class ApplyContextCompressionAction(CompressionContract):
    action: Literal["apply_context_compression"]
    pending_decision_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    context_revision_id: str = Field(min_length=1)
    checkpoint_id: str = Field(min_length=1)
