"""本文件对外提供 LeadAgentState 类与 artifacts 字段的 reducer。

对外提供:
    LeadAgentState — lead_agent 子图的 LangGraph State schema，继承 AgentState 并扩展业务字段
    SandboxState — 沙箱绑定状态
    merge_artifacts — artifacts 字段的 reducer，只增不减、去重保序

输入:
    AgentState: langchain 提供的 agent 基础状态 schema
    existing / new: list[str] | None — artifacts 字段的既有值与新增值

输出:
    LeadAgentState 类供 create_agent() 的 state_schema 参数使用
    merge_artifacts → list[str]，两参数皆为 None 时返回空列表

具体工作流:
    (1) LeadAgentState 只声明 agent 运行时需要的字段，字段语义写在字段名上
    (2) merge_artifacts 对并行写入做并集去重，避免后写覆盖先写

示例:
    graph = create_agent(model, tools, state_schema=LeadAgentState)
"""

from typing import TypedDict

from langchain.agents.middleware.types import AgentState
from typing_extensions import Annotated, NotRequired


class SandboxState(TypedDict):
    sandbox_id: NotRequired[str | None]


def merge_artifacts(existing: list[str] | None, new: list[str] | None) -> list[str]:
    if existing is None and new is None:
        return []
    if existing is None:
        return new
    if new is None:
        return existing
    return list(dict.fromkeys(existing + new))


class LeadAgentState(AgentState):
    sandbox: NotRequired[SandboxState | None]
    title: NotRequired[str | None]
    artifacts: Annotated[list[str], merge_artifacts]
    # 承诺层：人工确认并审核通过的最终任务合同（commitment-layer 交接写入）
    task_contract: NotRequired[str | None]
