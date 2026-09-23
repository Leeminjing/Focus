r"""本文件对外提供 ContextExpansionRepository 与 ExpansionRepositoryRejected。

输入为 AsyncSession、冻结 WorkContext opportunity、policy level、目标 lifecycle state、阶段合同与结果引用；输出为幂等持久化的
LoopContextExpansion 与追加 transition/journal 事件。具体工作流为 create 按稳定 identity 查重并依次记录 signals、projection、
planning、admission，transition 行锁记录并验证后冻结 resolution/compiled plan；活动覆盖只包含未决或仍具 active Lane 的 expansion。
示例：`row = await repository.create(session, opportunity, ...)`。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    DerivationStageRecord,
    ExpansionBlockerCode,
    ExpansionOpportunity,
)
from backend.app.desktop.agent_loop.context_expansion.lifecycle import (
    ExpansionLifecycleStateMachine,
    ExpansionTransitionRejected,
)
from backend.app.desktop.agent_loop.context_expansion.models import (
    LoopContextExpansion,
    LoopContextExpansionTransition,
)
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.models import LoopContextMembership
from backend.app.desktop.context_curation.models import CurationLane


class ExpansionRepositoryRejected(ValueError):
    pass


class ContextExpansionRepository:
    _TERMINAL = frozenset({"dispatched", "declined", "blocked", "failed", "superseded"})

    def __init__(self) -> None:
        self._machine = ExpansionLifecycleStateMachine()
        self._journal = LoopEventJournal()

    @classmethod
    def is_terminal(cls, state: str) -> bool:
        return state in cls._TERMINAL

    async def create(
        self,
        session: AsyncSession,
        opportunity: ExpansionOpportunity,
        *,
        policy_version: str,
        level: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        stage_records: tuple[DerivationStageRecord, ...] = (),
    ) -> LoopContextExpansion:
        existing = await session.get(LoopContextExpansion, opportunity.opportunity_id, with_for_update=True)
        if existing is not None:
            return existing
        summary = f"已收集 Context 派生 signals：{opportunity.work_spec.objective}"[:1000]
        first_source = opportunity.manifest_sources[0] if opportunity.manifest_sources else None
        records = self._stage_records(opportunity, policy_version, stage_records)
        record_payloads = {item.stage: item.model_dump(mode="json") for item in records}
        row = LoopContextExpansion(
            expansion_id=opportunity.opportunity_id,
            opportunity_id=opportunity.opportunity_id,
            loop_id=opportunity.loop_id,
            round_id=opportunity.round_id,
            source_context_id=first_source.context_id if first_source else None,
            source_revision_id=first_source.revision_id if first_source else None,
            state="signals_collected",
            policy_version=policy_version,
            level=level,
            independence_key=opportunity.independence_key,
            semantic_fingerprint=opportunity.semantic_fingerprint,
            workspace_mode=opportunity.work_spec.workspace_requirement,
            opportunity=opportunity.model_dump(mode="json"),
            work_spec=opportunity.work_spec.model_dump(mode="json"),
            manifest_ids=list(opportunity.manifest_ids),
            source_frontier=[source.model_dump(mode="json") for source in opportunity.manifest_sources],
            planner_version=opportunity.work_spec.planner_version,
            projector_version=opportunity.projector_versions[0] if len(opportunity.projector_versions) == 1 else None,
            stage_identities={
                "observation_hash": opportunity.observation_hash,
                "signal_ids": list(opportunity.signal_ids),
                "manifest_ids": list(opportunity.manifest_ids),
                "projector_versions": list(opportunity.projector_versions),
                "work_spec_id": opportunity.work_spec.work_spec_id,
                "stages": record_payloads,
            },
            safe_summary=summary,
            correlation_id=correlation_id or opportunity.opportunity_id,
            causation_id=causation_id,
            result={"stage_records": {"signal_collection": record_payloads["signal_collection"]}},
        )
        session.add(row)
        await session.flush()
        session.add(
            LoopContextExpansionTransition(
                transition_id=uuid.uuid4().hex,
                expansion_id=row.expansion_id,
                loop_id=row.loop_id,
                round_id=row.round_id,
                revision=1,
                from_state="signals_collected",
                to_state="signals_collected",
                safe_summary=summary,
                result=row.result,
            )
        )
        await self._event(session, row)
        row = await self.transition(
            session,
            row.expansion_id,
            "portfolio_projected",
            f"已冻结 {len(opportunity.manifest_ids)} 个 semantic manifests",
            result={"stage_record": record_payloads["portfolio_projection"]},
        )
        row = await self.transition(
            session,
            row.expansion_id,
            "work_planned",
            f"已冻结 WorkContextSpec：{opportunity.work_spec.work_spec_id}",
            result={"stage_record": record_payloads["cognitive_planning"]},
        )
        row = await self.transition(
            session,
            row.expansion_id,
            "admitted",
            f"Admission policy 接受派生：{opportunity.work_spec.objective}",
            result={"stage_record": record_payloads["admission"]},
        )
        return row

    async def transition(
        self,
        session: AsyncSession,
        expansion_id: str,
        target: str,
        summary: str,
        *,
        blocker_code: ExpansionBlockerCode | None = None,
        result: dict | None = None,
    ) -> LoopContextExpansion:
        row = await session.get(LoopContextExpansion, expansion_id, with_for_update=True)
        if row is None:
            raise LookupError("Context expansion 不存在")
        if row.state == target:
            return row
        try:
            self._machine.validate(row.state, target)
        except ExpansionTransitionRejected as exc:
            raise ExpansionRepositoryRejected(str(exc)) from exc
        previous = row.state
        row.state = target
        row.revision += 1
        row.safe_summary = summary[:1000]
        row.blocker_code = blocker_code
        if result is not None:
            appended_records = tuple(result.get("stage_records_append") or ())
            stage_record = result.get("stage_record")
            public_result = {
                key: value
                for key, value in result.items()
                if key not in {"stage_record", "stage_records_append"}
            }
            stage_results = dict((row.result or {}).get("stage_records") or {})
            if stage_record is not None:
                stage_results[str(stage_record["stage"])] = stage_record
            for appended in appended_records:
                stage_results[str(appended["stage"])] = appended
            if stage_record is not None or appended_records:
                identities = dict(row.stage_identities or {})
                identities["stages"] = {**dict(identities.get("stages") or {}), **stage_results}
                row.stage_identities = identities
            row.result = {**(row.result or {}), **public_result, "stage_records": stage_results}
            if result.get("resolution") is not None:
                row.resolution = result["resolution"]
            if result.get("evidence_frontier") is not None:
                row.evidence_frontier = result["evidence_frontier"]
            if result.get("resolver_version") is not None:
                row.resolver_version = result["resolver_version"]
            if result.get("compiled_plan") is not None:
                row.compiled_plan = result["compiled_plan"]
            if result.get("definition_hash") is not None:
                row.definition_hash = result["definition_hash"]
        if target in self._TERMINAL:
            row.completed_at = datetime.now(UTC)
        session.add(
            LoopContextExpansionTransition(
                transition_id=uuid.uuid4().hex,
                expansion_id=row.expansion_id,
                loop_id=row.loop_id,
                round_id=row.round_id,
                revision=row.revision,
                from_state=previous,
                to_state=target,
                blocker_code=blocker_code,
                safe_summary=row.safe_summary,
                result=row.result,
            )
        )
        await self._event(session, row)
        return row

    @staticmethod
    def _stage_records(
        opportunity: ExpansionOpportunity,
        policy_version: str,
        supplied: tuple[DerivationStageRecord, ...],
    ) -> tuple[DerivationStageRecord, ...]:
        by_stage = {item.stage: item for item in supplied}
        defaults = (
            DerivationStageRecord(
                stage="signal_collection",
                input_identities=(opportunity.observation_hash,),
                output_identities=opportunity.signal_ids,
                version="semantic-expansion-signals-v1",
                duration_ms=0,
                safe_summary="已收集结构化派生信号",
            ),
            DerivationStageRecord(
                stage="portfolio_projection",
                input_identities=(opportunity.observation_hash,),
                output_identities=opportunity.manifest_ids,
                version=opportunity.projector_versions[0] if opportunity.projector_versions else "semantic-manifest-projector-v1",
                duration_ms=0,
                safe_summary="已冻结 semantic manifests",
            ),
            DerivationStageRecord(
                stage="cognitive_planning",
                input_identities=opportunity.manifest_ids,
                output_identities=(opportunity.work_spec.work_spec_id,),
                version=opportunity.work_spec.planner_version,
                duration_ms=0,
                safe_summary="已冻结 WorkContextSpec",
            ),
            DerivationStageRecord(
                stage="admission",
                input_identities=(opportunity.work_spec.work_spec_id,),
                output_identities=(opportunity.opportunity_id,),
                version=policy_version,
                duration_ms=0,
                safe_summary="Admission policy 接受派生",
            ),
        )
        return tuple(by_stage.get(item.stage, item) for item in defaults)

    async def by_round(self, session: AsyncSession, round_id: str) -> tuple[LoopContextExpansion, ...]:
        return tuple(
            (
                await session.scalars(
                    select(LoopContextExpansion)
                    .where(LoopContextExpansion.round_id == round_id)
                    .order_by(LoopContextExpansion.created_at, LoopContextExpansion.expansion_id)
                )
            ).all()
        )

    async def by_directive(self, session: AsyncSession, directive_id: str) -> LoopContextExpansion | None:
        return await session.scalar(
            select(LoopContextExpansion).where(
                LoopContextExpansion.result["directive_id"].astext == directive_id
            )
        )

    async def active_independence_keys(
        self,
        session: AsyncSession,
        loop_id: str,
        *,
        exclude_round_id: str | None = None,
    ) -> frozenset[str]:
        pending = select(LoopContextExpansion.independence_key).where(
            LoopContextExpansion.loop_id == loop_id,
            LoopContextExpansion.state.not_in(self._TERMINAL),
        )
        active_lane = (
            select(LoopContextExpansion.independence_key)
            .join(
                LoopContextMembership,
                (LoopContextMembership.loop_id == LoopContextExpansion.loop_id)
                & (LoopContextMembership.lane_id == LoopContextExpansion.result["lane_id"].astext),
            )
            .join(CurationLane, CurationLane.lane_id == LoopContextMembership.lane_id)
            .where(
                LoopContextExpansion.loop_id == loop_id,
                LoopContextExpansion.state == "dispatched",
                LoopContextMembership.status == "active",
                CurationLane.lifecycle == "active",
            )
        )
        if exclude_round_id is not None:
            pending = pending.where(LoopContextExpansion.round_id != exclude_round_id)
            active_lane = active_lane.where(LoopContextExpansion.round_id != exclude_round_id)
        pending_values = await session.scalars(pending)
        active_values = await session.scalars(active_lane)
        return frozenset((*pending_values.all(), *active_values.all()))

    async def _event(self, session: AsyncSession, row: LoopContextExpansion) -> None:
        await self._journal.append(
            session,
            row.loop_id,
            CanonicalEventDraft(
                kind=f"context_expansion.{row.state}",
                entity_type="context_expansion",
                entity_id=row.expansion_id,
                entity_revision=row.revision,
                correlation_id=row.correlation_id,
                causation_id=row.causation_id,
                payload={
                    "expansion_id": row.expansion_id,
                    "opportunity_id": row.opportunity_id,
                    "round_id": row.round_id,
                    "source_frontier": row.source_frontier,
                    "evidence_frontier": row.evidence_frontier,
                    "work_spec_id": row.work_spec.get("work_spec_id"),
                    "stage_identities": row.stage_identities,
                    "state": row.state,
                    "level": row.level,
                    "policy_version": row.policy_version,
                    "workspace_mode": row.workspace_mode,
                    "independence_key": row.independence_key,
                    "safe_summary": row.safe_summary,
                    "blocker_code": row.blocker_code,
                    "result": row.result,
                },
                idempotency_key=f"context-expansion:{row.expansion_id}:revision:{row.revision}",
            ),
        )
