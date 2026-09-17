r"""本文件对外提供 Patrol 自主 Context 压缩包的授权合同、候选事实、证据验证与纯策略入口。

输入为版本化 Loop delegation、稳定 compression interrupt、精确 Context revision 和 Patrol 候选请求；
输出为不可变候选、checkpoint 内容证据、确定性授权 verdict 与可恢复 resolution。具体工作流为
candidate preparation 只产出带来源哈希的 proposal，Kernel 经纯策略验证后提交 resolution，恢复
协调器核对 checkpoint 内容证据后收口。示例：`verdict = CompressionAuthorityPolicy().evaluate(facts)`。
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
from backend.app.desktop.agent_loop.compression_authority.evidence import (
    CompressionCheckpointEvidence,
    CompressionEvidenceVerifier,
)

__all__ = [
    "ApplyContextCompressionAction",
    "AutonomousCompressionPolicy",
    "CompressionAuthorityFacts",
    "CompressionAuthorityPolicy",
    "CompressionAuthorityVerdict",
    "CompressionCandidateRequest",
    "CompressionCheckpointEvidence",
    "CompressionEvidenceVerifier",
]
