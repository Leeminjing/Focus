"""本文件对外提供 MustViewImagesMiddleware 与必需材料清单的上下文契约。

语义:（材料 × 轮次）的「本轮必须看」——用户每轮共同勾选哪些图片是本轮的必要输入。
中间件保证这些图片出现在本轮每一次模型请求中，使「必须看」成为运行层面的硬约束，
而不是一句提示词；缺图时以可诊断失败收场，绝不在没看到的情况下静默结束。

对外提供:
    MUST_VIEW_CONTEXT_KEY — run 上下文里承载必需材料清单的键名
    MustViewMaterialUnavailable — 必需材料不可读时抛出的失败类型（携带材料标识）
    MustViewImagesMiddleware — 在模型调用前把必需图片注入请求的中间件
    build_must_view_middleware — 按必需材料清单构造中间件

输入:
    runtime.context["workspace"]: str — 工作区路径，材料相对路径的解析根
    runtime.context[MUST_VIEW_CONTEXT_KEY]: list[dict] — 每项含 material_id 与 relative_path
    request.messages: list — 当前对话状态的消息（中间件在请求层追加，不回写状态）

输出:
    每次模型调用前追加一条携带图像内容块的 human 消息（追加发生在 request 层，
    不进入 graph state，因此压缩层在结构上无法吞掉像素）

具体工作流:
    (1) 清单只从 runtime.context 读取，不扫消息推断——因此与压缩门的装配顺序无关，
        也保证承载引用的消息被压缩摘要掉之后，必需项依然存在
    (2) 逐项按工作区解析路径并读取原图字节
    (3) 经 focus.images.scale_for_model 等比缩小为送模尺寸，组装 data URL 图像内容块
    (4) 全量注入：每次请求都带齐全部必需图片，这正是「本轮必须看」的语义
    (5) 任一必需材料不可读即抛 MustViewMaterialUnavailable，使本轮以可诊断失败结束

示例:
    在 backend/app/desktop/service.py 的 additional_middlewares 中与压缩门并列装配，仅主 Agent 生效。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from focus.images import scale_for_model, to_data_url

MUST_VIEW_CONTEXT_KEY = "must_view_materials"

_INJECTION_PREAMBLE = "以下是本轮必须查看的图片材料："


class MustViewMaterialUnavailable(RuntimeError):
    """必需的图片材料不可读；失败信息指明是哪一份材料。"""

    def __init__(self, material_id: str, relative_path: str, reason: str) -> None:
        self.material_id = material_id
        self.relative_path = relative_path
        super().__init__(
            f"本轮必须查看的图片材料不可读: {relative_path} (material_id={material_id})，{reason}"
        )


class MustViewImagesMiddleware(AgentMiddleware):
    """把 run 作用域的必需图片注入每一次模型请求。"""

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return handler(self._inject(request))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        return await handler(self._inject(request))

    def _inject(self, request: Any) -> Any:
        materials = _must_view_materials(request)
        if not materials:
            return request
        workspace = _workspace_of(request)
        content: list[dict[str, Any]] = [{"type": "text", "text": _INJECTION_PREAMBLE}]
        for material in materials:
            content.append(_image_block(workspace, material))
        return request.override(messages=[*request.messages, HumanMessage(content=content)])


def build_must_view_middleware() -> MustViewImagesMiddleware:
    return MustViewImagesMiddleware()


def _must_view_materials(request: Any) -> list[dict[str, Any]]:
    context = _context_of(request)
    materials = context.get(MUST_VIEW_CONTEXT_KEY) if isinstance(context, dict) else None
    if not isinstance(materials, list):
        return []
    return [item for item in materials if isinstance(item, dict) and item.get("relative_path")]


def _context_of(request: Any) -> Any:
    runtime = getattr(request, "runtime", None)
    context = getattr(runtime, "context", None)
    return context


def _workspace_of(request: Any) -> Path:
    context = _context_of(request)
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
