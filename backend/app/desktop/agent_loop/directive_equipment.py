"""本文件对外提供 resolve_context_equipment 与 resolve_directive_equipment 的正式角色装备解析。

输入为事务、Loop、目标 Context/已授权 Directive 与本轮权限；输出为完整装备及所属 Round 的授权绑定。
具体工作流为读取该 Context 的当前 membership 与 Lane workspace_mode，沿用纯装备政策；初始 primary 未指定角色缩权时继承授权装备，
派生 Lane 未指定模式时默认只读，显式 read_only 在任何角色上强制只有 read 和只读文件模式；提示词与 danger-full-access 均不能扩张角色能力。
Patrol 必须完整列出可治理 Context 与 action:index 新目标，Kernel 校验上限；执行从已提交 Decision 读取，
下一轮不继承，旧轮不被较新轮覆盖；逐调用仍核对当前 membership、角色与用户授权上限。
示例：equipment = await resolve_directive_equipment(session, loop, directive)。
"""

from sqlalchemy import select, tuple_

from backend.app.desktop.agent_loop.models import LoopAction, LoopContextMembership, LoopDecision, LoopDelegationGrant
from backend.app.desktop.context_curation.models import CurationLane
from backend.app.desktop.equipment_policy import effective_equipment


_MODES = {"read-only": 0, "workspace-write": 1, "danger-full-access": 2}
_DERIVED_ACTIONS = {"create_lane", "update_lane", "merge_contexts", "spawn_context", "recover_context"}


async def validate_context_permissions(session, loop, observation, proposal):
    expected = {row["context_id"] for row in observation.portfolio_frontier}
    derived = {f"action:{index}": action for index, action in enumerate(proposal.actions)
               if action.action in _DERIVED_ACTIONS}
    expected.update(derived)
    entries = proposal.context_permissions
    selected = {entry.context_id: entry.access_mode for entry in entries}
    if len(selected) != len(entries) or set(selected) != expected:
        raise ValueError("本轮 Context 权限清单缺项、重复或引用未声明目标")
    resolved = {}
    for context_id, mode in selected.items():
        if context_id in derived:
            action = derived[context_id]
            plan = getattr(action, "plan", None)
            if plan is None:
                raise ValueError("派生目标必须先解析为可信 Lane plan")
            policy = getattr(plan, "lane_policy", None) or {}
            read_only = policy.get("workspace_mode") != "isolated_write"
            equipment = effective_equipment(loop.equipment or {}, read_only=read_only)
        else:
            equipment = await resolve_context_equipment(session, loop, context_id)
        if mode is not None:
            if _MODES[mode] > _MODES[equipment["access_mode"]]:
                raise ValueError("本轮文件模式超出用户授权或 Context 角色上限")
            equipment = {**equipment, "access_mode": mode}
        resolved[context_id] = {**equipment, "access_mode": mode}
    for action in proposal.actions:
        target = getattr(action, "context_id", None) if action.action == "continue_context" else None
        if action.action == "deliver_user_message":
            from backend.app.desktop.agent_loop.models import LoopUserIntent
            user = await session.get(LoopUserIntent, action.intent_id)
            target = user.target_context_id if user else None
        if target is not None and selected.get(target) is None:
            raise ValueError("执行目标本轮未获授文件权限")
    if any(selected[key] is None for key in derived):
        raise ValueError("新增执行目标本轮未获授文件权限")
    return resolved


async def resolve_context_equipment(session, loop, context_id, *, overrides=None):
    membership = await session.scalar(select(LoopContextMembership).where(
        LoopContextMembership.loop_id == loop.loop_id,
        LoopContextMembership.context_id == context_id,
        LoopContextMembership.status.in_(("active", "paused"))))
    if membership is None:
        raise ValueError("目标 Context 缺少当前 Loop membership")
    lane = await session.get(CurationLane, membership.lane_id) if membership and membership.lane_id else None
    grant = await session.scalar(select(LoopDelegationGrant).where(
        LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.status == "active"))
    return _context_equipment(loop, membership, lane, grant, overrides=overrides)


def _context_equipment(loop, membership, lane, grant, *, overrides=None):
    workspace_mode = (lane.lane_policy or {}).get("workspace_mode") if lane is not None else None
    read_only = lane is not None and (workspace_mode == "read_only" or
        (workspace_mode is None and membership.role != "primary"))
    source = effective_equipment(loop.equipment or {}, read_only=read_only)
    if grant is None:
        raise ValueError("Context 缺少当前有效授权")
    source["permissions"] = [item for item in source["permissions"] if item in grant.permission_scope]
    resolved = effective_equipment(source, overrides=overrides, read_only=read_only)
    if _MODES[resolved["access_mode"]] > _MODES[source["access_mode"]]:
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
    equipment = await resolve_context_equipment(session, loop, directive.target_context_id, overrides=overrides)
    decision = await session.get(LoopDecision, directive.decision_id)
    action = await session.get(LoopAction, directive.action_id)
    if (decision is None or decision.authority_committed_at is None
            or decision.round_id != directive.round_id or decision.loop_id != loop.loop_id
            or action is None or action.decision_id != decision.decision_id):
        raise ValueError("Directive 缺少所属 Round 的成功权威提交")
    identity = f"action:{action.position}" if action and action.action_type in _DERIVED_ACTIONS else directive.target_context_id
    permissions = {row["context_id"]: row["access_mode"] for row in decision.intent.get("context_permissions", ())}
    mode = permissions.get(identity)
    if mode is None or _MODES[mode] > _MODES[equipment["access_mode"]]:
        raise ValueError("Directive 文件模式缺失、失效或超出当前上限")
    return {**equipment, "access_mode": mode, "round_authorization": {
        "loop_id": loop.loop_id, "round_id": directive.round_id, "decision_id": decision.decision_id,
        "context_id": directive.target_context_id, "grant_revision": directive.grant_revision,
        "access_mode": mode, "source": "patrol",
    }}


