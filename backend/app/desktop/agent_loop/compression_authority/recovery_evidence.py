r"""本文件对外提供 CompressionRecoveryEvidenceReader 的稳定恢复证据读取。

输入为数据库会话、候选、resume Run 与预期结果 revision；输出为不可变 checkpoint evidence。
具体工作流为先核对 revision/checkpoint/origin，再确认原 compression interrupt 已消费，最后读取精确
execution revision 并委托纯证据验证器核对压缩块来源与 replacement 哈希。示例：
`evidence = await reader.inspect(session, candidate, run, revision)`。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.compression_authority.evidence import (
    CompressionCheckpointEvidence,
    CompressionEvidenceVerifier,
)
from backend.app.desktop.compression import compression_recovery_payload
from backend.app.desktop.context_evolution import ContextRevisionReader, ContextRevisionRepository
from backend.app.desktop.models import DesktopThread


class CompressionRecoveryEvidenceReader:
    def __init__(self, checkpointer) -> None:
        self._checkpointer = checkpointer
        self._reader = ContextRevisionReader(ContextRevisionRepository(), checkpointer)

    async def inspect(self, session: AsyncSession, candidate, run, revision) -> CompressionCheckpointEvidence:
        if revision is None:
            return CompressionCheckpointEvidence(False, "result_revision_missing")
        if revision.ref.checkpoint_id != run.final_checkpoint_id or revision.origin_id != run.run_id:
            return CompressionCheckpointEvidence(False, "result_revision_identity_mismatch")
        task = await session.get(DesktopThread, candidate.context_id)
        if task is None:
            return CompressionCheckpointEvidence(False, "result_context_missing")
        if await compression_recovery_payload(session, task, self._checkpointer) is not None:
            return CompressionCheckpointEvidence(False, "compression_interrupt_still_pending")
        view = await self._reader.read(session, revision.ref, "execution")
        return CompressionEvidenceVerifier.verify(tuple(view.messages), list(candidate.normalized_ranges or ()))
