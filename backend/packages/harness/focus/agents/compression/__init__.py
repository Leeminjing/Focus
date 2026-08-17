"""本文件为 focus.agents.compression 包入口，重导出压缩门与令牌估算的公开 API。

对外提供:
    CompressionGate — 主 Agent 模型调用前的 human-in-the-loop 压缩门中间件
    build_compression_gate — 压缩门同步工厂函数
    estimate_messages_tokens — messages 整体 token 启发式估算
"""

from focus.agents.compression.gate import CompressionGate, build_compression_gate
from focus.agents.compression.tokens import estimate_messages_tokens

__all__ = [
    "CompressionGate",
    "build_compression_gate",
    "estimate_messages_tokens",
]
