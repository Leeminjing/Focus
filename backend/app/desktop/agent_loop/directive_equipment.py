"""本文件对外提供 resolve_context_equipment 与 resolve_directive_equipment 的正式角色装备解析。

输入为事务、当前 Loop、目标 Context/已授权 Directive 与真实显式 override；输出为完整模型/技能装备及实际角色能力。
具体工作流为读取该 Context 的当前 membership 与 Lane workspace_mode，沿用纯装备政策；初始 primary 未指定角色缩权时继承授权装备，
派生 Lane 未指定模式时默认只读，显式 read_only 在任何角色上强制只有 read 和只读文件模式；提示词与 danger-full-access 均不能扩张角色能力。
示例：equipment = await resolve_directive_equipment(session, loop, directive)。
"""

from sqlalchemy import select

from backend.app.desktop.agent_loop.models import LoopContextMembership, LoopDelegationGrant
from backend.app.desktop.context_curation.models import CurationLane
from backend.app.desktop.equipment_policy import effective_equipment
from focus.security.policy import AccessMode


async def resolve_context_equipment(session, loop, context_id, *, overrides=None):
    membership = await session.scalar(select(LoopContextMembership).where(
        LoopContextMembership.loop_id == loop.loop_id,
        LoopContextMembership.context_id == context_id,
        LoopContextMembership.status.in_(("active", "paused"))))
    if membership is None:
        raise ValueError("目标 Context 缺少当前 Loop membership")
    lane = await session.get(CurationLane, membership.lane_id) if membership and membership.lane_id else None
    workspace_mode = (lane.lane_policy or {}).get("workspace_mode") if lane is not None else None
    read_only = lane is not None and (workspace_mode == "read_only" or
        (workspace_mode is None and membership.role != "primary"))
    source = effective_equipment(loop.equipment or {}, read_only=read_only)
    grant = await session.scalar(select(LoopDelegationGrant).where(
        LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.status == "active"))
    if grant is None:
        raise ValueError("Context 缺少当前有效授权")
    source["permissions"] = [item for item in source["permissions"] if item in grant.permission_scope]
    resolved = effective_equipment(source, overrides=overrides, read_only=read_only)
    modes = {str(AccessMode.READ_ONLY): 0, str(AccessMode.WORKSPACE_WRITE): 1,
             str(AccessMode.DANGER_FULL_ACCESS): 2}
    if modes[resolved["access_mode"]] > modes[source["access_mode"]]:
        raise ValueError("文件模式超出可信 Context 装备")
    return resolved


async def resolve_directive_equipment(session, loop, directive):
    overrides = None
    if directive.origin_kind == "direct_user":
        from backend.app.desktop.agent_loop.models import LoopUserIntent

        intent = await session.get(LoopUserIntent, directive.correlation_id)
        if intent is None or intent.intent_kind != "direct_message":
            raise ValueError("用户 Directive 缺少原始请求")
        overrides = intent.request_payload.get("request") or {}
    return await resolve_context_equipment(session, loop, directive.target_context_id, overrides=overrides)
