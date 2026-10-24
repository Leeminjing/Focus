"""本文件对外提供 RoundConsequenceReader 与 RoundConsequences 快照。

输入为同事务 session 与冻结 LoopRound；输出为 stable、pending、failed、实际数量及未完成耐久身份。
具体工作流为读取本轮授权 Run 的结算、dispatch、Directive、Worker、模型预留及唯一 Decision 的
Portfolio/Workspace 后果；已结算的失败 Run 作为下轮证据，未处理的发布/采用失败阻止正常收口。
只读取现有实体，不启动执行、写工作区或另建 scheduler；示例：state = await reader.read(session, round_row)。
缺有效权威 Decision 的历史事务保持不稳定，只有耐久终止事件明确证明在授权前终止时可免除决策后果；清理仍逐项检查。
"""

from dataclasses import dataclass

from sqlalchemy import and_, or_, select

from backend.app.desktop.patrol.agent_loop.models import LoopAction, LoopDecision, LoopDirective, LoopWorkerRequest, LoopPatrolAttempt, LoopRound, LoopEventOutbox
from backend.app.desktop.patrol.agent_loop.context_expansion.models import LoopIndexBudgetReservation
from backend.app.desktop.contexts.curation.models import PortfolioRevision
from backend.app.desktop.persistence.models import DesktopRun, ModelAttemptAudit
from backend.app.desktop.runs.orchestration.models import RunDispatch
from backend.app.desktop.workspaces.coordination.models import WorkspaceAdoption


@dataclass(frozen=True)
class RoundConsequences:
    pending: tuple[str, ...]
    counts: dict[str, int]
    identities: dict[str, tuple[str, ...]]
    failed: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()

    @property
    def stable(self) -> bool:
        return not self.pending and not self.failed


