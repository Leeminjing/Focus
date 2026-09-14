"""本文件对外提供必看图片的逐图声明工具、完成门中间件与兼容入口。

输入为 runtime context 中 RunImageInputs 的 required 子集，以及模型对 report_must_view_images
工具的调用参数；输出为正常放行、回到 model 的有界补报请求，或 type=must_view_report 的人工中断。
具体工作流为：只校验 required 图片，按消息从新到旧取模型最近一次逐图声明，缺项最多提醒两次，
read=false 或提醒耗尽时携带材料与原因中断；本文件不读取或注入像素，read 仅表示模型声明，图片
像素交付由 image_attachment.py 独立负责。

逐图表态刻意用普通工具承载，而不是 provider 结构化输出：后者会让 LangChain 给整轮运行绑定
ToolStrategy 并强制 tool_choice，与 thinking 模型互斥，且把每一轮的输出形式都改成结构化应答。

示例：middleware=build_must_view_completion_middleware(); tools=[report_must_view_images]。
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.types import interrupt
from pydantic import BaseModel, Field, ValidationError

from focus.agents.image_inputs import (
    MODEL_IMAGE_INPUT_KEY,
    RUN_IMAGE_INPUTS_CONTEXT_KEY,
    RunImageInputs,
)

MUST_VIEW_CONTEXT_KEY = "must_view_materials"
_REMINDER_MARKER = "[focus-must-view]"
_MAX_REMINDERS = 2


class MustViewImageReport(BaseModel):
    material_id: str = Field(description="本轮必须查看的图片材料标识")
    read: bool = Field(description="模型是否声明已成功读到该图片内容")


class MustViewReports(BaseModel):
    images: list[MustViewImageReport] = Field(description="本轮每一张必看图片的模型声明")


@tool("report_must_view_images")
def report_must_view_images(images: list[MustViewImageReport]) -> str:
    """声明本轮每张必须查看的图片是否已读到。

    对每一张必看图片各给一条声明：成功读到该图片内容即把 read 置为真。只表态，不读取图片内容。
    """
    return f"已记录 {len(images)} 张必看图片的逐图声明。"


class MustViewCompletionMiddleware(AgentMiddleware):
    @hook_config(can_jump_to=["model", "end"])
    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        required = list(RunImageInputs.from_context(_context_of_runtime(runtime)).required)
        if not required:
            return None
        reports = _reports_of(state)
        missing = [item for item in required if item.material_id not in reports]
        unread = [item for item in required if reports.get(item.material_id) is False]
        if unread:
            return _escalate(missing, unread)
        if not missing:
            return None
        if _reminder_count(state) >= _MAX_REMINDERS:
            return _escalate(missing, ())
        return {
            "jump_to": "model",
            "messages": [HumanMessage(content=_reminder_text(missing))],
        }


def build_must_view_completion_middleware() -> MustViewCompletionMiddleware:
    return MustViewCompletionMiddleware()


def _reports_of(state: Any) -> dict[str, bool]:
    """取模型最近一次逐图表态；只认 AI 消息上的报告工具调用，催促语不算表态。"""
    messages = state.get("messages") if isinstance(state, dict) else None
    if not isinstance(messages, list):
        return {}
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        for call in reversed(list(getattr(message, "tool_calls", None) or [])):
            if call.get("name") != report_must_view_images.name:
                continue
            try:
                reports = MustViewReports.model_validate(call.get("args") or {})
            except ValidationError:
                continue
            return {
                str(item.material_id): bool(item.read)
                for item in reports.images
                if item.material_id
            }
    return {}


def _reminder_count(state: Any) -> int:
    messages = state.get("messages") if isinstance(state, dict) else None
    if not isinstance(messages, list):
        return 0
    return sum(
        1
        for message in messages
        if isinstance(getattr(message, "content", None), str)
        and str(message.content).startswith(_REMINDER_MARKER)
    )


def _reminder_text(missing: list[Any]) -> str:
    lines = "\n".join(
        f"- {item.relative_path} (material_id={item.material_id})" for item in missing
    )
    return (
        f"{_REMINDER_MARKER} 还有以下必须查看的图片没有表态，本轮不能结束。"
        f"请调用 {report_must_view_images.name} 为它们各补一条声明：\n{lines}"
    )


def _escalate(missing: list[Any], unread: Any) -> dict[str, Any] | None:
    unread_items = list(unread)
    decision = interrupt(
        {
            "type": "must_view_report",
            "missing": [item.material_id for item in missing],
            "unread": [item.material_id for item in unread_items],
            "materials": [
                {
                    "material_id": item.material_id,
                    "relative_path": item.relative_path,
                    "reason": "unread" if item in unread_items else "missing",
                }
                for item in [*missing, *unread_items]
            ],
            "actions": ["retry", "cancel"],
        }
    )
    if not isinstance(decision, dict) or decision.get("type") != "must_view_report":
        return None
    if decision.get("decision") == "cancel":
        return {"jump_to": "end"}
    if decision.get("decision") == "retry":
        return {
            "jump_to": "model",
            "messages": [HumanMessage(content=_reminder_text([*missing, *unread_items]))],
        }
    return None


def _context_of_runtime(runtime: Any) -> Any:
    return getattr(runtime, "context", None)


__all__ = [
    "MODEL_IMAGE_INPUT_KEY",
    "MUST_VIEW_CONTEXT_KEY",
    "RUN_IMAGE_INPUTS_CONTEXT_KEY",
    "MustViewCompletionMiddleware",
    "MustViewImageReport",
    "MustViewReports",
    "build_must_view_completion_middleware",
    "report_must_view_images",
]
