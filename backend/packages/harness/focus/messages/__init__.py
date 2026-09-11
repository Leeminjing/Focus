"""本文件为 focus.messages 包入口，重导出消息内容块识别与 token 用量核算的公开 API。

对外提供:
    message_content — 取出单条消息的 content 字段
    is_image_block — 判定单个内容块是否为图像块
    image_blocks — 取出一条消息里的全部图像块
    image_inline_payload — 提取内联图像块的 (mime, base64) 二元组
    content_text — 把一条消息的 content 归一为拼接文本
    strip_image_payloads — 把内联图像载荷替换为占位符
    IMAGE_PAYLOAD_PLACEHOLDER — 内联载荷的替换占位符
    MATERIAL_REF_PATTERN — 消息引用材料的标记正则
    format_material_ref — 生成材料引用标记
    material_ref_ids — 取出一段文本里引用的全部材料标识
    strip_material_refs — 移除文本中的材料引用标记
    references_material — 判定文本是否引用了给定材料
    estimate_raw_tokens — 纯文本启发式 token 估算
    estimate_image_tokens — 单张图片按尺寸折算的 token 数
    estimate_images_tokens — 消息列表里全部图片的折算 token 数
    estimate_messages_tokens — 消息列表整体 token 数

工作流:
    包内三个模块各司一职：blocks 只认识内容块的形状，material_refs 只承载
    「消息引用材料」的文本约定，usage 只把消息折算为用量。usage 依赖 blocks，
    blocks 与 material_refs 互不依赖。

示例:
    from focus.messages import estimate_messages_tokens, format_material_ref
"""

from focus.messages.blocks import (
    IMAGE_PAYLOAD_PLACEHOLDER,
    content_text,
    image_blocks,
    image_inline_payload,
    is_image_block,
    message_content,
    strip_image_payloads,
)
from focus.messages.material_refs import (
    MATERIAL_REF_PATTERN,
    format_material_ref,
    material_ref_ids,
    references_material,
    strip_material_refs,
)
from focus.messages.usage import (
    estimate_image_tokens,
    estimate_images_tokens,
    estimate_messages_tokens,
    estimate_raw_tokens,
)

__all__ = [
    "IMAGE_PAYLOAD_PLACEHOLDER",
    "MATERIAL_REF_PATTERN",
    "content_text",
    "estimate_image_tokens",
    "estimate_images_tokens",
    "estimate_messages_tokens",
    "estimate_raw_tokens",
    "format_material_ref",
    "image_blocks",
    "image_inline_payload",
    "is_image_block",
    "material_ref_ids",
    "message_content",
    "references_material",
    "strip_image_payloads",
    "strip_material_refs",
]
