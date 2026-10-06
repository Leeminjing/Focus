"""本文件对外提供 LoopAccountingQuery 的事务、初始证据与消费分层读模型。

输入为只读事务及 Loop identity；输出为实际 Round、legacy 轮、结算待清理数量、预留和未知消费、问候 baseline。
具体工作流为仅连接可信 Run/attempt/activation 及冻结任务记忆 work 身份读取事实，初始 Run 计数缺失时回读其精确 audit，
区分已报告实际消费、未报告预留和失去活动 owner 的未知消费；不按 task_id 猜测历史后代，不修改冻结历史或预算余额。
示例：view = await LoopAccountingQuery().read(session, loop.loop_id)，同一快照可用于 API 与 Live overlay。
round_history 只读保存各轮冻结身份与实际执行是否缺少 Patrol Decision；待观察轮没有执行时不误报历史违约。
"""

from sqlalchemy import func, select

from backend.app.desktop.agent_loop.activation_models import LoopActivation
from backend.app.desktop.agent_loop.models import AgentLoop, LoopRound, LoopBudgetUsage, LoopPatrolAttempt, LoopWorkerRequest
from backend.app.desktop.agent_loop.context_expansion.models import LoopIndexBudgetReservation
from backend.app.desktop.models import DesktopRun, ModelAttemptAudit
from backend.app.desktop.agent_loop.task_progress.models import LoopProgressWork


