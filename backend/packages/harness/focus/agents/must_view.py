"""本文件对外提供必需图片的注入中间件、逐图表态的输出结构与相关失败类型。

语义:（材料 × 轮次）的「本轮必须看」——用户每轮勾选哪些图片是本轮的必要输入。
中间件保证这些图片出现在本轮每一次模型请求中，并要求模型在结构化输出里对每张图逐条表态；
表态未齐或声明"未读到"时不以成功状态收场。

对外提供:
    MUST_VIEW_CONTEXT_KEY — run 上下文里承载必需材料清单的键名
    MODEL_IMAGE_INPUT_KEY — run 上下文里承载本轮模型是否具备图像输入能力的键名
    MustViewImageReport / MustViewReports — 逐图表态的输出结构
    MustViewMaterialUnavailable — 必需材料不可读时抛出的失败类型（携带材料标识）
    MustViewModelCannotReadImages — 本轮模型不具备图像输入能力时抛出的失败类型
    MustViewImagesMiddleware — 注入必需图片并校验逐图表态的中间件
    build_must_view_middleware — 构造该中间件

输入:
    runtime.context["workspace"]: str — 工作区路径，材料相对路径的解析根
    runtime.context[MUST_VIEW_CONTEXT_KEY]: list[dict] — 每项含 material_id 与 relative_path
    runtime.context[MODEL_IMAGE_INPUT_KEY]: bool — 本轮模型是否声明具备图像输入能力
    request.messages / state["structured_response"] — 当前对话与模型的结构化表态

输出:
    每次模型调用前追加一条携带图像内容块的 human 消息（只在 request 层，不进 state）；
    after_model 阶段校验表态完整性，未齐则把控制流送回模型节点，超限或声明未读到则升级为人工介入

具体工作流:
    (1) 清单只从 runtime.context 读取，不扫消息推断——因此与压缩门的装配顺序无关，
        也保证承载引用的消息被压缩摘要掉之后，必需项依然存在
    (2) 注入前先确认本轮模型声明了图像输入能力：未声明时图片不可能被处理，
        该轮必须以可诊断失败收场，而不是照样成功结束
    (3) 逐项按工作区解析路径、读取原图字节，经 focus.images.scale_for_model 缩小后组装图像内容块
    (4) 全量注入：每次请求都带齐全部必需图片，这正是「本轮必须看」的语义
    (5) 任一必需材料不可读即抛 MustViewMaterialUnavailable，使本轮以可诊断失败结束
    (6) after_model 校验表态：把 structured_response 里的表态与必需清单比对，
        缺项则拒绝结束并送回模型补齐（有界）；超限或出现"未读到"则 interrupt 交人工处置
    (7) 表态是模型的声明，其真伪不可验证；本文件不据此断言图片确已被读取

示例:
    Middleware 在 backend/app/desktop/service.py 的 additional_middlewares 中与压缩门并列装配，仅主 Agent 生效。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import HumanMessage
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from focus.images import scale_for_model, to_data_url

MUST_VIEW_CONTEXT_KEY = "must_view_materials"

MODEL_IMAGE_INPUT_KEY = "model_supports_image_input"
"""本轮所用模型是否声明具备图像输入能力；由 service 侧按模型条目写入 run 上下文。"""

_INJECTION_PREAMBLE = "以下是本轮必须查看的图片材料："

_REMINDER_MARKER = "[focus-must-view]"

_MAX_REMINDERS = 2
"""允许拒绝结束并催促补齐的次数上限；超过即交人工处置，避免无限循环。"""


class MustViewImageReport(BaseModel):
    material_id: str = Field(description="本轮必须查看的图片材料标识")
    read: bool = Field(description="是否成功读到该图片的内容；成功读到即置为真")


class MustViewReports(BaseModel):
    images: list[MustViewImageReport] = Field(
        description="对本轮每一张必须查看的图片各给一条表态"
    )


class MustViewMaterialUnavailable(RuntimeError):
    """必需的图片材料不可读；失败信息指明是哪一份材料。"""

    def __init__(self, material_id: str, relative_path: str, reason: str) -> None:
        self.material_id = material_id
        self.relative_path = relative_path
        super().__init__(
            f"本轮必须查看的图片材料不可读: {relative_path} (material_id={material_id})，{reason}"
        )


class MustViewModelCannotReadImages(RuntimeError):
    """本轮模型不具备图像输入能力，必需图片不可能被处理；该轮不得以成功状态结束。"""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        super().__init__(
            f"本轮存在必须查看的图片，但模型 '{model_name}' 未声明具备图像输入能力，"
            "图片无法被处理；请在模型条目声明 supports_image_input 或改用具备图像输入的模型"
        )


class MustViewImagesMiddleware(AgentMiddleware):
    """注入 run 作用域的必需图片，并校验模型的逐图表态。"""

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return handler(self._inject(request))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        return await handler(self._inject(request))

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        materials = _must_view_materials_from_context(_context_of_runtime(runtime))
        if not materials:
            return None
        reports = _reports_of(state)
        missing = [item for item in materials if item["material_id"] not in reports]
        unread = [
            item
            for item in materials
            if reports.get(item["material_id"]) is False
        ]
        if unread:
            return _escalate(missing, unread)
        if not missing:
            return None
        if _reminder_count(state) >= _MAX_REMINDERS:
            return _escalate(missing, [])
        return {
            "jump_to": "model",
            "messages": [HumanMessage(content=_reminder_text(missing))],
        }

    def _inject(self, request: Any) -> Any:
        context = _context_of(request)
        materials = _must_view_materials_from_context(context)
        if not materials:
            return request
        _require_image_capable_model(context)
        workspace = _workspace_of(context)
        content: list[dict[str, Any]] = [{"type": "text", "text": _INJECTION_PREAMBLE}]
        for material in materials:
            content.append(_image_block(workspace, material))
        return request.override(messages=[*request.messages, HumanMessage(content=content)])


def build_must_view_middleware() -> MustViewImagesMiddleware:
    return MustViewImagesMiddleware()


def _reports_of(state: Any) -> dict[str, bool]:
    reports = state.get("structured_response") if isinstance(state, dict) else None
    images = getattr(reports, "images", None)
    if not isinstance(images, list):
        return {}
    return {
        str(item.material_id): bool(item.read)
        for item in images
        if getattr(item, "material_id", None)
    }


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


def _reminder_text(missing: list[dict[str, Any]]) -> str:
    lines = "\n".join(
        f"- {item['relative_path']} (material_id={item['material_id']})" for item in missing
    )
    return (
        f"{_REMINDER_MARKER} 还有以下必须查看的图片没有表态，本轮不能结束。"
        f"请在结构化输出中为它们各补一条：\n{lines}"
    )


def _escalate(
    missing: list[dict[str, Any]], unread: list[dict[str, Any]]
) -> None:
    interrupt(
        {
            "type": "must_view_report",
            "missing": [item["material_id"] for item in missing],
            "unread": [item["material_id"] for item in unread],
        }
    )
    return None


def _must_view_materials_from_context(context: Any) -> list[dict[str, Any]]:
    materials = context.get(MUST_VIEW_CONTEXT_KEY) if isinstance(context, dict) else None
    if not isinstance(materials, list):
        return []
    return [item for item in materials if isinstance(item, dict) and item.get("relative_path")]


def _context_of(request: Any) -> Any:
    return _context_of_runtime(getattr(request, "runtime", None))


def _context_of_runtime(runtime: Any) -> Any:
    return getattr(runtime, "context", None)


def _require_image_capable_model(context: Any) -> None:
    if isinstance(context, dict) and context.get(MODEL_IMAGE_INPUT_KEY) is True:
        return
    model_name = context.get("model_name") if isinstance(context, dict) else None
    raise MustViewModelCannotReadImages(str(model_name or "未知模型"))


def _workspace_of(context: Any) -> Path:
    workspace = context.get("workspace") if isinstance(context, dict) else None
    if not workspace:
        raise MustViewMaterialUnavailable("-", "-", "缺少工作区上下文")
    return Path(str(workspace)).resolve()


def _image_block(workspace: Path, material: dict[str, Any]) -> dict[str, Any]:
    material_id = str(material.get("material_id") or "-")
    relative_path = str(material["relative_path"])
    path = Path(workspace, *Path(relative_path).parts)
    try:
        original = path.read_bytes()
    except OSError as error:
        raise MustViewMaterialUnavailable(material_id, relative_path, str(error)) from error
    if not original:
        raise MustViewMaterialUnavailable(material_id, relative_path, "材料内容为空")
    mime, scaled = scale_for_model(original)
    return {"type": "image_url", "image_url": {"url": to_data_url(mime, scaled)}}
