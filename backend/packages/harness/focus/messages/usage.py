"""本文件对外提供消息列表的 token 用量估算，作为压缩触发判定与上下文窗口校验的唯一口径来源。

对外提供:
    estimate_raw_tokens — 纯文本启发式：CJK 记 1、其余非空白字符除 4、每条消息加 12
    estimate_image_tokens — 单张图片按像素尺寸折算的 token 数
    estimate_images_tokens — 消息列表里全部图片的折算 token 数
    estimate_messages_tokens — 消息列表整体 token 数（文本口径 + 图片口径）

输入:
    raw: str — 已拼接的原始文本；message_count: int — 消息条数
    width: int / height: int — 图片像素宽高
    messages: list[Any] — dict 或 langchain BaseMessage 的消息列表

输出:
    int — 估算的 token 数，仅用于触发压缩与界面展示，不追求与具体 provider 精确一致

具体工作流:
    (1) 文本口径沿用既有启发式，保证纯文本会话的估算结果与引入图片计量之前逐位一致
    (2) 图片口径委托 focus.images.measure_image_tokens（像素维度折算，与编码字节数无关）
    (3) 内联载荷本身不计入文本口径：编码体积与等长文本的用量必须可区分
    (4) 载荷缺失、无法解析或尺寸非正时按文档化的名义尺寸折算，保证图片永不被计为零

示例:
    estimate_messages_tokens(state["messages"])
    estimate_image_tokens(3840, 2160) → 长边归一至送模上限后按 patch 网格折算
"""

import base64
from typing import Any

from focus.images import image_dimensions, measure_image_tokens
from focus.messages.blocks import content_text, image_blocks, image_inline_payload

_NOMINAL_IMAGE_EDGE_PX = 1024
"""无法解析真实尺寸时的名义边长；保证图片贡献恒大于零。"""

_TOKENS_PER_MESSAGE = 12


def estimate_raw_tokens(raw: str, message_count: int) -> int:
    cjk = sum(1 for char in raw if "㐀" <= char <= "鿿")
    other = sum(
        1 for char in raw if not char.isspace() and not ("㐀" <= char <= "鿿")
    )
    return cjk + (other + 3) // 4 + _TOKENS_PER_MESSAGE * (message_count + 2)


def estimate_image_tokens(width: int, height: int) -> int:
    if width <= 0 or height <= 0:
        return _nominal_image_tokens()
    return measure_image_tokens(width, height)


def estimate_images_tokens(messages: list[Any]) -> int:
    total = 0
    for message in messages:
        for block in image_blocks(message):
            total += _block_image_tokens(block)
    return total


def estimate_messages_tokens(messages: list[Any]) -> int:
    raw = "\n".join(content_text(message) for message in messages)
    return estimate_raw_tokens(raw, len(messages)) + estimate_images_tokens(messages)


def _nominal_image_tokens() -> int:
    return measure_image_tokens(_NOMINAL_IMAGE_EDGE_PX, _NOMINAL_IMAGE_EDGE_PX)


def _block_image_tokens(block: Any) -> int:
    inline = image_inline_payload(block)
    dimensions = _image_dimensions(inline[1]) if inline else None
    if dimensions is None:
        return _nominal_image_tokens()
    return estimate_image_tokens(*dimensions)


def _image_dimensions(payload_b64: str) -> tuple[int, int] | None:
    try:
        return image_dimensions(base64.b64decode(payload_b64, validate=False))
    except Exception:
        return None
