r"""本文件对外提供 PersistencePayloadNormalizer 与 NormalizedPayload 的兼容重导出。

输入为任意持久化载荷与来源标签；输出为 Desktop 公共边界生成的安全值和可审计元数据。
具体工作流为保持 Agent Loop 稳定导入路径，同时把实现放在不依赖领域 package 初始化的公共模块中以避免依赖环。
示例：`safe = PersistencePayloadNormalizer.normalize(payload, "journal")`。
"""

from backend.app.desktop.persistence_safety import NormalizedPayload, PersistencePayloadNormalizer

__all__ = ["NormalizedPayload", "PersistencePayloadNormalizer"]
