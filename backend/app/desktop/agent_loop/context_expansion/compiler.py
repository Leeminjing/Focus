r"""本文件对外提供 ExpansionPlanCompilerPort、ContextExpansionPlanCompiler 与 DeterministicExpansionPlanCompiler。

输入为冻结 observation、identity-only SpawnContextIntent、WorkContext opportunity、resolved multi-source evidence 与可选 dossier；
输出为 CompiledExpansion 或阶段专属 ExpansionBlocker。具体工作流为 production façade 重建并验证 manifests、读取授权 corpus、
解析 required evidence、尝试 dossier synthesis，再由纯 compiler 按 Work Contract、Evidence Ledger、Dossier、Primary Evidence 顺序
生成 CreateLanePlan 并调用通用 lane compiler；本文件不选择最近消息、不做 substring coverage。示例：
`compiled = await compiler.compile(observation, opportunity, intent)`。
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    CompiledExpansion,
    DerivationStageRecord,
    ExpansionBlocker,
    ExpansionOpportunity,
    ResolvedEvidenceBundle,
    SpawnContextIntent,
    stable_expansion_hash,
)
from backend.app.desktop.agent_loop.context_expansion.dossier import (
    DossierSynthesizerPort,
    EvidenceGroundedDossier,
    ResolvedEvidenceDossierSynthesizer,
    render_dossier,
)
from backend.app.desktop.agent_loop.context_expansion.evidence_corpus import (
    EvidenceCorpusReadError,
    FrozenEvidenceCorpusReader,
)
from backend.app.desktop.agent_loop.context_expansion.evidence_resolver import (
    MultiSourceEvidenceResolver,
)
from backend.app.desktop.agent_loop.context_expansion.manifest_adapter import (
    CompositeSemanticManifestProjector,
)
from backend.app.desktop.agent_loop.context_expansion.stage_telemetry import (
    DerivationStageTimer,
)
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.context_curation import (
    ComposeMessage,
    CopyMessage,
    CreateLanePlan,
    MultiSourceEvidence,
    ToolExchange,
    ToolExchangeCall,
    compile_lane,
    evidence_ref_key,
)


class ExpansionPlanCompilerPort(Protocol):
    async def compile(
        self,
        observation: LoopObservationEnvelope,
        opportunity: ExpansionOpportunity,
        intent: SpawnContextIntent,
    ) -> CompiledExpansion | ExpansionBlocker: ...


class ContextExpansionPlanCompiler:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        checkpointer: Any,
        *,
        projector: CompositeSemanticManifestProjector | None = None,
        resolver: MultiSourceEvidenceResolver | None = None,
        synthesizer: DossierSynthesizerPort | None = None,
    ) -> None:
        self._projector = projector or CompositeSemanticManifestProjector()
        self._reader = FrozenEvidenceCorpusReader(sessions, checkpointer)
        self._resolver = resolver or MultiSourceEvidenceResolver()
        self._synthesizer = synthesizer or ResolvedEvidenceDossierSynthesizer()
        self._compiler = DeterministicExpansionPlanCompiler()

    async def compile(
        self,
        observation: LoopObservationEnvelope,
        opportunity: ExpansionOpportunity,
        intent: SpawnContextIntent,
    ) -> CompiledExpansion | ExpansionBlocker:
        if intent.opportunity_id != opportunity.opportunity_id:
            return self._blocked(opportunity, "stale_source", "semantic intent 与冻结 opportunity 不一致")
        records: list[DerivationStageRecord] = []
        resolution_timer = DerivationStageTimer(
            "evidence_resolution",
            (opportunity.work_spec.work_spec_id, *opportunity.manifest_ids),
            self._resolver.VERSION,
        )
        try:
            manifests = self._projector.project(observation)
            corpus = await self._reader.read(observation, opportunity, manifests)
        except EvidenceCorpusReadError as exc:
            records.append(resolution_timer.finish((), exc.summary))
            return self._blocked(opportunity, exc.code, exc.summary, tuple(records))
        except (TypeError, ValueError) as exc:
            records.append(resolution_timer.finish((), "Evidence corpus projection 失败"))
            return self._blocked(opportunity, "portfolio_projection_failed", str(exc), tuple(records))
        limit = int((observation.budget.get("limits") or {}).get("max_expansion_evidence_items", 128) or 128)
        resolved = self._resolver.resolve(opportunity, manifests, corpus, max_items=max(1, limit))
        if isinstance(resolved, ExpansionBlocker):
            record = resolution_timer.finish((), resolved.summary)
            return resolved.model_copy(update={"stage_records": (*resolved.stage_records, record)})
        records.append(
            resolution_timer.finish(
                (resolved.resolution_id,),
                f"已解析 {len(resolved.items)} 项 requirement evidence",
            )
        )
        dossier_timer = DerivationStageTimer(
            "dossier_synthesis",
            (resolved.resolution_id,),
            type(self._synthesizer).__name__,
        )
        dossier_result = await self._synthesizer.synthesize(observation, resolved)
        records.append(
            dossier_timer.finish(
                (dossier_result.dossier.dossier_id,) if dossier_result.dossier else (),
                dossier_result.omitted_reason or "已构建带引用的 evidence-grounded dossier",
            )
        )
        compilation_timer = DerivationStageTimer(
            "compilation",
            (resolved.resolution_id, *(record.output_identities[0] for record in records[-1:] if record.output_identities)),
            self._compiler.VERSION,
        )
        compiled = self._compiler.compile(
            opportunity,
            intent,
            resolved,
            dossier=dossier_result.dossier,
            synthesis_omitted=dossier_result.dossier is None,
            stage_records=tuple(records),
        )
        if isinstance(compiled, ExpansionBlocker):
            record = compilation_timer.finish((), compiled.summary)
            return compiled.model_copy(update={"stage_records": (*compiled.stage_records, record)})
        record = compilation_timer.finish((compiled.definition_hash,), "已编译确定性 LanePlan")
        return compiled.model_copy(update={"stage_records": (*compiled.stage_records, record)})

    @staticmethod
    def _blocked(
        opportunity: ExpansionOpportunity,
        code: str,
        summary: str,
        stage_records: tuple[DerivationStageRecord, ...] = (),
    ) -> ExpansionBlocker:
        return ExpansionBlocker(
            code=code,
            summary=summary[:2000],
            opportunity_id=opportunity.opportunity_id,
            stage_records=stage_records,
        )


class DeterministicExpansionPlanCompiler:
    VERSION = "semantic-context-compiler-v2"

    def compile(
        self,
        opportunity: ExpansionOpportunity,
        intent: SpawnContextIntent,
        bundle: ResolvedEvidenceBundle,
        *,
        dossier: EvidenceGroundedDossier | None = None,
        synthesis_omitted: bool = False,
        stage_records: tuple[DerivationStageRecord, ...] = (),
    ) -> CompiledExpansion | ExpansionBlocker:
        try:
            plan = self._plan(opportunity, bundle, dossier)
            compiled_lane = compile_lane(plan.model_dump(mode="json"), bundle.evidence)
        except (KeyError, TypeError, ValueError) as exc:
            return ExpansionBlocker(
                code="compiler_failed",
                summary=f"无法从 resolved evidence 编译可运行 Context：{str(exc)[:1200]}",
                opportunity_id=opportunity.opportunity_id,
            )
        expansion_id = stable_expansion_hash(
            "compiled-expansion-v2",
            opportunity.opportunity_id,
            intent.model_dump(mode="json"),
            bundle.resolution_id,
            dossier.dossier_id if dossier else None,
            compiled_lane.definition_hash,
            self.VERSION,
        )
        return CompiledExpansion(
            expansion_id=expansion_id,
            opportunity=opportunity,
            intent=intent,
            resolution=bundle,
            plan=plan,
            definition_hash=compiled_lane.definition_hash,
            compiler_version=self.VERSION,
            dossier_id=dossier.dossier_id if dossier else None,
            synthesis_omitted=synthesis_omitted,
            stage_records=stage_records,
        )

    def _plan(
        self,
        opportunity: ExpansionOpportunity,
        bundle: ResolvedEvidenceBundle,
        dossier: EvidenceGroundedDossier | None,
    ) -> CreateLanePlan:
        if not bundle.evidence_frontier:
            raise ValueError("自动派生 Context 至少需要一项 resolved evidence")
        items: list[ComposeMessage | CopyMessage | ToolExchange] = [
            ComposeMessage(
                type="compose_message",
                role="system",
                content=self._work_contract(opportunity),
                sources=bundle.evidence_frontier,
            ),
            ComposeMessage(
                type="compose_message",
                role="system",
                content=self._evidence_ledger(opportunity, bundle),
                sources=bundle.evidence_frontier,
            ),
        ]
        if dossier is not None and dossier.statements:
            dossier_sources = tuple(
                sorted(
                    {
                        evidence_ref_key(ref): ref
                        for statement in dossier.statements
                        for ref in statement.citations
                    }.values(),
                    key=evidence_ref_key,
                )
            )
            items.append(
                ComposeMessage(
                    type="compose_message",
                    role="system",
                    content=render_dossier(dossier),
                    sources=dossier_sources,
                )
            )
        items.extend(self._primary_evidence(bundle.evidence))
        return CreateLanePlan(
            action="create",
            purpose=opportunity.work_spec.objective,
            source_frontier=bundle.source_frontier,
            evidence_frontier=bundle.evidence_frontier,
            items=tuple(items),
            lane_policy={
                "expansion_id": opportunity.opportunity_id,
                "work_spec_id": opportunity.work_spec.work_spec_id,
                "resolution_id": bundle.resolution_id,
                "dossier_id": dossier.dossier_id if dossier else None,
                "workspace_mode": opportunity.work_spec.workspace_requirement,
                "completion_criteria": opportunity.work_spec.completion_criteria,
            },
        )

    @staticmethod
    def _work_contract(opportunity: ExpansionOpportunity) -> str:
        spec = opportunity.work_spec
        questions = "\n".join(f"- {item}" for item in spec.questions)
        completion = "\n".join(f"- {item}" for item in spec.completion_criteria)
        return (
            "Work Contract\n"
            f"Objective: {spec.objective}\n"
            f"Separation reason: {spec.separation_reason}\n"
            f"Workspace requirement: {spec.workspace_requirement}\n"
            f"Questions:\n{questions}\n"
            f"Completion criteria:\n{completion}"
        )

    @staticmethod
    def _evidence_ledger(
        opportunity: ExpansionOpportunity,
        bundle: ResolvedEvidenceBundle,
    ) -> str:
        by_requirement: dict[str, list[str]] = {}
        for item in bundle.items:
            by_requirement.setdefault(item.requirement_id, []).append("/".join(evidence_ref_key(item.ref)))
        lines = ["Evidence Ledger"]
        for requirement in opportunity.work_spec.evidence_requirements:
            refs = sorted(by_requirement.get(requirement.requirement_id, ()))
            rendered = ", ".join(refs) if refs else "omitted (optional)"
            lines.append(f"- {requirement.requirement_id} [{requirement.role}/{requirement.necessity}]: {rendered}")
        return "\n".join(lines)

    @staticmethod
    def _primary_evidence(
        evidence: MultiSourceEvidence,
    ) -> tuple[ComposeMessage | CopyMessage | ToolExchange, ...]:
        items: list[ComposeMessage | CopyMessage | ToolExchange] = []
        for source in evidence.sources:
            results = {message.tool_call_id: message for message in source.messages if message.tool_call_id}
            consumed: set[tuple[str, str, str, str]] = set()
            for message in source.messages:
                if message.ref.key in consumed or message.role == "tool":
                    continue
                if message.tool_calls:
                    siblings = tuple(results.get(str(call.get("id") or "")) for call in message.tool_calls)
                    if any(result is None for result in siblings):
                        raise ValueError("resolved Tool Exchange 不完整")
                    calls = tuple(
                        ToolExchangeCall(
                            name=str(call.get("name") or ""),
                            args=dict(call.get("args") or {}),
                            result_content=result.content,
                            status=result.status or "success",
                        )
                        for call, result in zip(message.tool_calls, siblings, strict=True)
                        if result is not None
                    )
                    refs = (message.ref, *(result.ref for result in siblings if result is not None))
                    items.append(
                        ToolExchange(
                            type="tool_exchange",
                            assistant_content=str(message.content or ""),
                            calls=calls,
                            sources=refs,
                        )
                    )
                    consumed.update(ref.key for ref in refs)
                else:
                    items.append(CopyMessage(type="copy_message", source=message.ref))
                    consumed.add(message.ref.key)
        for item in sorted(evidence.structured, key=lambda value: evidence_ref_key(value.ref)):
            content = item.content if isinstance(item.content, str) else json.dumps(
                item.content,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            items.append(
                ComposeMessage(
                    type="compose_message",
                    role="system",
                    content=f"Primary structured evidence [{'/'.join(evidence_ref_key(item.ref))}]:\n{content}",
                    sources=(item.ref,),
                )
            )
        return tuple(items)
