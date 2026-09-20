r"""本文件对外提供 FactEvidenceReference、FactActor、FactVerificationDecision 与 FactVerificationPolicy。

输入为类型化证据引用、观察者和期望验证状态；输出为允许的验证决定或显式拒绝。具体工作流为区分 observer、evidence
与 authority，仅允许已提交 Run、Tool、Workspace、Artifact、Context revision、Kernel 或用户确认充当验证证据，模型文本
只能形成 observed 事实。示例：`decision = policy.evaluate(evidence, observer)`。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


EvidenceKind = Literal["run", "tool", "workspace", "artifact", "context_revision", "kernel", "user", "model_statement"]


class _FactContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FactEvidenceReference(_FactContract):
    kind: EvidenceKind
    entity_id: str = Field(min_length=1, max_length=160)
    run_id: str | None = Field(default=None, max_length=32)
    context_revision_id: str | None = Field(default=None, max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FactActor(_FactContract):
    kind: Literal["system", "tool", "context", "patrol", "user", "model"]
    actor_id: str = Field(min_length=1, max_length=160)


class FactVerificationDecision(_FactContract):
    target_state: Literal["observed", "verifying", "verified"]
    verifier: FactActor | None = None
    reason: str


class FactVerificationPolicy:
    _AUTHORITATIVE = frozenset({"run", "tool", "workspace", "artifact", "context_revision", "kernel", "user"})

    def evaluate(self, evidence: tuple[FactEvidenceReference, ...], observer: FactActor) -> FactVerificationDecision:
        accepted = tuple(item for item in evidence if item.kind in self._AUTHORITATIVE)
        if not accepted:
            return FactVerificationDecision(target_state="observed", reason="仅有观察或模型陈述，尚无权威证据")
        verifier_kind = "tool" if any(item.kind == "tool" for item in accepted) else "system"
        verifier_id = next(item.entity_id for item in accepted if item.kind == "tool") if verifier_kind == "tool" else "fact-verification-policy"
        return FactVerificationDecision(
            target_state="verified",
            verifier=FactActor(kind=verifier_kind, actor_id=verifier_id),
            reason="类型化权威证据已提交",
        )
