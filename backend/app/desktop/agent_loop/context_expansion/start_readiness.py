"""本文件对外提供 QuestionDisposition、ExecutionReadiness 与冻结能力读取入口。

输入为已有问题身份、调查目标和所需执行权限，以及实际 Loop 装备/Grant；输出为类型化问题处置与平台能力事实。
具体工作流为校验问题身份，按角色缩权和有效授权交集读取能力；研究计划只提出需求，不授予权限或证明结果。
示例：scope = await load_execution_readiness(sessions, observation, work_spec)；scope.validate_plans(plans, questions)。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from backend.app.desktop.agent_loop.context_expansion.contracts import stable_expansion_hash
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant
from backend.app.desktop.equipment_policy import effective_equipment
from backend.app.desktop.agent_loop.mission_contract import ExecutionBoundaries
from backend.app.desktop.agent_loop.mission_projection import EffectiveMissionProjector
from focus.tools.builtins.workspace_tools import select_workspace_tools, TOOL_NAMES_BY_PERMISSION


def work_question_id(question: str) -> str:
    return stable_expansion_hash("work-spec-question", " ".join(question.split()).casefold())


class QuestionDisposition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: Literal["prerequisite", "execution_research"]
    investigation: str = Field(min_length=1, max_length=2000)
    required_capabilities: tuple[Literal["read", "write", "host_command"], ...] = ()

    @model_validator(mode="after")
    def _require_research_path(self):
        if self.disposition == "execution_research" and not self.required_capabilities:
            raise ValueError("执行研究必须声明实际所需能力")
        if len(self.required_capabilities) != len(set(self.required_capabilities)):
            raise ValueError("研究能力不得重复")
        return self


class ExecutionReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str = Field(min_length=1)
    goal_revision: int = Field(gt=0)
    authority_revision: int = Field(gt=0)
    capabilities: tuple[Literal["read", "write", "host_command"], ...]
    workspace_mode: Literal["read_only", "isolated_write"]
    tools: tuple[str, ...] = ()
    mission_boundaries: ExecutionBoundaries | None = None

    def validate_plans(self, plans, questions) -> None:
        known = {work_question_id(q) for q in questions}
        ids = [p.question_id for p in plans]
        if len(ids) != len(set(ids)) or set(ids) - known:
            raise ValueError("问题处置必须绑定唯一的现有问题身份")
        for plan in plans:
            if set(plan.required_capabilities) - set(self.capabilities):
                raise ValueError("研究路径缺少实际获准能力")


async def load_execution_readiness(sessions, observation, work_spec) -> ExecutionReadiness:
    async with sessions() as session:
        loop = await session.get(AgentLoop, observation.loop_id)
        grant = await session.scalar(select(LoopDelegationGrant).where(
            LoopDelegationGrant.loop_id == observation.loop_id,
            LoopDelegationGrant.revision == observation.authority_revision,
            LoopDelegationGrant.status == "active"))
        if (loop is None or grant is None or loop.goal_revision != observation.goal_revision
                or loop.authority_revision != observation.authority_revision
                or loop.current_round_id != observation.round_id or loop.status != "running"):
            raise ValueError("工作开始条件的来源或授权已过期")
        equipment = effective_equipment(loop.equipment or {}, read_only=work_spec.workspace_requirement == "read_only")
        available = set(equipment.get("permissions") or ()) & set(grant.permission_scope)
        tools = tuple(sorted(t.name for t in select_workspace_tools(list(available))))
        available = {p for p in available if set(TOOL_NAMES_BY_PERMISSION.get(p, ())) & set(tools)}
        mission = EffectiveMissionProjector.from_observation(observation)
        return ExecutionReadiness(workspace_id=loop.workspace_id, goal_revision=loop.goal_revision,
            authority_revision=grant.revision, workspace_mode=work_spec.workspace_requirement,
            capabilities=tuple(sorted(available & {"read", "write", "host_command"})), tools=tools,
            mission_boundaries=ExecutionBoundaries.model_validate(mission.boundaries))
