"""按输入身份读取已提交的处理反馈及同次决策证据。

复用 Kernel 的 apply_user_inputs 记录，不修改输入、Mission 或执行状态。
仅读取当前工作区已授权且实际应用的动作；停止请求必须等待执行器结算，
不能将 Run 的 interrupted 或 Worker 的 cancelled 提前解释为清理完成。
"""

from sqlalchemy import func, select

from backend.app.desktop.agent_loop.models import (
    AgentLoop, LoopAction, LoopDecision, LoopDirective, LoopUserIntent, LoopWorkerRequest,
)
from backend.app.desktop.models import DesktopRun


class PatrolInputFeedbackReader:
    async def read(self, session, workspace_id, rows):
        if not rows:
            return {}
        # DISTINCT ON 将每条输入限制为最新一次真实处置，延期后可被再次处理。
        applications = (await session.execute(
            select(LoopUserIntent.intent_id, LoopAction, LoopDecision)
            .select_from(LoopUserIntent)
            .join(LoopAction, LoopAction.result["input_ids"].contains(
                func.jsonb_build_array(LoopUserIntent.intent_id)))
            .join(LoopDecision, LoopDecision.decision_id == LoopAction.decision_id)
            .join(AgentLoop, AgentLoop.loop_id == LoopDecision.loop_id)
            .where(
                LoopUserIntent.intent_id.in_([row.intent_id for row in rows]),
                AgentLoop.workspace_id == workspace_id,
                LoopAction.action_type == "apply_user_inputs",
                LoopAction.status == "applied",
                LoopDecision.authority_committed_at.is_not(None),
            )
            .distinct(LoopUserIntent.intent_id)
            .order_by(LoopUserIntent.intent_id, LoopDecision.authority_committed_at.desc(),
                      LoopDecision.decision_id.desc())
        )).all()
        cancellations = await self._cancellations(
            session, {decision.decision_id for _, _, decision in applications})
        result = {}
        for intent_id, action, decision in applications:
            use = next((item for item in action.payload.get("inputs", ())
                        if item.get("intent_id") == intent_id), None)
            if use is None:
                continue
            result[intent_id] = {
                "disposition": use["disposition"],
                "explanation": use["explanation"],
                "mission_revision": action.result.get("mission_revision"),
                "changes": [item for item in action.payload.get("changes", ())
                            if item.get("intent_id") == intent_id],
                "decision_id": decision.decision_id,
                "loop_id": decision.loop_id,
                "round_id": decision.round_id,
                "observation_id": decision.observation_id,
                "observation_generation": decision.observation_generation,
                "rationale": decision.rationale,
                "evidence": decision.evidence,
                "applied_at": decision.authority_committed_at.isoformat(),
                # 多条输入可能共同参与一次决策，不声称这些动作由单条输入独自产生。
                "related_cancellations": cancellations.get(decision.decision_id, []),
            }
        return result

    async def _cancellations(self, session, decision_ids):
        if not decision_ids:
            return {}
        actions = tuple(await session.scalars(select(LoopAction).where(
            LoopAction.decision_id.in_(decision_ids),
            LoopAction.action_type == "cancel_execution",
            LoopAction.status == "applied",
        ).order_by(LoopAction.decision_id, LoopAction.position)))
        targets = {kind: {action.payload["target_id"] for action in actions
                          if action.payload.get("target_kind") == kind}
                   for kind in ("run", "directive", "worker")}
        directives = {row.directive_id: row for row in await session.scalars(
            select(LoopDirective).where(LoopDirective.directive_id.in_(targets["directive"]))
        )} if targets["directive"] else {}
        run_ids = targets["run"] | {row.launched_run_id for row in directives.values()
                                    if row.launched_run_id}
        runs = {row.run_id: row for row in await session.scalars(
            select(DesktopRun).where(DesktopRun.run_id.in_(run_ids))
        )} if run_ids else {}
        workers = {row.worker_request_id: row for row in await session.scalars(
            select(LoopWorkerRequest).where(LoopWorkerRequest.worker_request_id.in_(targets["worker"]))
        )} if targets["worker"] else {}
        result = {}
        for action in actions:
            payload = action.payload
            kind, identity = payload["target_kind"], payload["target_id"]
            target = {"run": runs, "directive": directives, "worker": workers}[kind].get(identity)
            run = runs.get(target.launched_run_id) if kind == "directive" and target else None
            state = self.cancellation_state(action.result, kind, target, run)
            result.setdefault(action.decision_id, []).append({
                "action_id": action.action_id, "target_kind": kind, "target_id": identity,
                "round_id": payload["round_id"], "reason": payload["reason"], "state": state,
            })
        return result

    @staticmethod
    def cancellation_state(receipt, kind, target, launched_run=None):
        if receipt.get("status") == "already_terminal":
            return "already_terminal"
        if target is None:
            return "unknown"
        if kind == "directive":
            if target.launched_run_id:
                return PatrolInputFeedbackReader.cancellation_state(receipt, "run", launched_run)
            return "stopped" if target.lifecycle_state == "cancelled" else "cancel_requested"
        settled = target.settled_at if kind == "run" else target.completed_at
        if settled is None:
            return "cancel_requested"
        return "stopped" if target.status in {"interrupted", "cancelled"} else "settled"
