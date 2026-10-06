"""本文件对外提供 RunOwnershipPolicy 的受理与实时控制校验。

输入为事务、持久或待受理 Run；输出为经验证的 Loop owner 或明确拒绝。
具体工作流为锁定控制版本、校验权威 Directive 或用户 intent 来源，保存授权快照；父链由准入层独立校验。
实时检查使用保存的授权版本；暂停、撤权及新 Mission 都使旧执行失效，不凭 task_id 推断历史归属。
示例：await RunOwnershipPolicy().admit(session, run) 在 durable admission 事务内调用。
类型 direct_message 禁止仅凭 user_intent_id 受理裸 Run，必须同时有当前已授权 Directive 和一致的用户来源。
"""

from datetime import UTC, datetime

from sqlalchemy import select

from backend.app.desktop.models import DesktopRun


class RunOwnershipPolicy:
    async def admit(self, session, run):
        from backend.app.desktop.agent_loop.models import LoopDirective, LoopUserIntent

        if run.loop_id is None:
            if run.round_id is not None:
                raise ValueError("Run Round 缺少 Loop")
            return None
        loop, grant = await self._current(session, run)
        if run.directive_id:
            source = await session.get(LoopDirective, run.directive_id)
            if source is None or (source.loop_id, source.round_id) != (run.loop_id, run.round_id):
                raise ValueError("Run Directive 来源不匹配")
            if source.target_context_id != run.task_id or source.goal_revision != loop.goal_revision:
                raise ValueError("Run Directive Context 或 Mission 来源不匹配")
            if source.lifecycle_state not in {"authorized", "delivering", "delivered", "run_started"}:
                raise ValueError("Run Directive 已终止")
            if source.origin_kind == "direct_user":
                user = await session.get(LoopUserIntent, source.correlation_id)
                if (user is None or user.intent_kind != "direct_message" or run.user_intent_id != user.intent_id
                        or user.loop_id != run.loop_id or user.target_context_id != run.task_id
                        or user.delivery_state not in {"observed", "delivered"}
                        or user.resulting_run_id not in {None, run.run_id}):
                    raise ValueError("Run 用户消息交付身份不匹配")
        elif run.user_intent_id:
            source = await session.get(LoopUserIntent, run.user_intent_id)
            if source is None or source.loop_id != run.loop_id:
                raise ValueError("Run 用户 intent 来源不匹配")
            if source.intent_kind == "direct_message":
                raise ValueError("Loop 用户消息必须经过冻结和 Patrol Directive 授权")
        else:
            raise ValueError("Loop Run 缺少权威来源，须经 Context/Kernel 派生")
        if not set((run.equipment or {}).get("permissions") or ()).issubset(set(grant.permission_scope)):
            raise ValueError("Run 装备超出当前授权")
        run.equipment = {**(run.equipment or {}), "_loop_authority_revision": loop.authority_revision,
                         "_loop_goal_revision": loop.goal_revision}
        return loop

    async def assert_live(self, session, run):
        if run.loop_id is None or run.round_id is None:
            return
        loop, grant = await self._current(session, run)
        equipment = run.equipment or {}
        if (equipment.get("_loop_authority_revision") != loop.authority_revision
                or equipment.get("_loop_goal_revision") != loop.goal_revision):
            raise ValueError("Run 控制版本已失效")
        if run.status not in {"pending", "running"}:
            raise ValueError("Run 已终止")
        if not set(equipment.get("permissions") or ()).issubset(set(grant.permission_scope)):
            raise ValueError("Run 权限已撤回")

    @staticmethod
    async def _current(session, run):
        from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant

        loop = await session.get(AgentLoop, run.loop_id, with_for_update=True)
        if loop is None or loop.status != "running" or loop.current_round_id != run.round_id:
            raise ValueError("Run Loop/Round 控制已失效")
        grant = await session.scalar(select(LoopDelegationGrant).where(
            LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.revision == loop.authority_revision,
        ))
        if grant is None or grant.status != "active" or (grant.expires_at and grant.expires_at <= datetime.now(UTC)):
            raise ValueError("Run grant 已失效")
        return loop, grant
