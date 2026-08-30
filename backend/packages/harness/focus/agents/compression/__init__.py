"""本文件为 focus.agents.compression 包入口，重导出压缩门、令牌估算与机械命中的公开 API。

对外提供:
    CompressionGate — 主 Agent 模型调用前的 human-in-the-loop 压缩门中间件
    build_compression_gate — 压缩门同步工厂函数
    apply_compression_ranges — 把压缩范围编译为新的 messages 状态（阈值/快捷压缩共用）
    estimate_messages_tokens — messages 整体 token 启发式估算
    hit_keyword_message_ids — 子串匹配找出含某关键词的消息 id（机械命中）
    build_keyword_ranges — 把选中的消息 id 组装为压缩范围列表
    scrub_message_contents — 机械剥离消息文本中的禁用词（先抠词再压缩）
"""

from focus.agents.compression.gate import (
    CompressionGate,
    apply_compression_ranges,
    build_compression_gate,
)
from focus.agents.compression.keyword import (
    build_keyword_ranges,
    hit_keyword_message_ids,
    scrub_message_contents,
)
from focus.agents.compression.tokens import estimate_messages_tokens

__all__ = [
    "CompressionGate",
    "apply_compression_ranges",
    "build_compression_gate",
    "build_keyword_ranges",
    "estimate_messages_tokens",
    "hit_keyword_message_ids",
    "scrub_message_contents",
]
