r"""本文件对外提供 StructuredContextQualityService 与 QualityWorkerProposal。

输入为冻结 WorkContextSpec、ResolvedEvidenceBundle、ValidatedContextDossier 与只提供 schema-constrained 调用的独立模型；输出为
ContextQualityResult。具体工作流为先运行 deterministic preflight，结构不合格时不调用模型；合格时 quality worker 只能对
minimality、sufficiency、coherence 返回 verdict、理由和已有 identities，随后 fail-closed policy 要求三维全部 pass。本模块不改写
work、dossier 或 evidence。示例：`result = await service.verify(work_spec, bundle, dossier)`。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    ResolvedEvidenceBundle,
    WorkContextSpec,
)
from backend.app.desktop.agent_loop.context_expansion.quality import (
    ContextQualityResult,
    ContextQualityVerifier,
    QualityDimensionVerdict,
)
from backend.app.desktop.agent_loop.context_expansion.synthesis import (
    ValidatedContextDossier,
)


class QualityWorkerProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dimensions: tuple[QualityDimensionVerdict, ...] = Field(min_length=3, max_length=3)


class StructuredContextQualityService:
    VERSION = "structured-context-quality-service-v1"

    def __init__(self, model) -> None:
        self._model = model
        self._verifier = ContextQualityVerifier()

    async def verify(
        self,
        work_spec: WorkContextSpec,
        bundle: ResolvedEvidenceBundle,
        dossier: ValidatedContextDossier,
    ) -> ContextQualityResult:
        preflight = self._verifier.preflight(work_spec, bundle, dossier)
        if not preflight.eligible:
            return self._verifier.verify(work_spec, bundle, dossier, None)
        try:
            proposal = await self._model.invoke(
                QualityWorkerProposal,
                self._authority(),
                {
                    "work_spec": work_spec.model_dump(mode="json"),
                    "resolved_evidence": bundle.model_dump(mode="json"),
                    "dossier": dossier.model_dump(mode="json"),
                    "preflight": preflight.model_dump(mode="json"),
                },
            )
        except (TypeError, ValueError) as exc:
            return ContextQualityResult(
                preflight=preflight,
                blocker_code="quality_contract_invalid",
                blocker_summary=f"context quality worker failed: {str(exc)[:1600]}",
                attempt_records=tuple(getattr(self._model, "last_attempt_records", ())),
            )
        result = self._verifier.verify(
            work_spec,
            bundle,
            dossier,
            proposal.model_dump(mode="json"),
        )
        return result.model_copy(
            update={"attempt_records": tuple(getattr(self._model, "last_attempt_records", ()))},
        )

    @staticmethod
    def _authority() -> str:
        return "你是无权 context_quality_verifier。只评价输入冻结 Context package 的 minimality、sufficiency、coherence，各维度返回 pass/fail/unknown、grounded reasons 与输入中已有 identities。不得重写 objective、claims、evidence、completion criteria，不得新增执行指令或修改状态。形式 coverage 不等于充分；来源冲突必须显式保留；协议闭包 evidence 不得当作冗余。"


class DeterministicTestContextQualityService:
    VERSION = "deterministic-test-context-quality-service-v1"

    async def verify(
        self,
        work_spec: WorkContextSpec,
        bundle: ResolvedEvidenceBundle,
        dossier: ValidatedContextDossier,
    ) -> ContextQualityResult:
        verifier = ContextQualityVerifier()
        dimensions = tuple(
            QualityDimensionVerdict(
                dimension=dimension,
                verdict="pass",
                reasons=("test adapter accepts deterministic fixture",),
            )
            for dimension in ("minimality", "sufficiency", "coherence")
        )
        return verifier.verify(
            work_spec,
            bundle,
            dossier,
            {"dimensions": tuple(item.model_dump(mode="json") for item in dimensions)},
        )
