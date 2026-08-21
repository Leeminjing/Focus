"""spatial-patrol 插件的空间服务:载体解析、坐标换算、从锚点向外观察、观察工具构建。

对外提供:
    ObservationService — 观察服务(路径校验 / PDF 扩张取文 / 图片裁剪视觉描述)
    build_observation_tools(service) — observe_anchor / expand_observation 两个工具

输入:
    plugin_config: dict — config.json 原样内容(observe_radius_start/growth/max_radius)
    vision_model: BaseChatModel | None — 视觉模型(插件接入时已校验,可为 None 仅测试)

观察语义(f18-spatial-patrol spec):
    PDF: pypdfium2 get_text_bounded 以锚点为中心逐圈取文字(纯文本,text-only 模型可读);
    图片: Pillow 按锚点裁剪,视觉模型描述。半径 r 为相对页/图尺寸的归一化值。
"""

import base64
import inspect
import io
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from focus.config import get_app_config

logger = logging.getLogger(__name__)

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
_TEXT_SUFFIXES = {".docx", ".doc", ".md", ".txt"}
_TEXT_SPATIAL_SUFFIXES = {".docx", ".doc"}  # 仅 docx/doc 提供空间能力(md/txt 纯预览)

_service: "ObservationService | None" = None


@lru_cache(maxsize=16)
def _render_pdf_page_cached(
    path_text: str, mtime_ns: int, page_number: int, scale: float,
) -> bytes:
    """按文件版本和页号缓存 PDF PNG；mtime 变化时自然产生新缓存键。"""
    del mtime_ns
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(path_text) as document:
        page = document[page_number - 1]
        bitmap = page.render(scale=scale)
        try:
            image = bitmap.to_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
        finally:
            bitmap.close()


def _main_model_text_only() -> bool:
    """主模型是否 text-only;配置不可得时按 text-only 保守处理。

    ponytail: 以 models[0].model 的 deepseek 前缀判定(DeepSeek V4 text-only,
    官方文档确认);未来接入多厂商时再在模型条目加显式 multimodal 标记。
    """
    try:
        app_config = get_app_config("config.yaml")
    except Exception:
        return True
    if not app_config.models:
        return True
    return str(app_config.models[0].model or "").startswith("deepseek")


def init_service(plugin_config: dict, registry: Any = None) -> "ObservationService":
    """插件接入时初始化共享观察服务。

    失效判定:主模型为 text-only 且无 vision 条目且无视觉插件(service.vision)
    → 抛 RuntimeError(loader 标记插件 Unavailable,提醒用户)。
    """
    global _service
    from plugins.spatial_patrol.vision import resolve_vision_model

    vision = None
    try:
        vision = resolve_vision_model(plugin_config)
    except RuntimeError:
        vision = None
    if vision is None and _main_model_text_only():
        providers = (
            registry.declared_providers("service.vision")
            if registry is not None and hasattr(registry, "declared_providers")
            else []
        )
        if not providers:
            raise RuntimeError(
                "纯文本模型且无视觉插件,请启用视觉插件或配置 vision 模型"
            )
    _service = ObservationService(plugin_config, vision, registry)
    return _service


