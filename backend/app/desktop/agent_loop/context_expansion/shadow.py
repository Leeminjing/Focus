r"""本文件对外提供 ShadowOpportunityOutcome 与 SemanticDerivationShadowReport。

输入为冻结 observation 的 preview/index coverage、canonical opportunity 数量以及 observe-only compilation 结果；输出为稳定 identity 的
comparison artifact。具体工作流为逐 opportunity 记录 resolved evidence 数量、三维 quality verdict 或 blocker，再比较 preview 与完整
Revision 可达消息数；该合同不创建 Context、不修改 Portfolio，也不包含执行授权。示例：`report = SemanticDerivationShadowReport.create(...)`。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    stable_expansion_hash,
)


class _ShadowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ShadowOpportunityOutcome(_ShadowModel):
    opportunity_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolved_evidence_count: int = Field(ge=0)
    quality_verdicts: tuple[str, ...] = ()
    blocker_code: str | None = None


class SemanticDerivationShadowReport(_ShadowModel):
    report_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    frontier_hash: str = Field(min_length=1)
    preview_message_count: int = Field(ge=0)
    indexed_message_count: int = Field(ge=0)
    canonical_candidate_count: int = Field(ge=0)
    outcomes: tuple[ShadowOpportunityOutcome, ...]

    @classmethod
    def create(
        cls,
        *,
        frontier_hash: str,
        preview_message_count: int,
        indexed_message_count: int,
        canonical_candidate_count: int,
        outcomes: tuple[ShadowOpportunityOutcome, ...],
    ) -> SemanticDerivationShadowReport:
        ordered = tuple(sorted(outcomes, key=lambda item: item.opportunity_id))
        payload = (
            frontier_hash,
            preview_message_count,
            indexed_message_count,
            canonical_candidate_count,
            tuple(item.model_dump(mode="json") for item in ordered),
        )
        return cls(
            report_id=stable_expansion_hash("semantic-derivation-shadow-report", *payload),
            frontier_hash=frontier_hash,
            preview_message_count=preview_message_count,
            indexed_message_count=indexed_message_count,
            canonical_candidate_count=canonical_candidate_count,
            outcomes=ordered,
        )
