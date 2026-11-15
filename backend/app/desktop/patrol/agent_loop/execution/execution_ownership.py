"""本文件对外提供 RunOwnershipPolicy 的受理与实时控制校验。

输入为事务、持久或待受理 Run；输出为经验证的 Loop owner 或明确拒绝。
具体工作流为锁定控制版本、校验权威 Directive 或用户 intent 来源，保存授权快照；父链由准入层独立校验。
实时检查使用所属 Round 的成功决策与授权版本；暂停、撤权和明确取消使执行失效，新 Mission 不撤销旧轮权限。
示例：await RunOwnershipPolicy().admit(session, run) 在 durable admission 事务内调用。
新 Run 缺少本轮授权快照时仅从服务端已验证的 Directive/Decision 补齐，已有快照仍按原值校验，不修正伪造或失效绑定。
"""

from datetime import UTC, datetime

from sqlalchemy import select



class RunOwnershipPolicy:
    async def admit(self, session, run):
        from backend.app.desktop.patrol.agent_loop.models import LoopDirective, LoopUserIntent, LoopRound

        if run.loop_id is None:
            if run.round_id is not None:
                raise ValueError("Run Round 缺少 Loop")
            if run.kind == "main":
                from backend.app.desktop.patrol.agent_loop.inputs.context_main_guard import ensure_context_main_input_allowed

                await ensure_context_main_input_allowed(session, run.task_id, lock=True)
            return None
        loop, grant = await self._current(session, run)
        round_row = await session.get(LoopRound, run.round_id)
        if run.directive_id:
            source = await session.get(LoopDirective, run.directive_id)
            if source is None or (source.loop_id, source.round_id) != (run.loop_id, run.round_id):
                raise ValueError("Run Directive 来源不匹配")
            if source.target_context_id != run.task_id or source.goal_revision != round_row.goal_revision:
                raise ValueError("Run Directive Context 或 Mission 来源不匹配")
            if source.lifecycle_state not in {"authorized", "delivering", "delivered", "run_started"}:
                raise ValueError("Run Directive 已终止")
        elif run.user_intent_id:
            source = await session.get(LoopUserIntent, run.user_intent_id)
            if source is None or source.loop_id != run.loop_id:
                raise ValueError("Run 用户 intent 来源不匹配")
            if source.intent_kind != "workspace_input":
                raise ValueError("Loop Run 只能关联工作区输入")
        else:
            raise ValueError("Loop Run 缺少权威来源，须经 Context/Kernel 派生")
        if not set((run.equipment or {}).get("permissions") or ()).issubset(set(grant.permission_scope)):
            raise ValueError("Run 装备超出当前授权")
        equipment = dict(run.equipment or {})
        if run.directive_id and "round_authorization" not in equipment:
            from backend.app.desktop.patrol.agent_loop.execution.directive_equipment import resolve_directive_equipment

            authorized = await resolve_directive_equipment(session, loop, source)
            equipment["round_authorization"] = authorized["round_authorization"]
        run.equipment = {**equipment, "_loop_authority_revision": loop.authority_revision,
                         "_loop_goal_revision": round_row.goal_revision}
        await self._permission(session, run)
        return loop

    async def assert_live(self, session, run):
        if run.loop_id is None or run.round_id is None:
            return
        loop, grant = await self._current(session, run)
        from backend.app.desktop.patrol.agent_loop.models import LoopRound, LoopDirective

        round_row = await session.get(LoopRound, run.round_id)
        equipment = run.equipment or {}
        if (equipment.get("_loop_authority_revision") != loop.authority_revision
                or equipment.get("_loop_goal_revision") != round_row.goal_revision):
            raise ValueError("Run 控制版本已失效")
        if run.status not in {"pending", "running"}:
            raise ValueError("Run 已终止")
        if not set(equipment.get("permissions") or ()).issubset(set(grant.permission_scope)):
            raise ValueError("Run 权限已撤回")
        if run.directive_id:
            directive = await session.get(LoopDirective, run.directive_id)
            if directive is None or directive.lifecycle_state not in {"authorized", "delivering", "delivered", "run_started"}:
                raise ValueError("Run Directive 已撤销")
        await self._permission(session, run)

    @staticmethod
    async def _permission(session, run):
        from backend.app.desktop.patrol.agent_loop.execution.directive_equipment import round_permission_state

        state = await round_permission_state(session, run)
        if state is not None and (not state.get("valid") or state.get("access_mode") != (run.equipment or {}).get("access_mode")):
            raise ValueError("Run 本轮文件权限已失效或不匹配")

    @staticmethod
    async def _current(session, run):
        from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopDelegationGrant, LoopRound, LoopDecision

        loop = await session.get(AgentLoop, run.loop_id, with_for_update=True)
        round_row = await session.get(LoopRound, run.round_id)
        if (loop is None or loop.interaction_mode != "workspace_patrol" or loop.status != "running" or round_row is None
            or round_row.loop_id != loop.loop_id or round_row.status in {"settled", "error", "superseded"}):
            raise ValueError("Run Loop/Round 控制已失效")
        if run.directive_id:
            decision = await session.get(LoopDecision, round_row.decision_id) if round_row.decision_id else None
            if decision is None or decision.authority_committed_at is None:
                raise ValueError("Run 所属 Round 未成功授权")
        grant = await session.scalar(select(LoopDelegationGrant).where(
            LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.revision == loop.authority_revision,
        ))
        if grant is None or grant.status != "active" or (grant.expires_at and grant.expires_at <= datetime.now(UTC)):
            raise ValueError("Run grant 已失效")
        return loop, grant
