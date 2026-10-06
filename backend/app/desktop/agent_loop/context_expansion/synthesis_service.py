r"""本文件对外提供 StructuredContextSynthesisService。

输入为冻结 WorkContextSpec、ResolvedEvidenceBundle、Loop observation 与只提供 schema-constrained 调用的模型；输出为经过独立
claim direct-support 检查的 ValidatedContextDossier 或显式 blocker。具体工作流为 dossier_synthesizer 先生成带本地 claim keys 的
section 内嵌 claim graph，服务端从唯一声明归属物化稳定 claim identities 并复用结构准入；非法引用或 graph 在既有有界模型调用内反馈，成功后才读取引用。
作者引用只能选择本次 citation_catalog 的键，结构 Schema 表达权威的引用/前提形状，解析后保留原精确引用；空目录调用前阻断。
claim_verifier 的输出须恰好覆盖 confirmed identities，再判断是否被其冻结 citations 直接支持；
每条 citation 输入保留正文、精确引用和消息角色/工具状态或结构化来源类型，沿用冻结来源，不补充未引用证据。
独立核验请求按精确引用身份建立共享 evidence 目录，每条声明只列自身 citation identities；相同来源正文只发送一次，
同正文不同身份/角色仍分别保留，声明与输出评估身份及完整请求容量准入保持。
本次身份枚举与精确数量进入同一请求 schema 和既有容量准入，不能输出新身份或用少量结果替代全部评估。
最后交给 deterministic ContextSynthesisValidator；失败保留已结构准入的 ContextSynthesisReview 与实际独立判定，供私有阶段审计精确复查。
每次模型调用在同一 finally 中收集本次 attempts；没有调用的角色不读取其上次结果，也不对真实的不同调用按内容去重。
错误摘要与 attempt 日志不包含候选正文或核验理由，未成功的作者不补造候选，未完成的核验不补造 verdict；任何失败均不回退为逐 evidence 复制。示例：
`result = await service.synthesize(observation, work_spec, bundle)`。
"""

from __future__ import annotations

from functools import partial
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
    ContextSynthesisReview,
    ContextSynthesisValidator,
    ContextSynthesisWorkerDraft,
    SynthesisSection,
)
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.context_curation import NamespacedMessageRef, evidence_ref_key
from backend.app.desktop.agent_loop.derivation_worker import StructuredResultValidationError, safe_validation_message


