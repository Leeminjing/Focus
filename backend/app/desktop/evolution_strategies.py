"""本文件对外提供 EvolutionStrategy、freeze_strategies 和策略认知约定。

输入为三段策略正文或当前事务与工作区身份；输出为校验后的正文及带内容 hash 的冻结条目。
工作流在既有记忆库筛选启用且范围匹配的策略，完整投影，不维护执行状态或授予权限。
示例：snapshot = await freeze_strategies(session, workspace_id)。
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import or_, select

from focus.history import content_hash
from backend.app.desktop.models import Memory


class EvolutionStrategy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    applicability: str = Field(min_length=1, max_length=12000)
    evolution: str = Field(min_length=1, max_length=12000)
    inheritance: str = Field(min_length=1, max_length=12000)

    @field_validator("applicability", "evolution", "inheritance")
    @classmethod
    def _nonblank(cls, value):
        if not value.strip():
            raise ValueError("策略正文不能为空白")
        return value


EVOLUTION_GUIDANCE = """evolution_strategies 是用户配置的演化参考，不授予权限或替代当前要求。
结合冻结用户意见、Mission 和真实 Context 结果判断适用性，在 rationale 说明采用策略的 memory_id/hash 与理由。
不需要新 Context 时继续已有 Context，work_specs 可以为空；不要为了策略名称制造线程。
采用诊断—重构时，先建议原 Context 分析用户不满的原因，准确传达反馈及所引用文件版本；
诊断未完成或未发布时，不生成声称基于诊断的重做候选。诊断发布后再引用有效 Mission 和真实消息证据。
被否定方案只按失败证据的必要范围保留，不能当成有效计划；诊断推测不自动升级为已验证事实。
讨论不能强制升级为 Mission 决定。候选、权限、执行容量与工作区规则仍由既有合同约束。
"""


async def freeze_strategies(session, workspace_id):
    rows = (await session.scalars(select(Memory).where(
        Memory.category == "evolution_strategy", Memory.enabled.is_(True),
        or_(Memory.workspace_id.is_(None), Memory.workspace_id == workspace_id),
    ).order_by(Memory.memory_id))).all()
    result = []
    for row in rows:
        body = {"memory_id": row.memory_id, "title": row.title, "workspace_id": row.workspace_id,
                "strategy": EvolutionStrategy.model_validate(row.strategy).model_dump(mode="json")}
        result.append({**body, "content_hash": content_hash(body)})
    return tuple(result)
