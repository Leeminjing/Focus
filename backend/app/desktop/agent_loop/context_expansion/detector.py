r"""本文件对外提供 ExpansionOpportunityDetector。

输入为冻结 LoopObservationEnvelope；输出为不调用模型的确定性 ExpansionOpportunity 集合。具体工作流为读取精确
frontier、结构化 Mission checks、稳定失败、预算压力和 Portfolio 用户意图，生成带稳定 identity 的候选并按 independence
key 去重。示例：`opportunities = detector.detect(observation)`。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from backend.app.desktop.agent_loop.context_expansion.contracts import ExpansionOpportunity
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.context_evolution import ContextRevisionRef


class ExpansionOpportunityDetector:
    def detect(self, observation: LoopObservationEnvelope) -> tuple[ExpansionOpportunity, ...]:
        source = self._source(observation)
        if source is None:
            return ()
        candidates = [
            *self._mission_opportunities(observation, source),
            *self._failure_opportunities(observation, source),
            *self._token_opportunities(observation, source),
            *self._user_opportunities(observation, source),
        ]
        unique: dict[str, ExpansionOpportunity] = {}
        for candidate in candidates:
            unique.setdefault(candidate.independence_key.casefold(), candidate)
        return tuple(unique.values())

    @staticmethod
    def _source(observation: LoopObservationEnvelope) -> ContextRevisionRef | None:
        for item in observation.portfolio_frontier:
            revision = item.get("revision")
            if revision:
                return ContextRevisionRef.model_validate(revision)
        return None

    def _mission_opportunities(
        self,
        observation: LoopObservationEnvelope,
        source: ContextRevisionRef,
    ) -> Iterable[ExpansionOpportunity]:
        mission = observation.mission or {}
        checks = tuple(item for item in mission.get("completion_checks", ()) if item.get("required", True))
        existing_roles = " ".join(str(item.get("role") or "") for item in observation.portfolio_frontier).casefold()
        if len(checks) < 2:
            return ()
        result = []
        workspace_mode, workspace_triggers = self._workspace_mode(observation, " ".join(str(item) for item in checks))
        for check in checks:
            evidence_kinds = tuple(check.get("expected_evidence_kinds") or ())
            is_verification = bool(set(evidence_kinds) & {"test", "tool", "artifact", "fact", "user"})
            check_id = str(check.get("check_id") or "completion")
            if is_verification and not any(role in existing_roles for role in ("test", "verif", "review")):
                result.append(
                    ExpansionOpportunity.create(
                        loop_id=observation.loop_id,
                        round_id=observation.round_id,
                        source=source,
                        purpose=f"Independent verification: {check.get('claim', check_id)}",
                        work_order=f"Independently gather {', '.join(evidence_kinds) or 'verifiable'} evidence for completion check {check_id} without changing the primary execution direction.",
                        completion_check=str(check.get("claim") or check_id),
                        workspace_mode=workspace_mode,
                        independence_key=f"completion-check:{check_id}",
                        triggers=("mission_checks", "independent_verification", *workspace_triggers),
                        evidence_hints=(check_id, *evidence_kinds),
                        required=True,
                    )
                )
        return tuple(result)

    def _failure_opportunities(
        self,
        observation: LoopObservationEnvelope,
        source: ContextRevisionRef,
    ) -> Iterable[ExpansionOpportunity]:
        failures = tuple(
            item for item in observation.stable_results
            if str(item.get("status") or "").casefold() in {"error", "failed", "failure"}
        )
        usage = observation.budget.get("usage", {})
        if not failures or int(usage.get("no_progress_count", 0) or 0) < 2:
            return ()
        ids = tuple(str(item.get("run_id") or item.get("context_id") or "failure") for item in failures[-3:])
        return (
            ExpansionOpportunity.create(
                loop_id=observation.loop_id,
                round_id=observation.round_id,
                source=source,
                purpose="Independent repeated-failure analysis",
                work_order="Analyze the repeated failure evidence independently, identify a falsifiable root cause, and do not modify the authoritative workspace.",
                completion_check="Produce a root-cause hypothesis tied to reproducible evidence or disprove the suspected cause.",
                workspace_mode="read_only",
                independence_key="repeated-failure-analysis",
                triggers=("repeated_failure",),
                evidence_hints=ids,
                required=True,
            ),
        )

    def _token_opportunities(
        self,
        observation: LoopObservationEnvelope,
        source: ContextRevisionRef,
    ) -> Iterable[ExpansionOpportunity]:
        limits = observation.budget.get("limits", {})
        usage = observation.budget.get("usage", {})
        limit = int(limits.get("max_input_tokens", 0) or 0)
        used = int(usage.get("input_tokens", 0) or 0)
        if limit <= 0 or used / limit < 0.8:
            return ()
        outcome = str((observation.mission or {}).get("outcome") or "the current mission")
        return (
            ExpansionOpportunity.create(
                loop_id=observation.loop_id,
                round_id=observation.round_id,
                source=source,
                purpose="Clean-context continuation",
                work_order=f"Continue the remaining work for {outcome} from a curated minimal evidence set.",
                completion_check="Demonstrate forward progress without depending on omitted conversation history.",
                workspace_mode="read_only",
                independence_key="token-pressure-continuation",
                triggers=("token_pressure",),
                evidence_hints=(f"input_tokens:{used}/{limit}",),
                required=used / limit >= 0.9,
            ),
        )

    def _user_opportunities(
        self,
        observation: LoopObservationEnvelope,
        source: ContextRevisionRef,
    ) -> Iterable[ExpansionOpportunity]:
        markers = ("并行", "独立", "另一个 context", "parallel", "separate context", "in parallel")
        intents = tuple(
            item for item in observation.user_intents
            if item.get("scope") == "portfolio" and any(marker in str(item.get("content") or "").casefold() for marker in markers)
        )
        result = []
        for item in intents:
            content = str(item.get("content") or "Run the requested work independently.")
            workspace_mode, workspace_triggers = self._workspace_mode(observation, content)
            result.append(ExpansionOpportunity.create(
                loop_id=observation.loop_id,
                round_id=observation.round_id,
                source=source,
                purpose="User-requested parallel investigation",
                work_order=content,
                completion_check="Return an independently verifiable result for the user's parallel request.",
                workspace_mode=workspace_mode,
                independence_key=f"user-intent:{item.get('intent_id')}",
                triggers=("user_parallel_intent", *workspace_triggers),
                evidence_hints=(str(item.get("intent_id") or ""),),
                required=True,
            ))
        return tuple(result)

    @staticmethod
    def _workspace_mode(observation: LoopObservationEnvelope, text: str) -> tuple[str, tuple[str, ...]]:
        workspace = observation.workspace or {}
        explicit_write = bool(workspace.get("parallel_write_required") or workspace.get("requires_isolation"))
        write_markers = ("修改", "修复", "实现", "write", "implement", "fix", "edit")
        if explicit_write and any(marker in text.casefold() for marker in write_markers):
            return "isolated_write", ("workspace_state",)
        return "read_only", ()
