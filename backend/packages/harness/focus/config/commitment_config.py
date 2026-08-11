"""
本文件定义 CommitmentConfig 配置模型，控制承诺层（CommitmentMiddleware）的装配开关。

对外提供:
    CommitmentConfig(BaseModel) — 承诺层配置对象

输入:
    config.yaml 的 commitment 段，或 AppConfig.model_validate 递归解析

输出:
    CommitmentConfig 实例

工作流:
    (1) AppConfig 聚合 commitment: CommitmentConfig = CommitmentConfig()
    (2) commitment.enabled=false（默认）时不装配 CommitmentMiddleware
    (3) commitment.enabled=true 时装配，触发条件见 commitment-layer spec

示例:
    config = CommitmentConfig()              # enabled=False
    config = CommitmentConfig(enabled=True)
"""

from pydantic import BaseModel


class CommitmentConfig(BaseModel):
    enabled: bool = False
    context7_url: str = "https://mcp.context7.com/mcp"
