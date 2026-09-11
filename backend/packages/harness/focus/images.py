"""本文件对外提供图像事实与送模缩放，与 focus.readers(文档阅读)对称，是图像知识的唯一归属地。

对外提供:
    image_dimensions — 从图像字节解析像素宽高
    image_mime_from_name — 由文件名推断图像 MIME
    is_image_name — 判定文件名是否为受支持的图像
    image_exceeds_original_limit — 判定原图是否超出可接受体积上限
    measure_image_tokens — 按 patch 网格把像素宽高折算为 token 数
    scale_for_model — 把原图等比缩小到送模尺寸上限，返回 (mime, 缩放后字节)
    to_data_url — 组装内联 data URL

输入:
    data: bytes — 图像原始字节
    name: str — 文件名（含扩展名）
    width / height: int — 像素宽高

输出:
    image_dimensions → tuple[int, int] | None（非图像或无法解析返回 None）
    image_mime_from_name / image_mime_of → str | None；is_image_name → bool
    measure_image_tokens → int；scale_for_model → tuple[str, bytes]；to_data_url → str

具体工作流:
    (1) 尺寸与缩放都交给 Pillow；Pillow 在首次真正用到时才导入，
        使纯文本路径不必为图像库付导入成本
    (2) measure_image_tokens 先把长边归一到 IMAGE_MODEL_MAX_EDGE_PX（只缩不放），
        再按 PAGE_PATCH_GRID_PX 的 patch 网格计数：ceil(w/grid) × ceil(h/grid)。
        依据 OpenAI 视觉文档的 patch/tile 计量——图像成本由像素维度决定，与编码字节数无关
    (3) scale_for_model 同样只缩不放，并统一转为 PNG 输出，保证解码方无需猜测格式
    (4) 任何解析失败都返回 None 或原图，绝不抛异常打断运行

示例:
    measure_image_tokens(3840, 2160) → 长边归一至 1568 后按 49×27 个 patch 折算
    mime, data = scale_for_model(original_bytes)
"""

import base64
import io
import math
from pathlib import Path

IMAGE_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
}

PAGE_PATCH_GRID_PX = 32
"""图像计量的 patch 网格边长。"""

IMAGE_MODEL_MAX_EDGE_PX = 1568
"""送入模型的长边上限；超出才缩放，不放大。"""

IMAGE_MODEL_MAX_BYTES = 4 * 1024 * 1024
"""单张图片送入模型的字节上限。"""

ORIGINAL_IMAGE_MAX_BYTES = 20 * 1024 * 1024
"""粘贴原图可接受的字节上限；超出直接拒绝，不落盘。"""

DEFAULT_IMAGE_MIME = "image/png"


def is_image_name(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_MIME_BY_SUFFIX


def image_exceeds_original_limit(name: str, size: int) -> bool:
    return is_image_name(name) and size > ORIGINAL_IMAGE_MAX_BYTES


def image_mime_from_name(name: str) -> str | None:
    return IMAGE_MIME_BY_SUFFIX.get(Path(name).suffix.lower())


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            return int(image.width), int(image.height)
    except Exception:
        return None


def measure_image_tokens(width: int, height: int) -> int:
    if width <= 0 or height <= 0:
        return 0
    longest = max(width, height)
    if longest > IMAGE_MODEL_MAX_EDGE_PX:
        scale = IMAGE_MODEL_MAX_EDGE_PX / longest
        width = max(1, int(width * scale))
        height = max(1, int(height * scale))
    patches = math.ceil(width / PAGE_PATCH_GRID_PX) * math.ceil(height / PAGE_PATCH_GRID_PX)
    return max(1, patches)


def scale_for_model(data: bytes) -> tuple[str, bytes]:
    if len(data) <= IMAGE_MODEL_MAX_BYTES:
        dimensions = image_dimensions(data)
        if dimensions is not None and max(dimensions) <= IMAGE_MODEL_MAX_EDGE_PX:
            return _original_mime(data), data
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            scaled = image.convert("RGB")
            longest = max(scaled.width, scaled.height)
            if longest > IMAGE_MODEL_MAX_EDGE_PX:
                ratio = IMAGE_MODEL_MAX_EDGE_PX / longest
                scaled = scaled.resize(
                    (max(1, int(scaled.width * ratio)), max(1, int(scaled.height * ratio)))
                )
            buffer = io.BytesIO()
            scaled.save(buffer, format="PNG", optimize=True)
        return DEFAULT_IMAGE_MIME, buffer.getvalue()
    except Exception:
        return _original_mime(data), data


def to_data_url(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _original_mime(data: bytes) -> str:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            return str(Image.MIME.get(image.format, DEFAULT_IMAGE_MIME))
    except Exception:
        return DEFAULT_IMAGE_MIME
