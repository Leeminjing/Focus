"""Patrol 自动压缩的门控与受验证摘要应用。"""

from focus.agents.compression.gate import (
    CompressionGate,
    apply_compression_ranges,
    build_compression_gate,
)

__all__ = [
    "CompressionGate",
    "apply_compression_ranges",
    "build_compression_gate",
]
