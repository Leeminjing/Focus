r"""本文件对外提供 StructuredContextQualityService 与 QualityWorkerProposal。

输入为冻结 WorkContextSpec、ResolvedEvidenceBundle、ValidatedContextDossier 与只提供 schema-constrained 调用的独立模型；输出为
ContextQualityResult。具体工作流为先运行与Resolver共享完整工具闭包的deterministic preflight，结构不合格时不调用模型；合格时附只读阶段与精确协议用途投影，评价创建前输入能否支持WorkSpec执行，不要求未来输出已存在。quality worker 只能对
minimality、sufficiency、coherence 返回 verdict、理由和已有 identities。冻结引用与维度合同进入模型已有的有界结果校验，成功前拒绝非法输出；合法fail/unknown不重试或改写。
随后 fail-closed policy 要求三维全部 pass；无校验入口的适配器只调用一次并复用相同校验。本模块不改写
work、dossier 或 evidence。示例：`result = await service.verify(work_spec, bundle, dossier)`。
"""

from __future__ import annotations

from functools import partial

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
from backend.app.desktop.agent_loop.context_expansion.quality_input_scope import quality_input_scope
from backend.app.desktop.agent_loop.context_expansion.synthesis import (
    ValidatedContextDossier,
)
from backend.app.desktop.agent_loop.derivation_worker import (
    StructuredResultValidationError,
    safe_validation_message,
)


class QualityWorkerProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dimensions: tuple[QualityDimensionVerdict, ...] = Field(min_length=3, max_length=3)


class StructuredContextQualityService:
    VERSION = "structured-context-quality-service-v4"

    def __init__(self, model) -> None:
        self._model = model
        self._verifier = ContextQualityVerifier()

    def bind_usage_receipts(self, receipts):
        if hasattr(self._model, "bind_usage_receipts"):
            self._model.bind_usage_receipts(receipts)

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
            proposal = await self._invoke_validated(
                QualityWorkerProposal,
                self._authority(),
                {
                    "work_spec": work_spec.model_dump(mode="json"),
                    "resolved_evidence": bundle.model_dump(mode="json"),
                    "dossier": dossier.model_dump(mode="json"),
                    "preflight": preflight.model_dump(mode="json"),
                    "evaluation_scope": quality_input_scope(work_spec, bundle, dossier),
                },
                partial(self._validate_proposal, work_spec=work_spec, bundle=bundle, dossier=dossier),
            )
        except (TypeError, ValueError) as exc:
            return ContextQualityResult(
                preflight=preflight,
                blocker_code="quality_contract_invalid",
                blocker_summary=f"context quality worker failed: {safe_validation_message(exc)[:1600]}",
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

    async def _invoke_validated(self, schema, system, payload, validator):
        method = getattr(self._model, "invoke_validated", None)
        if method is not None:
            return await method(schema, system, payload, validator)
        result = await self._model.invoke(schema, system, payload)
        validator(result)
        return result

    def _validate_proposal(self, proposal, *, work_spec, bundle, dossier):
        result = self._verifier.verify(work_spec, bundle, dossier, proposal.model_dump(mode="json"))
        if result.blocker_code == "quality_contract_invalid":
            raise StructuredResultValidationError(
                "quality_contract_invalid",
                result.blocker_summary,
                unit_identity=dossier.dossier_id,
                violated_rule="quality_frozen_identity_and_dimension_contract",
            )

    @staticmethod
    def _authority() -> str:
        return (
            "你是无权 context_quality_verifier。只评价输入冻结 Context package 的 minimality、sufficiency、coherence，"
            "各维度返回 pass/fail/unknown、grounded reasons 与输入中已有 identities。"
            "evaluation_scope 是平台从冻结输入派生的只读阶段和用途事实：当前处于编译、发布、执行之前，"
            "评价输入能否支持 WorkSpec 执行，而非完成判据是否已经兑现。目标 Context 尚未创建，"
            "自身身份在正式发布时产生，结论和送达事实在执行后核验；不得因这些未来输出尚不存在而判输入不足，"
            "也不得声称其已经存在或完成。existing_source_identities 只代表已有来源，"
            "其历史来源缺失、冲突、证据不足仍须按事实判定。protocol_only_evidence_keys 是共享 Tool Exchange "
            "原子闭包强制保留的 caller 或兄弟结果，包括完整正文；不能仅因其主题不直接服务 WorkSpec "
            "而当作冗余。其他选择或claim的冗余、来源冲突与输入不足仍须独立评价。"
            "不得重写 objective、claims、evidence、completion criteria，不得新增执行指令或修改状态。"
            "形式 coverage 不等于充分；来源冲突必须显式保留。"
        )


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
