r"""本文件对外提供 DelegatedAuthorityGuard 与 AuthorityViolation。

输入为当前 Loop、grant、Patrol intent、时间与 action；输出为已验证的唯一 delegated authority 或
结构化拒绝。具体工作流为依次核对 holder、版本、有效期、capability、Context、permission 和 gate
范围，无副作用的结构化 expansion decline 只保留审计而不要求 mutation capability；用户无需竞争，因为用户覆盖先推进所有控制 revision。
示例：`guard.validate(loop, grant, intent)`。
"""

from __future__ import annotations

from datetime import UTC, datetime

from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant
from backend.app.desktop.agent_loop.schemas import PatrolDecisionIntent


class AuthorityViolation(RuntimeError):
    pass


class DelegatedAuthorityGuard:
    def validate(
        self,
        loop: AgentLoop,
        grant: LoopDelegationGrant | None,
        intent: PatrolDecisionIntent,
    ) -> None:
        if loop.status != "running":
            raise AuthorityViolation(f"Loop 当前状态不接受 Patrol decision: {loop.status}")
        if grant is None or grant.status != "active":
            raise AuthorityViolation("delegation grant 不存在或已撤销")
        if grant.expires_at is not None and grant.expires_at <= datetime.now(UTC):
            raise AuthorityViolation("delegation grant 已过期")
        if loop.holder_id != intent.holder_id or grant.holder_id != intent.holder_id:
            raise AuthorityViolation("decision holder 不是当前唯一 authority holder")
        if loop.authority_revision != intent.grant_revision or grant.revision != intent.grant_revision:
            raise AuthorityViolation("authority revision 已变化")
        if loop.goal_revision != intent.goal_revision:
            raise AuthorityViolation("goal revision 已变化")
        allowed = set(grant.capabilities)
        for action in intent.actions:
            if action.action == "decline_expansion":
                continue
            if action.action not in allowed:
                raise AuthorityViolation(f"grant 不允许 action: {action.action}")
            context_id = getattr(action, "context_id", None)
            if context_id is not None and context_id not in set(grant.context_scope):
                raise AuthorityViolation(f"Context 不在 delegation scope: {context_id}")