async def round_permission_state(session, run):
    return (await round_permission_states(session, (run,)))[getattr(run, "run_id", "")]


async def round_permission_states(session, runs):
    """固定批次数读取授权依赖；单 Run 与 Live 快照使用相同的验证政策。"""
    from backend.app.desktop.agent_loop.models import AgentLoop, LoopDirective, LoopRound

    runs = tuple(runs)
    bound = [run for run in runs if (run.equipment or {}).get("round_authorization")]
    result = {getattr(run, "run_id", ""): {"valid": False, "reason": "missing_round_authorization"} for run in runs}
    if not bound:
        return result

    async def rows(model, key, identities):
        identities = {value for value in identities if value is not None}
        return {} if not identities else {getattr(row, key): row for row in await session.scalars(
            select(model).where(getattr(model, key).in_(identities)))}

    loops = await rows(AgentLoop, "loop_id", (run.loop_id for run in bound))
    rounds = await rows(LoopRound, "round_id", (run.round_id for run in bound))
    directives = await rows(LoopDirective, "directive_id", (run.directive_id for run in bound))
    decisions = await rows(LoopDecision, "decision_id", (row.decision_id for row in directives.values()))
    actions = await rows(LoopAction, "action_id", (row.action_id for row in directives.values()))
    grant_keys = {(run.loop_id, run.equipment["round_authorization"].get("grant_revision")) for run in bound}
    grants = {(row.loop_id, row.revision): row for row in await session.scalars(
        select(LoopDelegationGrant).where(tuple_(LoopDelegationGrant.loop_id, LoopDelegationGrant.revision).in_(grant_keys)
            | (LoopDelegationGrant.loop_id.in_(loops) & (LoopDelegationGrant.status == "active"))))}
    active_grants = {row.loop_id: row for row in grants.values() if row.status == "active"}
    context_keys = {(run.loop_id, run.task_id) for run in bound}
    memberships = {(row.loop_id, row.context_id): row for row in await session.scalars(
        select(LoopContextMembership).where(
            tuple_(LoopContextMembership.loop_id, LoopContextMembership.context_id).in_(context_keys),
            LoopContextMembership.status.in_(("active", "paused"))))}
    lanes = await rows(CurationLane, "lane_id", (row.lane_id for row in memberships.values()))
    for run in bound:
        binding = run.equipment["round_authorization"]
        directive = directives.get(run.directive_id)
        membership = memberships.get((run.loop_id, run.task_id))
        result[getattr(run, "run_id", "")] = _round_permission_state(
            run, binding, loops.get(run.loop_id), rounds.get(run.round_id), directive,
            grants.get((run.loop_id, binding.get("grant_revision"))),
            decisions.get(directive.decision_id) if directive else None,
            actions.get(directive.action_id) if directive else None,
            membership, lanes.get(membership.lane_id) if membership else None, active_grants.get(run.loop_id))
    return result


def _round_permission_state(run, binding, loop, round_row, directive, grant, decision, action,
                            membership, lane, active_grant):
    from datetime import UTC, datetime

    identity = f"action:{action.position}" if action and action.action_type in _DERIVED_ACTIONS else run.task_id
    selected = {row["context_id"]: row["access_mode"]
                for row in (decision.intent.get("context_permissions", ()) if decision else ())}
    mode = binding.get("access_mode")
    valid = bool(loop and round_row and grant and directive and loop.status == "running"
        and decision and action and action.decision_id == decision.decision_id
        and decision.authority_committed_at is not None and round_row.decision_id == decision.decision_id
        and round_row.loop_id == loop.loop_id and directive.round_id == round_row.round_id
        and round_row.status not in {"settled", "error", "superseded"}
        and run.status in {"pending", "running"} and run.settled_at is None
        and directive.lifecycle_state in {"authorized", "delivering", "delivered", "run_started"}
        and grant.status == "active" and grant.revision == loop.authority_revision
        and (grant.expires_at is None or grant.expires_at > datetime.now(UTC))
        and set((run.equipment or {}).get("permissions", ())).issubset(grant.permission_scope)
        and mode in _MODES and selected.get(identity) == mode
        and _MODES[mode] <= _MODES.get((loop.equipment or {}).get("access_mode", "read-only"), -1)
        and binding.get("round_id") == run.round_id and binding.get("loop_id") == run.loop_id
        and binding.get("context_id") == run.task_id and binding.get("decision_id") == directive.decision_id)
    if valid and membership is None:
        valid = False
    if valid:
        try:
            current = _context_equipment(loop, membership, lane, active_grant)
            valid = (_MODES[mode] <= _MODES[current["access_mode"]]
                     and set((run.equipment or {}).get("permissions", ())).issubset(current["permissions"]))
        except ValueError:
            valid = False
    reason = None if valid else "migration_permission_conflict" if binding.get("migration_conflict") else "round_authorization_expired"
    return {**binding, "valid": valid, "reason": reason}
