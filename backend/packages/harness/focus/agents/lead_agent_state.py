"""
本文件对外提供 LeadAgentState 类及两个辅助 TypedDict 和两个自定义 reducer。

LeadAgentState: lead_agent 子图的 LangGraph State schema，继承自 AgentState，扩展 4 个业务字段
SandboxState: 沙箱绑定状态
ViewedImageData: 已查看图片数据

merge_artifacts: artifacts 字段的 reducer，只增不减，去重保序
merge_viewed_images: viewed_images 字段的 reducer，累积/覆盖/清空

输入: 无 — 本文件为纯定义文件，不包含函数入口
输出: LeadAgentState 类及辅助类型供 create_agent() 的 state_schema 参数使用


示例:
    from focus.agents.lead_agent_state import LeadAgentState, merge_artifacts, merge_viewed_images
    graph = create_agent(model, tools, state_schema=LeadAgentState)
"""

from typing import TypedDict

from langchain.agents.middleware.types import AgentState
from typing_extensions import Annotated, NotRequired


class SandboxState(TypedDict):
    sandbox_id: NotRequired[str | None]


class ViewedImageData(TypedDict):
    base64: str
    mime_type: str


def merge_artifacts(existing: list[str] | None, new: list[str] | None) -> list[str]:
    if existing is None and new is None:
        return []
    if existing is None:
        return new
    if new is None:
        return existing
    return list(dict.fromkeys(existing + new))


def merge_viewed_images(
    existing: dict[str, ViewedImageData] | None,
    new: dict[str, ViewedImageData] | None,
) -> dict[str, ViewedImageData]:
    if existing is None and new is None:
        return {}
    if existing is None:
        return new
    if new is None:
        return existing
    if not new:
        return {}
    return {**existing, **new}


class LeadAgentState(AgentState):
    sandbox: NotRequired[SandboxState | None]
    title: NotRequired[str | None]
    artifacts: Annotated[list[str], merge_artifacts]
    viewed_images: Annotated[dict[str, ViewedImageData], merge_viewed_images]
    # 承诺层：人工确认并审核通过的最终任务合同（commitment-layer 交接写入）
    task_contract: NotRequired[str | None]