class StructuredContextSynthesisService:
    VERSION = "structured-context-synthesizer-v9"

    def __init__(self, synthesis_model, claim_verifier_model=None) -> None:
        self._synthesis_model = synthesis_model
        self._claim_verifier_model = claim_verifier_model or synthesis_model
        self._validator = ContextSynthesisValidator()

    def bind_usage_receipts(self, receipts):
        for model in (self._synthesis_model, self._claim_verifier_model):
            if hasattr(model, "bind_usage_receipts"):
                model.bind_usage_receipts(receipts)

    async def synthesize(
        self,
        observation: LoopObservationEnvelope,
        work_spec: WorkContextSpec,
        bundle: ResolvedEvidenceBundle,
    ) -> ContextSynthesisResult:
        attempts: list[dict[str, Any]] = []
        draft: ContextSynthesisDraft | None = None
        assessments: tuple[ClaimSupportAssessment, ...] | None = None
        try:
            worker_draft = await self._invoke_validated(
                self._synthesis_model,
                ContextSynthesisWorkerDraft.schema_for(bundle),
                self._synthesis_authority(),
                {
                    "work_spec": work_spec.model_dump(mode="json"),
                    "question_identities": {
                        self._validator.question_id(question): question
                        for question in work_spec.questions
                    },
                    "resolved_evidence": bundle.model_dump(mode="json"),
                    "citation_catalog": {
                        key: ref.model_dump(mode="json")
                        for key, ref in ContextSynthesisWorkerDraft.citation_catalog(bundle).items()
                    },
                },
                partial(self._validate_worker_draft, work_spec=work_spec, bundle=bundle),
                attempts=attempts,
            )
            draft = worker_draft.materialize()
            assessments = await self._assess_support(draft, bundle, attempts)
            dossier = self._validator.validate(
                work_spec,
                bundle,
                draft,
                assessments,
                synthesizer_version=self.VERSION,
            )
        except (KeyError, TypeError, ValueError) as exc:
            return ContextSynthesisResult(
                blocker_code="synthesis_invalid",
                blocker_summary=f"claim-level synthesis failed: {safe_validation_message(exc)[:1600]}",
                attempt_records=tuple(attempts),
                review=ContextSynthesisReview(
                    work_spec_id=work_spec.work_spec_id,
                    resolution_id=bundle.resolution_id,
                    synthesizer_version=self.VERSION,
                    draft=draft,
                    support_assessments=assessments,
                ) if draft is not None else None,
            )
        return ContextSynthesisResult(dossier=dossier, attempt_records=tuple(attempts))

    async def _assess_support(
        self, draft: ContextSynthesisDraft, bundle: ResolvedEvidenceBundle,
        attempts: list[dict[str, Any]],
    ) -> tuple[ClaimSupportAssessment, ...]:
        confirmed = tuple(item for item in draft.claims if item.authority == "confirmed")
        proposal = await self._invoke_validated(
            self._claim_verifier_model,
            ClaimSupportProposal.schema_for(tuple(claim.claim_id for claim in confirmed)),
            self._verification_authority(),
            self._support_inputs(confirmed, bundle),
            partial(self._validate_support_proposal, confirmed=confirmed),
            attempts=attempts,
        )
        by_id = {item.claim_id: item for item in proposal.assessments}
        return tuple(
            ClaimSupportAssessment(
                claim_id=claim.claim_id,
                verdict=by_id[claim.claim_id].verdict,
                citation_keys=tuple(evidence_ref_key(ref) for ref in claim.citations),
                reason=by_id[claim.claim_id].reason,
            )
            for claim in confirmed
        )

    @classmethod
    def _support_inputs(
        cls, confirmed: tuple[ContextSynthesisClaim, ...], bundle: ResolvedEvidenceBundle,
    ) -> dict[str, Any]:
        evidence = {}
        claims = []
        for claim in confirmed:
            identities = tuple(evidence_ref_key(ref) for ref in claim.citations)
            for identity, ref in zip(identities, claim.citations, strict=True):
                if identity not in evidence:
                    evidence[identity] = cls._citation_payload(bundle, ref)
            claims.append({"claim_id": claim.claim_id, "statement": claim.statement, "citations": identities})
        return {"claims": tuple(claims), "evidence": tuple(evidence.values())}

    @staticmethod
    async def _invoke_validated(model, schema, authority, payload, validator, *, attempts):
        try:
            invoke_validated = getattr(model, "invoke_validated", None)
            if invoke_validated is not None:
                return await invoke_validated(schema, authority, payload, validator)
            result = await model.invoke(schema, authority, payload)
            validator(result)
            return result
        finally:
            attempts.extend(tuple(getattr(model, "last_attempt_records", ())))

    def _validate_worker_draft(self, worker_draft, *, work_spec, bundle):
        try:
            self._validator.validate_draft(work_spec, bundle, worker_draft.materialize())
        except (KeyError, TypeError, ValueError) as exc:
            raise StructuredResultValidationError(
                "synthesis_graph_invalid",
                "Claim graph 必须使用冻结 bundle 的精确引用、已声明的 requirement/question 和有效本地 claim keys。"
                f"失败规则：{safe_validation_message(exc)[:800]}",
                unit_identity=work_spec.work_spec_id,
                violated_rule="frozen_claim_graph",
            ) from exc

    @staticmethod
    def _validate_support_proposal(proposal, *, confirmed):
        identities = tuple(item.claim_id for item in proposal.assessments)
        expected = {claim.claim_id for claim in confirmed}
        if len(identities) != len(set(identities)) or set(identities) != expected:
            raise StructuredResultValidationError(
                "synthesis_support_contract_invalid",
                "assessments 必须恰好逐项覆盖输入 claims 的 claim_id，不得重复、遗漏或新增；不改变实际支持判断。",
                unit_identity="confirmed_claims",
                violated_rule="exact_confirmed_claim_identity",
            )

    @staticmethod
    def _citation_payload(bundle: ResolvedEvidenceBundle, ref) -> dict[str, Any]:
        if isinstance(ref, NamespacedMessageRef):
            evidence = bundle.evidence.message(ref)
            source = evidence.model_dump(mode="json", exclude={"content"})
        else:
            evidence = bundle.evidence.structured_item(ref)
            source = {"ref": evidence.ref.model_dump(mode="json")}
        return {"identity": evidence_ref_key(ref), "content": evidence.content, "source": source}

    @staticmethod
    def _content(bundle: ResolvedEvidenceBundle, ref) -> Any:
        if isinstance(ref, NamespacedMessageRef):
            return bundle.evidence.message(ref).content
        return bundle.evidence.structured_item(ref).content

    @staticmethod
    def _synthesis_authority() -> str:
        return "你是无权 dossier_synthesizer。只基于输入冻结 WorkSpec 与 ResolvedEvidenceBundle 生成按 section 内嵌的唯一原子 claim 清单；citations 必须选择 citation_catalog 的字符串键，不得自行拼写引用对象或添加目录外身份；confirmed 只能表达 citation 直接支持的事实且不得有 premise，跨来源关系必须标为 inference 且至少有一个 premise，premise 只能引用本输出已声明的本地 claim_key 且依赖无环，分组显示顺序不决定依赖顺序，未证实原因必须 hypothesis 且保留 citation 或 premise。每个 required requirement/question 必须映射或显式列为 unresolved。不得读取外部历史、改写 WorkSpec、执行工作或创建 Context。"

    @staticmethod
    def _verification_authority() -> str:
        return "你是无权 claim_verifier。evidence 是按精确 identity 保留正文与来源的共享目录；每条 claim 的 citations 只列其获准引用的 identities，按 identity 查找对应 evidence 后逐项判断 confirmed atomic claim 是否被这些 citations 直接蕴含，只返回 supported、unsupported 或 unknown 与简短理由；不得使用未被该 claim 引用的其他 evidence、补充证据、改写 claim、进行常识推断或输出执行指令。"


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