def get_service() -> "ObservationService":
    """获取已初始化的观察服务(entry 与 routes 共享同一实例)。"""
    if _service is None:
        raise RuntimeError("空间观察服务未初始化")
    return _service


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class ObservationService:
    """从空间锚点向外观察:载体解析 + 坐标换算 + 内容取样。"""

    def __init__(
        self,
        plugin_config: dict,
        vision_model: BaseChatModel | None,
        registry: Any = None,
    ) -> None:
        self._config = plugin_config or {}
        self._vision = vision_model
        self._registry = registry
        self._radius_start = float(self._config.get("observe_radius_start", 0.05))
        self._radius_growth = float(self._config.get("observe_radius_growth", 2.0))
        self._radius_max = float(self._config.get("max_radius", 0.5))
        self._radius_by_run: dict[str, float] = {}

    # === 路径与载体 ===

    @staticmethod
    def resolve_path(workspace: str, content_ref: str) -> Path:
        """解析载体路径并执行工作区 containment 校验(越界抛 ValueError)。"""
        root = Path(workspace).resolve()
        target = (root / content_ref).resolve() if not Path(content_ref).is_absolute() else Path(content_ref).resolve()
        if target != root and root not in target.parents:
            raise ValueError("载体路径不属于当前工作区")
        return target

    def is_viewable(self, content_ref: str) -> bool:
        suffix = Path(content_ref).suffix.lower()
        return suffix == ".pdf" or suffix in _IMAGE_SUFFIXES or suffix in _TEXT_SUFFIXES

    def supports_spatial(self, content_ref: str) -> bool:
        """是否可建立空间锚点/投放:图像页载体 + docx/doc 文本载体。"""
        suffix = Path(content_ref).suffix.lower()
        return suffix == ".pdf" or suffix in _IMAGE_SUFFIXES or suffix in _TEXT_SPATIAL_SUFFIXES

    @staticmethod
    def extract_text(path: Path) -> str:
        """按扩展名提取文本(docx/doc 经 focus/readers,md/txt 直接读取)。"""
        suffix = path.suffix.lower()
        if suffix == ".docx":
            from focus.readers import _read_docx

            return _read_docx(str(path))
        if suffix == ".doc":
            from focus.readers import _read_doc

            return _read_doc(str(path))
        if suffix in (".md", ".txt"):
            return path.read_text(encoding="utf-8", errors="replace")
        raise ValueError(f"不支持的文本类型: {suffix}")

    @staticmethod
    def _data_uri(image: Any) -> str:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode()}"

    # === PDF ===

    @staticmethod
    def pdf_page_size(pdf_path: Path, page_number: int) -> tuple[float, float]:
        import pypdfium2 as pdfium

        with pdfium.PdfDocument(str(pdf_path)) as document:
            page = document[page_number - 1]
            return page.get_size()

    @staticmethod
    def pdf_page_count(pdf_path: Path) -> int:
        import pypdfium2 as pdfium

        with pdfium.PdfDocument(str(pdf_path)) as document:
            return len(document)

    @staticmethod
    def pdf_page_png(pdf_path: Path, page_number: int, scale: float = 2.0) -> bytes:
        """渲染 PDF 页为 PNG 字节(供前端查看器与观察裁剪)。"""
        resolved = pdf_path.resolve()
        return _render_pdf_page_cached(
            str(resolved), resolved.stat().st_mtime_ns, page_number, float(scale),
        )

    def pdf_text_around(
        self, pdf_path: Path, page_number: int, x: float, y: float, radius: float
    ) -> str:
        """以锚点为中心取 PDF 页矩形区域文字(锚点 y 为顶部向下归一化,pdfium 为底部向上)。"""
        import pypdfium2 as pdfium

        with pdfium.PdfDocument(str(pdf_path)) as document:
            page = document[page_number - 1]
            width, height = page.get_size()
            textpage = page.get_textpage()
            try:
                anchor_x = x * width
                anchor_y_top = y * height
                half_w = radius * width
                half_h = radius * height
                left = _clamp(anchor_x - half_w, 0.0, width)
                right = _clamp(anchor_x + half_w, 0.0, width)
                bottom = _clamp((height - anchor_y_top) - half_h, 0.0, height)
                top = _clamp((height - anchor_y_top) + half_h, 0.0, height)
                text = textpage.get_text_bounded(left=left, bottom=bottom, right=right, top=top)
                return (text or "").strip()
            finally:
                textpage.close()

    # === 图片 ===

    async def image_around(
        self, image_path: Path, x: float, y: float, radius: float
    ) -> str:
        """裁剪锚点附近图像并描述:vision 条目优先,缺省经视觉插件 service.vision。"""
        from PIL import Image

        with Image.open(image_path) as image:
            width, height = image.size
            anchor_x = x * width
            anchor_y = y * height
            half = radius * max(width, height)
            box = (
                int(_clamp(anchor_x - half, 0, width)),
                int(_clamp(anchor_y - half, 0, height)),
                int(_clamp(anchor_x + half, 0, width)),
                int(_clamp(anchor_y + half, 0, height)),
            )
            cropped = image.crop(box)
        data_url = self._data_uri(cropped)
        if self._vision is not None:
            message = HumanMessage(
                content=[
                    {
                        "type": "text",
                        "text": (
                            "描述图像中锚点标记附近的内容:有哪些对象、文字、布局关系。"
                            "只描述可见内容,不要猜测图像外的东西。"
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]
            )
            response = await self._vision.ainvoke([message])
            content = response.content
            if isinstance(content, list):
                content = "".join(
                    item if isinstance(item, str) else str(item.get("text", ""))
                    for item in content
                )
            return str(content).strip()
        return await self._describe_via_service(data_url)

    async def _describe_via_service(self, data_url: str) -> str:
        """经视觉插件的 service.vision 实现描述(约定: async describe(data_url) -> str)。"""
        if self._registry is None or not hasattr(self._registry, "service"):
            return "[视觉能力不可用:未配置 vision 模型且无视觉插件]"
        impls = self._registry.service("service.vision")
        if not impls:
            return "[视觉能力不可用:无视觉插件实现]"
        describe = getattr(impls[0], "describe", None)
        if not callable(describe):
            return "[视觉插件实现缺少 describe 方法]"
        result = describe(data_url)
        if inspect.isawaitable(result):
            result = await result
        return str(result).strip()

    # === 观察入口 ===

    async def observe(
        self, workspace: str, content_ref: str, page: int, x: float, y: float, radius: float
    ) -> str:
        """给定锚点与半径,返回该圈内容文本(供工具与预览接口共用)。"""
        path = self.resolve_path(workspace, content_ref)
        if not path.is_file():
            raise ValueError(f"载体文件不存在: {content_ref}")
        radius = _clamp(radius, 0.001, self._radius_max)
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            text = self.pdf_text_around(path, page, x, y, radius)
            if text:
                return (
                    f"[第 {page} 页,锚点 ({x:.3f}, {y:.3f}),半径 {radius:.3f};"
                    f"相对方向=锚点四周,相对距离=0..{radius:.3f} 页尺寸] 附近文字:\n{text}"
                )
            return f"[第 {page} 页,锚点 ({x:.3f}, {y:.3f}),半径 {radius:.3f}] 该范围内没有文字内容"
        if suffix in _IMAGE_SUFFIXES:
            description = await self.image_around(path, x, y, radius)
            return (
                f"[图片,锚点 ({x:.3f}, {y:.3f}),半径 {radius:.3f}] 附近内容描述:\n{description}"
            )
        if suffix in _TEXT_SPATIAL_SUFFIXES:
            text = self.extract_text(path)
            if not text:
                return f"[{suffix},锚点 ({x:.3f}, {y:.3f})] 文本为空"
            pos = int(_clamp(y, 0.0, 1.0) * len(text))
            window = max(1, int(radius * len(text)))
            start = max(0, pos - window)
            end = min(len(text), pos + window)
            snippet = text[start:end]
            return (
                f"[{suffix},锚点字符位置 {pos}/{len(text)},半径 {radius:.3f}] 附近文本:\n"
                f"…{snippet}…\n(锚点位于该窗口中部)"
            )
        raise ValueError(f"不支持的载体类型: {suffix}")

    @property
    def radius_start(self) -> float:
        return self._radius_start

    @property
    def radius_growth(self) -> float:
        return self._radius_growth

    def remember_radius(self, run_id: str, radius: float) -> float:
        value = _clamp(radius, 0.001, self._radius_max)
        self._radius_by_run[run_id] = value
        return value

    def next_radius(self, run_id: str) -> float:
        current = self._radius_by_run.get(run_id, self._radius_start)
        return self.remember_radius(run_id, current * self._radius_growth)


def build_observation_tools(service: ObservationService):
    """构建空间观察工具(observe_anchor / expand_observation)。

    锚点信息经 runtime.context(spatial_id/content_ref/page/x/y/workspace)注入,
    与投放时的 langgraph_context 对齐。
    """

    @tool
    async def observe_anchor(radius: float | None = None, runtime: ToolRuntime = None) -> str:
        """从你的空间锚点观察周围内容:返回锚点附近半径内的文字或图像描述。radius 为相对页尺寸的归一化半径(0..1),留空则从初始半径开始。"""
        context = _anchor_context(runtime)
        radius = float(radius) if radius is not None else service.radius_start
        radius = service.remember_radius(str(context.get("run_id") or context["spatial_id"]), radius)
        return await service.observe(
            context["workspace"], context["content_ref"],
            int(context["page"]), float(context["x"]), float(context["y"]), radius,
        )

    @tool
    async def expand_observation(radius: float | None = None, runtime: ToolRuntime = None) -> str:
        """扩大观察半径再看一圈:radius 为相对页尺寸的归一化半径(0..1),留空则使用初始半径的下一圈。"""
        context = _anchor_context(runtime)
        run_key = str(context.get("run_id") or context["spatial_id"])
        radius = (
            service.remember_radius(run_key, float(radius))
            if radius is not None else service.next_radius(run_key)
        )
        return await service.observe(
            context["workspace"], context["content_ref"],
            int(context["page"]), float(context["x"]), float(context["y"]), radius,
        )

    return [observe_anchor, expand_observation]


def _anchor_context(runtime: ToolRuntime) -> dict[str, Any]:
    context = runtime.context
    if not isinstance(context, dict):
        raise RuntimeError("缺少空间上下文: runtime.context 必须为 dict")
    missing = [
        key for key in ("spatial_id", "workspace", "content_ref", "page", "x", "y")
        if key not in context
    ]
    if missing:
        raise RuntimeError(f"缺少空间上下文字段: {', '.join(missing)}")
    return context
