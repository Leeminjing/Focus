"""本文件对外提供 ModelUsageReceipts 的 Run 模型预算预留及幂等结算。

输入为当前事务、持久 ModelAttemptAudit、可信 Run 来源及预估/实际 tokens；输出为该审计内的 receipt 与共享 LoopBudgetUsage 更新。
具体工作流为锁控制及共享预算，采样前预留一次调用和输入估算；完成时用实际值替换预留，未知消费保留占用。
重复 attempt 不重复收费，初始问候仅属 baseline；本模块复用 LoopUsageLedger，不维护第二套余额，并同事务发布独立消费读模型，不推进控制版本。
示例：await receipts.reserve(session, audit, input_tokens=256)；await receipts.settle(session, audit)。
"""

from backend.app.desktop.models import DesktopRun


class ModelUsageReceipts:
    async def reserve(self, session, audit, *, input_tokens=0, output_tokens=0):
        from backend.app.desktop.agent_loop.execution_ownership import RunOwnershipPolicy
        from backend.app.desktop.agent_loop.budgets import LoopBudgetGuard
        from backend.app.desktop.agent_loop.models import LoopBudgetUsage, LoopDelegationGrant
        from backend.app.desktop.agent_loop.usage import LoopUsageDelta, LoopUsageLedger
        from sqlalchemy import select

        run = await session.get(DesktopRun, audit.run_id)
        if run is None:
            raise ValueError("模型尝试缺少持久 Run")
        await RunOwnershipPolicy().assert_live(session, run)
        if run.loop_id is None or run.round_id is None:
            return
        if audit.usage_accounting:
            return
        grant = await session.scalar(select(LoopDelegationGrant).where(
            LoopDelegationGrant.loop_id == run.loop_id, LoopDelegationGrant.status == "active",
        ).order_by(LoopDelegationGrant.revision.desc()).limit(1))
        usage = await session.get(LoopBudgetUsage, run.loop_id, with_for_update=True)
        if usage is None or grant is None:
            raise ValueError("模型预算来源不存在")
        projected = {"model_calls": 1, "input_tokens": max(0, input_tokens), "output_tokens": max(0, output_tokens)}
        current = {name: getattr(usage, name) for name in ("model_calls", "input_tokens", "output_tokens", "rounds", "retries")}
        from backend.app.desktop.agent_loop.context_expansion.models import LoopIndexBudgetReservation
        from sqlalchemy import func

        pending = (await session.execute(select(*(func.coalesce(func.sum(getattr(LoopIndexBudgetReservation, name)), 0)
            for name in ("model_calls", "input_tokens", "output_tokens"))).where(
                LoopIndexBudgetReservation.loop_id == run.loop_id, LoopIndexBudgetReservation.settled_at.is_(None)))).one()
        for name, value in zip(("model_calls", "input_tokens", "output_tokens"), pending, strict=True):
            current[name] += int(value)
        decision = LoopBudgetGuard().evaluate(current, grant.budgets, "model_attempt", projected)
        if decision.status == "exhausted":
            raise ValueError("模型预算已耗尽: " + ",".join(decision.reasons))
        LoopUsageLedger.apply(usage, LoopUsageDelta(**projected))
        audit.usage_accounting = {"loop_id": run.loop_id, "round_id": run.round_id,
                                  "model_calls": 1, "input_tokens": projected["input_tokens"],
                                  "output_tokens": projected["output_tokens"], "state": "reserved"}
        from backend.app.desktop.agent_loop.accounting_events import LoopAccountingEventRecorder

        await LoopAccountingEventRecorder().record(session, run.loop_id,
            source_kind="run_attempt", source_id=audit.attempt_id, transition="reserved")

    async def settle(self, session, audit):
        from backend.app.desktop.agent_loop.models import LoopBudgetUsage

        receipt = audit.usage_accounting or {}
        if not receipt or receipt.get("state") != "reserved":
            return
        usage = await session.get(LoopBudgetUsage, receipt["loop_id"], with_for_update=True)
        if usage is None:
            raise ValueError("模型消费 receipt 缺少预算行")
        actual = audit.usage
        if actual is None:
            audit.usage_accounting = {**receipt, "state": "unknown"}
        else:
            measured = {name: max(0, int(actual.get(name) or 0)) for name in ("input_tokens", "output_tokens")}
            for name, value in measured.items():
                setattr(usage, name, max(0, getattr(usage, name) + value - receipt[name]))
            audit.usage_accounting = {**receipt, **measured, "state": "actual"}
        from backend.app.desktop.agent_loop.accounting_events import LoopAccountingEventRecorder

        await LoopAccountingEventRecorder().record(session, receipt["loop_id"],
            source_kind="run_attempt", source_id=audit.attempt_id, transition=audit.usage_accounting["state"])
