r"""本文件对外提供 Patrol 自主 Context 压缩包的授权合同、候选事实、纯策略与提交仓储。

输入为版本化 Loop delegation、稳定 compression interrupt、精确 Context revision 和 Patrol 候选请求；
输出为不可变候选、确定性授权 verdict 与可恢复 resolution。具体工作流为 candidate preparation 只
产出 proposal，Kernel 经纯策略验证后提交 resolution，恢复协调器再调用既有 compression resume
路径。示例：`verdict = CompressionAuthorityPolicy().evaluate(facts)`。
"""

from backend.app.desktop.agent_loop.compression_authority.contracts import (
    ApplyContextCompressionAction,
    AutonomousCompressionPolicy,
    CompressionCandidateRequest,
)
from backend.app.desktop.agent_loop.compression_authority.policy import (
    CompressionAuthorityFacts,
    CompressionAuthorityPolicy,
    CompressionAuthorityVerdict,
)

__all__ = [
    "ApplyContextCompressionAction",
    "AutonomousCompressionPolicy",
    "CompressionAuthorityFacts",
    "CompressionAuthorityPolicy",
    "CompressionAuthorityVerdict",
    "CompressionCandidateRequest",
]