class RoundConsequenceReader:
    async def read(self, session, round_row):
        scope = (DesktopRun.loop_id == round_row.loop_id, DesktopRun.round_id == round_row.round_id)
        identities = {}
        queries = {
            "unsettled_runs": select(DesktopRun.run_id).where(*scope,
                or_(DesktopRun.status.in_(("pending", "running")), DesktopRun.settled_at.is_(None))),
            "active_dispatches": select(RunDispatch.dispatch_id).join(DesktopRun,
                DesktopRun.run_id == RunDispatch.run_id).where(*scope,
                RunDispatch.status.in_(("accepted", "claimed", "running"))),
            "active_directives": select(LoopDirective.directive_id).where(LoopDirective.round_id == round_row.round_id,
                LoopDirective.lifecycle_state.not_in(("settled", "failed", "cancelled", "rejected", "delivery_failed"))),
            "active_workers": select(LoopWorkerRequest.worker_request_id).where(LoopWorkerRequest.round_id == round_row.round_id,
                or_(LoopWorkerRequest.status.in_(("pending", "running")),
                    and_(LoopWorkerRequest.status.in_(("superseded", "cancelled")), LoopWorkerRequest.completed_at.is_(None)))),
            "cognition_cleanup": select(LoopPatrolAttempt.patrol_attempt_id).where(
                LoopPatrolAttempt.round_id == round_row.round_id,
                LoopPatrolAttempt.status.in_(("superseded", "interrupted", "cancelled")),
                LoopPatrolAttempt.completed_at.is_(None)),
            "pending_cancellations": select(LoopEventOutbox.event_id).where(
                LoopEventOutbox.loop_id == round_row.loop_id, LoopEventOutbox.status == "pending",
                LoopEventOutbox.event_type.in_(("loop.cognition.preempted", "loop.execution.cancel_requested")),
                or_(LoopEventOutbox.event_type == "loop.execution.cancel_requested",
                    LoopEventOutbox.payload["requires_cleanup"].as_boolean().is_(True)),
                LoopEventOutbox.payload["round_id"].astext == round_row.round_id),
            "prepared_models": select(ModelAttemptAudit.attempt_id).join(DesktopRun,
                ModelAttemptAudit.run_id == DesktopRun.run_id).where(*scope, ModelAttemptAudit.status == "prepared"),
            "unreported_models": select(LoopIndexBudgetReservation.reservation_id).where(
                LoopIndexBudgetReservation.loop_id == round_row.loop_id,
                LoopIndexBudgetReservation.actual_usage["round_id"].astext == round_row.round_id,
                LoopIndexBudgetReservation.settled_at.is_(None),
                LoopIndexBudgetReservation.actual_usage["receipt_state"].astext.is_distinct_from("unknown")),
        }
        for kind, query in queries.items():
            identities[kind] = tuple(sorted((await session.scalars(query)).all()))
        decision = await session.get(LoopDecision, round_row.decision_id) if round_row.decision_id else None
        authorized = bool(decision and decision.authority_committed_at is not None
            and (decision.loop_id, decision.round_id) == (round_row.loop_id, round_row.round_id))
        preauthorization_termination = False
        if round_row.decision_id is None and round_row.status in {"error", "superseded"}:
            preauthorization_termination = bool(await session.scalar(select(LoopEventOutbox.event_id).where(
                LoopEventOutbox.loop_id == round_row.loop_id, LoopEventOutbox.event_type == "RoundTerminated",
                LoopEventOutbox.idempotency_key == f"round-terminated:{round_row.round_id}",
                LoopEventOutbox.payload["round_id"].astext == round_row.round_id,
                LoopEventOutbox.payload["authority_committed"].as_boolean().is_(False))))
        identities["decision_unstable"] = (decision.decision_id if decision else round_row.round_id,) if (
            (not authorized and not preauthorization_termination)
            or (authorized and decision.status in {"pending", "publishing", "publishing_run", "adopting"})) else ()
        identities["failed_authorizations"] = (decision.decision_id,) if authorized and decision.status in {"rejected", "superseded"} else ()
        failed = await self._publication_effects(session, decision if authorized else None, identities)
        if identities["failed_authorizations"]:
            failed += ("failed_authorizations",)
        counts = {kind: len(values) for kind, values in identities.items()}
        return RoundConsequences(tuple(kind for kind, count in counts.items() if count and kind not in failed),
            counts, identities, failed, ("missing_patrol_decision",) if not authorized and not preauthorization_termination else ())

    async def read_loop(self, session, loop_id, *, exclude_undecided_round_id=None):
        rows = tuple(await session.scalars(select(LoopRound).where(LoopRound.loop_id == loop_id).order_by(LoopRound.number)))
        identities, failed, diagnostics = {}, [], []
        for row in rows:
            state = await self.read(session, row)
            for kind, values in state.identities.items():
                if kind == "decision_unstable" and row.round_id == exclude_undecided_round_id and not row.decision_id:
                    continue
                identities[kind] = tuple(sorted(set(identities.get(kind, ())) | set(values)))
            failed.extend(state.failed)
            diagnostics.extend(f"{row.round_id}:{value}" for value in state.diagnostics)
        counts = {kind: len(values) for kind, values in identities.items()}
        return RoundConsequences(tuple(kind for kind, count in counts.items() if count and kind not in failed),
            counts, identities, tuple(sorted(set(failed))), tuple(diagnostics))

    @staticmethod
    async def _publication_effects(session, decision, identities):
        actions = tuple((await session.scalars(select(LoopAction).where(
            LoopAction.decision_id == decision.decision_id))).all()) if decision else ()
        portfolio_ids = {row.result["portfolio_revision_id"] for row in actions if row.result.get("portfolio_revision_id")}
        portfolios = tuple((await session.scalars(select(PortfolioRevision).where(
            PortfolioRevision.portfolio_revision_id.in_(portfolio_ids)))).all()) if portfolio_ids else ()
        observed = {row.portfolio_revision_id for row in portfolios}
        identities["portfolio_publications"] = tuple(sorted(portfolio_ids - observed | {
            row.portfolio_revision_id for row in portfolios if row.status not in {"published", "error", "superseded", "waiting_user", "degraded"}}))
        identities["failed_publications"] = tuple(sorted(row.portfolio_revision_id for row in portfolios
            if row.status in {"error", "superseded", "waiting_user", "degraded"}))
        adoption_ids = {row.action_id for row in actions if row.action_type == "adopt_workspace_result"}
        adoptions = tuple((await session.scalars(select(WorkspaceAdoption).where(
            WorkspaceAdoption.adoption_id.in_(adoption_ids)))).all()) if adoption_ids else ()
        observed_adoptions = {row.adoption_id for row in adoptions}
        identities["workspace_adoptions"] = tuple(sorted(adoption_ids - observed_adoptions | {
            row.adoption_id for row in adoptions if row.status in {"pending", "adopting"}}))
        repairable = {row.action_id for row in actions if row.result.get("repair_available") is True}
        identities["failed_adoptions"] = tuple(sorted(row.adoption_id for row in adoptions
            if row.status in {"conflict", "waiting_user", "rejected"}
            and not (row.adoption_id in repairable and not row.application_intent
                     and (row.evidence or {}).get("repair_context") and row.conflict.get("kind") == "code_overlap")))
        return tuple(kind for kind in ("failed_publications", "failed_adoptions") if identities[kind])
