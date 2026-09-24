r"""本文件对外提供 StructuredContextSynthesisService。

输入为冻结 WorkContextSpec、ResolvedEvidenceBundle、Loop observation 与只提供 schema-constrained 调用的模型；输出为经过独立
claim direct-support 检查的 ValidatedContextDossier 或显式 blocker。具体工作流为 dossier_synthesizer 先生成带本地 claim keys 的
claim graph，服务端物化稳定 claim identities，再由 claim_verifier 只判断 confirmed claims 是否被其冻结 citations 直接支持，最后
交给 deterministic ContextSynthesisValidator；任何失败均不回退为逐 evidence 复制。示例：
`result = await service.synthesize(observation, work_spec, bundle)`。
"""

from __future__ import annotations

from typing import Any

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    ResolvedEvidenceBundle,
    WorkContextSpec,
)
from backend.app.desktop.agent_loop.context_expansion.synthesis import (
    ClaimSupportAssessment,
    ClaimSupportProposal,
    ContextSynthesisClaim,
    ContextSynthesisDraft,
    ContextSynthesisResult,
    ContextSynthesisValidator,
    ContextSynthesisWorkerDraft,
    SynthesisSection,
)
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.context_curation import NamespacedMessageRef, evidence_ref_key


class StructuredContextSynthesisService:
    VERSION = "structured-context-synthesizer-v1"

    def __init__(self, synthesis_model, claim_verifier_model=None) -> None:
        self._synthesis_model = synthesis_model
        self._claim_verifier_model = claim_verifier_model or synthesis_model
        self._validator = ContextSynthesisValidator()

    async def synthesize(
        self,
        observation: LoopObservationEnvelope,
        work_spec: WorkContextSpec,
        bundle: ResolvedEvidenceBundle,
    ) -> ContextSynthesisResult:
        attempts: list[dict[str, Any]] = []
        try:
            worker_draft = await self._synthesis_model.invoke(
                ContextSynthesisWorkerDraft,
                self._synthesis_authority(),
                {
                    "work_spec": work_spec.model_dump(mode="json"),
                    "question_identities": {
                        self._validator.question_id(question): question
                        for question in work_spec.questions
                    },
                    "resolved_evidence": bundle.model_dump(mode="json"),
                },
            )
            attempts.extend(tuple(getattr(self._synthesis_model, "last_attempt_records", ())))
            draft = worker_draft.materialize()
            confirmed = tuple(item for item in draft.claims if item.authority == "confirmed")
            proposal = await self._claim_verifier_model.invoke(
                ClaimSupportProposal,
                self._verification_authority(),
                {
                    "claims": tuple(
                        {
                            "claim_id": claim.claim_id,
                            "statement": claim.statement,
                            "citations": tuple(
                                {
                                    "identity": evidence_ref_key(ref),
                                    "content": self._content(bundle, ref),
                                }
                                for ref in claim.citations
                            ),
                        }
                        for claim in confirmed
                    )
                },
            )
            attempts.extend(tuple(getattr(self._claim_verifier_model, "last_attempt_records", ())))
            by_id = {item.claim_id: item for item in proposal.assessments}
            assessments = tuple(
                ClaimSupportAssessment(
                    claim_id=claim.claim_id,
                    verdict=by_id[claim.claim_id].verdict,
                    citation_keys=tuple(evidence_ref_key(ref) for ref in claim.citations),
                    reason=by_id[claim.claim_id].reason,
                )
                for claim in confirmed
            )
            dossier = self._validator.validate(
                work_spec,
                bundle,
                draft,
                assessments,
                synthesizer_version=self.VERSION,
            )
        except (KeyError, TypeError, ValueError) as exc:
            recorded = list(attempts)
            for item in (
                *tuple(getattr(self._synthesis_model, "last_attempt_records", ())),
                *tuple(getattr(self._claim_verifier_model, "last_attempt_records", ())),
            ):
                if item not in recorded:
                    recorded.append(item)
            return ContextSynthesisResult(
                blocker_code="synthesis_invalid",
                blocker_summary=f"claim-level synthesis failed: {str(exc)[:1600]}",
                attempt_records=tuple(recorded),
            )
        return ContextSynthesisResult(dossier=dossier, attempt_records=tuple(attempts))

    @staticmethod
    def _content(bundle: ResolvedEvidenceBundle, ref) -> Any:
        if isinstance(ref, NamespacedMessageRef):
            return bundle.evidence.message(ref).content
        return bundle.evidence.structured_item(ref).content

    @staticmethod
    def _synthesis_authority() -> str:
        return "你是无权 dossier_synthesizer。只基于输入冻结 WorkSpec 与 ResolvedEvidenceBundle 生成原子 claim graph；confirmed 只能表达 citation 直接支持的事实，跨来源关系必须标为 inference 并引用更早 premise，未证实原因必须 hypothesis。每个 required requirement/question 必须映射或显式列为 unresolved。不得读取外部历史、改写 WorkSpec、执行工作或创建 Context。"

    @staticmethod
    def _verification_authority() -> str:
        return "你是无权 claim_verifier。逐项判断 confirmed atomic claim 是否被给定 citations 直接蕴含，只返回 supported、unsupported 或 unknown 与简短理由；不得补充证据、改写 claim、进行常识推断或输出执行指令。"


class DeterministicTestContextSynthesisService:
    VERSION = "deterministic-test-context-synthesizer-v1"

    def __init__(self) -> None:
        self._validator = ContextSynthesisValidator()

    async def synthesize(
        self,
        observation: LoopObservationEnvelope,
        work_spec: WorkContextSpec,
        bundle: ResolvedEvidenceBundle,
    ) -> ContextSynthesisResult:
        question_ids = tuple(self._validator.question_id(item) for item in work_spec.questions)
        claims = tuple(
            ContextSynthesisClaim.create(
                statement=self._statement(bundle, item.ref),
                authority="confirmed",
                citations=(item.ref,),
                requirement_ids=(item.requirement_id,),
                question_ids=question_ids,
            )
            for item in bundle.items
        )
        draft = ContextSynthesisDraft(
            sections=(SynthesisSection(title="Resolved Evidence", claim_ids=tuple(item.claim_id for item in claims)),),
            claims=claims,
        )
        assessments = tuple(
            ClaimSupportAssessment(
                claim_id=claim.claim_id,
                verdict="supported",
                citation_keys=tuple(evidence_ref_key(ref) for ref in claim.citations),
                reason="test adapter preserves the cited evidence content verbatim",
            )
            for claim in claims
        )
        try:
            dossier = self._validator.validate(
                work_spec,
                bundle,
                draft,
                assessments,
                synthesizer_version=self.VERSION,
            )
        except ValueError as exc:
            return ContextSynthesisResult(
                blocker_code="synthesis_invalid",
                blocker_summary=str(exc)[:1600],
            )
        return ContextSynthesisResult(dossier=dossier)

    @staticmethod
    def _statement(bundle: ResolvedEvidenceBundle, ref) -> str:
        content = StructuredContextSynthesisService._content(bundle, ref)
        text = content if isinstance(content, str) else str(content)
        return " ".join(text.split())[:2000] or "Evidence content is empty"
