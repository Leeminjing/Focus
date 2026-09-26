r"""本文件对外提供 ContextRecoveryStage，将 identity-only Patrol 选择解析为可信的单来源 Lane mutation。

输入为包含公开 recovery opportunities 的冻结 observation 和 PatrolDecisionIntent；输出为保持控制版本不变、仅将合法
recover_context 替换为 CreateLaneAction 的 intent，或明确 waiting reason。具体工作流为校验 identity 属于当前 observation，读取持久
合同并逐字段匹配冻结 authority，再注入服务端保存的 plan；模型提供未知 identity 或任意 plan 均不会进入该接口。示例：
`resolution = await stage.resolve(observation, intent)`。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.context_recovery.contracts import ContextRecoveryResolution
from backend.app.desktop.agent_loop.context_recovery.repository import ContextRecoveryOpportunityRepository
from backend.app.desktop.agent_loop.schemas import CreateLaneAction, LoopObservationEnvelope, PatrolDecisionIntent


class ContextRecoveryStage:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._repository = ContextRecoveryOpportunityRepository()

    async def resolve(
        self,
        observation: LoopObservationEnvelope,
        intent: PatrolDecisionIntent,
    ) -> ContextRecoveryResolution:
        allowed = {
            str(item.get("opportunity_id"))
            for item in observation.recovery_opportunities
            if item.get("opportunity_id")
        }
        resolved = []
        async with self._sessions() as session:
            for action in intent.actions:
                if action.action != "recover_context":
                    resolved.append(action)
                    continue
                if action.opportunity_id not in allowed:
                    return ContextRecoveryResolution(waiting_reason="recover_context 引用了当前 observation 之外的 opportunity")
                opportunity = await self._repository.get_contract(session, action.opportunity_id)
                if opportunity is None or opportunity.status != "pending":
                    return ContextRecoveryResolution(waiting_reason="Context recovery opportunity 不存在或已消费")
                if not self._matches(observation, opportunity):
                    return ContextRecoveryResolution(waiting_reason="Context recovery opportunity 已与冻结 observation 不一致")
                plan = opportunity.plan.model_copy(
                    update={
                        "lane_policy": {
                            **opportunity.plan.lane_policy,
                            "recovery_opportunity_id": opportunity.opportunity_id,
                        }
                    }
                )
                resolved.append(
                    CreateLaneAction(
                        action="create_lane",
                        plan=plan,
                        message="继续完成原 Context 的既定目标；先确认中断工具调用未产生业务结果，再从保留的约束与证据恢复执行。",
                    )
                )
        return ContextRecoveryResolution(intent=intent.model_copy(update={"actions": tuple(resolved)}))

    @staticmethod
    def _matches(observation: LoopObservationEnvelope, opportunity) -> bool:
        workspace_revision = int(observation.workspace.get("revision") or 0)
        grant_id = str(observation.grant.get("grant_id") or "")
        grant_revision = int(observation.grant.get("revision") or 0)
        return (
            opportunity.loop_id == observation.loop_id
            and opportunity.source_frontier_hash == observation.observed_frontier_hash
            and opportunity.goal_revision == observation.goal_revision
            and opportunity.workspace_revision == workspace_revision
            and opportunity.authority_revision == observation.authority_revision
            and opportunity.grant_id == grant_id
            and opportunity.grant_revision == grant_revision
        )
