"""
本文件定义 StreamBridgeConfig Pydantic 配置模型。

对外提供:
    StreamBridgeConfig(BaseModel) — stream_bridge 配置段的数据模型

输入: config.yaml 中 stream_bridge 段的原始数据
输出: StreamBridgeConfig 实例

示例:
    from focus.config.stream_bridge_config import StreamBridgeConfig

    cfg = StreamBridgeConfig(type="memory", queue_maxsize=512)
"""

from pydantic import BaseModel, Field


class StreamBridgeConfig(BaseModel):
    type: str = "memory"
    queue_maxsize: int = Field(default=512, ge=1)
