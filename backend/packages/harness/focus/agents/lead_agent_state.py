"""本文件对外提供 LeadAgentState 类与 artifacts 字段的 reducer。

对外提供:
    LeadAgentState — lead_agent 子图的 LangGraph State schema，继承 AgentState 并扩展业务字段
    merge_artifacts — artifacts 字段的 reducer，只增不减、去重保序
    execution_items — 唯一 typed 历史，messages 为其可验证 LangGraph 兼容投影
    world_state_snapshot / request_manifest — 随精确 checkpoint 提交的运行基线与 sampling 来源
    inbox_delivery — 同 checkpoint 保存的待确认消息身份，供崩溃后补交耐久投递 receipt

输入为:
    AgentState: langchain 提供的 agent 基础状态 schema
    existing / new: list[str] | None — artifacts 字段的既有值与新增值

输出为:
    LeadAgentState 类供 create_agent() 的 state_schema 参数使用
    merge_artifacts → list[str]，两参数皆为 None 时返回空列表

具体工作流为:
    (1) LeadAgentState 只声明 agent 运行时需要的字段，字段语义写在字段名上；
        本地 Agent 不存在隔离标识，故不再保留空的隔离状态槽位
    (2) merge_artifacts 对并行写入做并集去重，避免后写覆盖先写

示例:
    graph = create_agent(model, tools, state_schema=LeadAgentState)
"""

from langchain.agents.middleware.types import AgentState
from typing_extensions import Annotated, NotRequired
from focus.history.bridge import replace_execution_items


def merge_artifacts(existing: list[str] | None, new: list[str] | None) -> list[str]:
    if existing is None and new is None:
        return []
    if existing is None:
        return new
    if new is None:
        return existing
    return list(dict.fromkeys(existing + new))


class LeadAgentState(AgentState):
    title: NotRequired[str | None]
    artifacts: Annotated[list[str], merge_artifacts]
    task_contract: NotRequired[str | None]
    execution_items: Annotated[list[dict] | None, replace_execution_items]
    world_state_snapshot: NotRequired[dict | None]
    request_manifest: NotRequired[dict | None]
    inbox_delivery: NotRequired[dict | None]