class LoopAccountingQuery:
    async def read(self, session, loop_id):
        rounds = tuple((await session.scalars(select(LoopRound).where(LoopRound.loop_id == loop_id))).all())
        legacy = [row for row in rounds if row.status == "settled" and not row.observation_id and not row.decision_id]
        completed = [row for row in rounds if row.status == "settled" and row.decision_id and row.observation_id]
        activation = await session.scalar(select(LoopActivation).where(LoopActivation.loop_id == loop_id))
        initial = await session.get(DesktopRun, activation.selected_run_id) if activation else None
        attempts = tuple((await session.scalars(select(ModelAttemptAudit).join(DesktopRun,
            DesktopRun.run_id == ModelAttemptAudit.run_id).where(DesktopRun.loop_id == loop_id,
                DesktopRun.round_id.is_not(None), DesktopRun.run_id != initial.run_id if initial else True))).all())
        reservations = [row.usage_accounting for row in attempts if (row.usage_accounting or {}).get("state") == "reserved"]
        unknown = [row for row in attempts if (row.usage_accounting or {}).get("state") == "unknown" or not row.usage_accounting]
        unsettled = int(await session.scalar(select(func.count()).select_from(DesktopRun).where(
            DesktopRun.loop_id == loop_id, DesktopRun.round_id.is_not(None), DesktopRun.settled_at.is_(None),
            DesktopRun.status.not_in(("pending", "running")),
        )) or 0)
        index_pending = tuple((await session.scalars(select(LoopIndexBudgetReservation).where(
            LoopIndexBudgetReservation.loop_id == loop_id, LoopIndexBudgetReservation.settled_at.is_(None)))).all())
        loop = await session.get(AgentLoop, loop_id)
        active_owners = await self._active_model_owners(session, loop)
        index_unknown = [row for row in index_pending if (row.actual_usage or {}).get("receipt_state") == "unknown"
            or ((row.actual_usage or {}).get("owner_kind"), (row.actual_usage or {}).get("owner_id")) not in active_owners]
        memory_work = tuple((await session.scalars(select(LoopProgressWork).where(LoopProgressWork.loop_id == loop_id))).all())
        memory_unreported = [{"model_calls": int(item.get("reserved_calls") or 0),
            "input_tokens": int(item.get("estimated_input_tokens") or 0),
            "output_tokens": int(item.get("reserved_output_tokens") or 0),
            "unknown": bool(item.get("accounted")) or work.state != "claimed" or item.get("fence") != work.fence}
            for work in memory_work for item in work.usage if not item.get("usage_reported")]
        task_audits = tuple((await session.execute(select(ModelAttemptAudit.status, DesktopRun.run_id, DesktopRun.loop_id).join(
            DesktopRun, DesktopRun.run_id == ModelAttemptAudit.run_id).where(
                DesktopRun.task_id == initial.task_id if initial else False))).all())
        baseline_attempts = [item for item in task_audits if item.run_id == initial.run_id] if initial else []
        usage = await session.get(LoopBudgetUsage, loop_id)
        baseline = await self._initial_evidence(session, initial)
        return {"completed_rounds": len(completed), "legacy_rounds": len(legacy),
            "round_history": await self._round_history(session, rounds),
            "started_rounds": len(rounds) - len(legacy), "cleanup_pending_runs": unsettled,
            "reserved_model_calls": len(reservations) + sum(row.model_calls for row in index_pending) + sum(item["model_calls"] for item in memory_unreported),
            "reserved_input_tokens": sum(int(item.get("input_tokens") or 0) for item in reservations) + sum(row.input_tokens for row in index_pending) + sum(item["input_tokens"] for item in memory_unreported),
            "reserved_output_tokens": sum(int(item.get("output_tokens") or 0) for item in reservations) + sum(row.output_tokens for row in index_pending) + sum(item["output_tokens"] for item in memory_unreported),
            "unknown_model_attempts": len(unknown) + sum(row.model_calls for row in index_unknown) + sum(item["model_calls"] for item in memory_unreported if item["unknown"]), "initial_evidence": baseline,
            "consumption": self._consumption(usage, attempts, index_pending, memory_unreported),
            "historical_reconciliation": {"task_completed_attempts": sum(item.status == "completed" for item in task_audits),
                "baseline_completed_attempts": sum(item.status == "completed" for item in baseline_attempts),
                "owned_completed_attempts": sum(item.status == "completed" for item in attempts),
                "unattributed_task_attempts": sum(item.loop_id is None for item in task_audits),
                "ledger_model_calls": usage.model_calls if usage else 0,
                "owned_audit_model_calls": len(attempts)}}

    @staticmethod
    async def _round_history(session, rounds):
        executed = set(await session.scalars(select(DesktopRun.round_id).where(
            DesktopRun.round_id.in_([row.round_id for row in rounds])).distinct())) if rounds else set()
        return [{"round_id": row.round_id, "number": row.number, "status": row.status,
            "observation_id": row.observation_id, "decision_id": row.decision_id,
            "completed_transaction": row.status == "settled" and bool(row.observation_id and row.decision_id),
            "diagnostics": ["missing_patrol_decision"] if row.round_id in executed and not row.decision_id else []}
            for row in sorted(rounds, key=lambda item: item.number)]

    @staticmethod
    async def _initial_evidence(session, initial):
        if initial is None:
            return None
        audits = tuple((await session.scalars(select(ModelAttemptAudit).where(
            ModelAttemptAudit.run_id == initial.run_id))).all())
        reported = [row for row in audits if row.usage is not None]
        measured = {field: sum(int((row.usage or {}).get(field) or 0) for row in reported)
            for field in ("input_tokens", "output_tokens")}
        return {"run_id": initial.run_id, "status": initial.status,
            "model_calls": max(initial.model_call_count, len(audits)),
            "input_tokens": max(initial.prompt_input_tokens, measured["input_tokens"]),
            "output_tokens": max(initial.prompt_output_tokens, measured["output_tokens"]),
            "model_attempts": len(audits), "unknown_model_attempts": len(audits) - len(reported),
            "round_id": None, "source": "run_and_exact_audits"}

    @staticmethod
    async def _active_model_owners(session, loop):
        if loop is None or loop.status != "running":
            return set()
        owners = set()
        for kind, model, identity in (("patrol", LoopPatrolAttempt, LoopPatrolAttempt.patrol_attempt_id),
                ("worker", LoopWorkerRequest, LoopWorkerRequest.worker_request_id)):
            ids = await session.scalars(select(identity).where(model.loop_id == loop.loop_id,
                model.round_id == loop.current_round_id, model.status == "running"))
            owners.update((kind, value) for value in ids)
        current = await session.get(LoopRound, loop.current_round_id) if loop.current_round_id else None
        if current is not None and current.observation_id and current.status not in {"settled", "error", "superseded"}:
            owners.add(("round", current.round_id))
        return owners

    @staticmethod
    def _consumption(usage, attempts, index_pending, memory_unreported=()):
        fields = ("model_calls", "input_tokens", "output_tokens")
        unreported = [row.usage_accounting for row in attempts
            if (row.usage_accounting or {}).get("state") in {"reserved", "unknown"}]
        ledger = {name: int(getattr(usage, name) or 0) if usage else 0 for name in fields}
        run_reserves = {name: sum(int(item.get(name) or 0) for item in (*unreported, *memory_unreported)) for name in fields}
        index_reserves = {name: sum(getattr(row, name) for row in index_pending) for name in fields}
        return {"actual": {name: max(0, ledger[name] - run_reserves[name]) for name in fields},
            "unreported_reservations": {name: run_reserves[name] + index_reserves[name] for name in fields},
            "occupied": {name: ledger[name] + index_reserves[name] for name in fields}}
