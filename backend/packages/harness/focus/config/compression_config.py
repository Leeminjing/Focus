"""
本文件定义 CompressionConfig 配置模型，控制 Patrol 托管执行的自动压缩门（CompressionGate）
的装配开关与触发阈值。

对外提供:
    CompressionConfig(BaseModel) — 压缩配置对象

输入:
    config.yaml 的 compression 段，或 AppConfig.model_validate 递归解析

输出:
    CompressionConfig 实例

工作流:
    (1) AppConfig 聚合 compression: CompressionConfig = CompressionConfig()
    (2) enabled=false（默认）时不装配压缩门
    (3) enabled=true 时只为 Patrol Loop 托管的主 Agent 装配，超过
        context_window × threshold_ratio 时交给 Patrol 自动生成候选；普通会话不产生人工编辑请求

示例:
    config = CompressionConfig()                # enabled=False
    config = CompressionConfig(enabled=True, threshold_ratio=0.9)
"""

from pydantic import BaseModel


class CompressionConfig(BaseModel):
    enabled: bool = False
    threshold_ratio: float = 0.9
