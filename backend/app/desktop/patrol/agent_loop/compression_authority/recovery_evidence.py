r"""本文件对外提供 CompressionRecoveryEvidenceReader 的稳定恢复证据读取。

输入为数据库会话、候选及 resume Run；输出为不可变 checkpoint evidence。
具体工作流为核对候选固定输入与 Run 执行身份，再精确读取该 Run 的 final checkpoint，确认原
compression interrupt 已消费并验证来源及 replacement 哈希。Context current 前进不改变结果证据。示例：
`evidence = await reader.inspect(session, candidate, run)`。
"""

from __future__ import annotations

from focus.runtime.runs.events import serialize_message
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.contexts.evolution import (
    ContextRevisionNotFound,
    ContextRevisionRepository,
)
from backend.app.desktop.conversations.interrupts import pending_interrupt_payload
from backend.app.desktop.patrol.agent_loop.compression_authority.evidence import (
    CompressionCheckpointEvidence,
    CompressionEvidenceVerifier,
)


class CompressionRecoveryEvidenceReader:
    def __init__(self, checkpointer) -> None:
        self._checkpointer = checkpointer
        self._revisions = ContextRevisionRepository()

    async def inspect(self, session: AsyncSession, candidate, run) -> CompressionCheckpointEvidence:
        if not run.final_checkpoint_id or run.task_id != candidate.context_id:
            return CompressionCheckpointEvidence(False, "result_checkpoint_identity_missing")
        try:
            source = await self._revisions.get_by_id(session, candidate.base_context_revision_id)
            source = await self._revisions.get_input(session, source.ref)
        except ContextRevisionNotFound:
            return CompressionCheckpointEvidence(False, "candidate_input_snapshot_missing")
        if (source.ref.context_id != candidate.context_id
                or source.ref.checkpoint_id != candidate.base_checkpoint_id
                or run.execution_thread_id != source.ref.execution_thread_id
                or (run.checkpoint_ns or "") != source.ref.checkpoint_ns):
            return CompressionCheckpointEvidence(False, "result_execution_identity_mismatch")
        checkpoint = await self._checkpointer.aget_tuple({"configurable": {
            "thread_id": run.execution_thread_id,
            "checkpoint_ns": run.checkpoint_ns or "",
            "checkpoint_id": run.final_checkpoint_id,
        }})
        if checkpoint is None:
            return CompressionCheckpointEvidence(False, "result_checkpoint_missing")
        actual = checkpoint.config.get("configurable", {})
        if (actual.get("checkpoint_id") != run.final_checkpoint_id
                or actual.get("thread_id") != run.execution_thread_id
                or actual.get("checkpoint_ns", "") != (run.checkpoint_ns or "")):
            return CompressionCheckpointEvidence(False, "result_checkpoint_identity_mismatch")
        if pending_interrupt_payload(checkpoint, "compression_request") is not None:
            return CompressionCheckpointEvidence(False, "compression_interrupt_still_pending")
        values = checkpoint.checkpoint.get("channel_values", {})
        messages = tuple(serialize_message(message) for message in values.get("messages", ()))
        return CompressionEvidenceVerifier.verify(messages, list(candidate.normalized_ranges or ()))
