"""本文件对外提供消息内容块的形状识别与取值函数，供用量核算、图片注入与文件读取共用。

对外提供:
    message_content — 取出单条消息的 content 字段
    is_image_block — 判定单个内容块是否为图像块
    image_blocks — 取出一条消息里的全部图像块
    image_inline_payload — 提取内联图像块的 (mime, base64) 二元组
    content_text — 把一条消息的 content 归一为拼接文本
    strip_image_payloads — 把内联图像载荷替换为占位符，供需要 JSON 文本口径的调用方复用

输入:
    block: Any — 单个内容块。兼容 OpenAI 兼容形态 {"type": "image_url", "image_url": {"url": ...}}
                 与跨 provider 标准形态 {"type": "image", "base64": ..., "mime_type": ...}
    content: Any — 消息的 content 字段，字符串或内容块列表
    message: Any — 单条消息，dict 或带 content 属性的对象（langchain BaseMessage）

输出:
    message_content → Any；is_image_block → bool；image_blocks → list[dict]
    image_inline_payload → tuple[str, str] | None，非内联载荷返回 None
    content_text → str；strip_image_payloads → list[Any]

具体工作流:
    (1) message_content 先按 dict 取值，再回退到属性读取，两类消息形态归一
    (2) is_image_block 以块的 type 字段判定，同时认 image_url 与 image 两种命名
    (3) image_inline_payload 只接受 data URL 形态；远程 http(s) URL 与缺失载荷返回 None
    (4) content_text 对列表逐块取文本后拼接、对字符串原样返回；非文本块贡献空串，
        以保持纯文本会话的核算口径与引入图片计量之前完全一致
    (5) strip_image_payloads 只重建含图像块的消息，其余原样透传

示例:
    content_text({"content": [{"type": "text", "text": "看这张"}]}) → "看这张"
    image_inline_payload({"type": "image", "base64": "iVBOR", "mime_type": "image/png"}) → ("image/png", "iVBOR")
"""

import re
from typing import Any

_IMAGE_BLOCK_TYPES = frozenset({"image_url", "image"})
_DEFAULT_IMAGE_MIME = "image/png"
_DATA_URL_PATTERN = re.compile(
    r"^data:(?P<mime>[^;,]+)?;base64,(?P<data>.+)$", re.DOTALL | re.IGNORECASE
)

IMAGE_PAYLOAD_PLACEHOLDER = "<image>"
"""内联图像载荷的替换占位符；与 <image> 文本同形的消息在核算时不再重复计入图片用量。"""


def message_content(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("content", "")
    return getattr(message, "content", "")


def is_image_block(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") in _IMAGE_BLOCK_TYPES


def image_blocks(message: Any) -> list[dict]:
    content = message_content(message)
    if not isinstance(content, list):
        return []
    return [block for block in content if is_image_block(block)]


def image_inline_payload(block: Any) -> tuple[str, str] | None:
    if not isinstance(block, dict):
        return None
    if block.get("type") == "image":
        return _standard_payload(block)
    if block.get("type") == "image_url":
        return _openai_payload(block)
    return None


def content_text(message: Any) -> str:
    content = message_content(message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_block_text(block) for block in content)
    return str(content)


def strip_image_payloads(messages: list[Any]) -> list[Any]:
    stripped: list[Any] = []
    for message in messages:
        content = message_content(message)
        if not isinstance(content, list) or not image_blocks(message):
            stripped.append(message)
            continue
        rebuilt = [_strip_block_payload(block) for block in content]
        if isinstance(message, dict):
            stripped.append({**message, "content": rebuilt})
        elif hasattr(message, "model_copy"):
            stripped.append(message.model_copy(update={"content": rebuilt}))
        else:
            stripped.append(message)
    return stripped


def _block_text(block: Any) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        return str(block.get("text") or "")
    return ""


def _standard_payload(block: dict) -> tuple[str, str] | None:
    data = block.get("base64")
    if not isinstance(data, str) or not data:
        return None
    mime = block.get("mime_type")
    return (str(mime) if mime else _DEFAULT_IMAGE_MIME, data)


def _openai_payload(block: dict) -> tuple[str, str] | None:
    image_url = block.get("image_url")
    if isinstance(image_url, dict):
        image_url = image_url.get("url")
    if not isinstance(image_url, str):
        return None
    match = _DATA_URL_PATTERN.match(image_url)
    if match is None:
        return None
    return (match.group("mime") or _DEFAULT_IMAGE_MIME, match.group("data"))


def _strip_block_payload(block: Any) -> Any:
    if not is_image_block(block):
        return block
    if block.get("type") == "image":
        return {**block, "base64": IMAGE_PAYLOAD_PLACEHOLDER}
    image_url = block.get("image_url")
    if isinstance(image_url, dict):
        return {**block, "image_url": {**image_url, "url": IMAGE_PAYLOAD_PLACEHOLDER}}
    return {**block, "image_url": IMAGE_PAYLOAD_PLACEHOLDER}
