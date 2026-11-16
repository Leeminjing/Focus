"""本文件对外提供 repaired_execution_for_run 的只读执行投影解析。

输入为持久 Main Run、会话和 checkpointer；输出为与该 Run 精确来源一致的修复消息，其他合法投影返回 None。
具体工作流为优先读取自足的 Run 输入快照并核验 hash 与执行身份；迁移中 Run 仅能读取仍由运行 owner 固定的精确输入或当前状态。
仅消费已有非成功修复，不生成工具结果、不修改 checkpoint 或冻结来源。resume 调用方使用原中断任务。
示例：messages = await repaired_execution_for_run(session, run, checkpointer)。
"""

from backend.app.desktop.contexts.evolution import (
    ContextRevisionProjectionStatus,
    ContextRevisionReader,
    ContextRevisionRepository,
)
from backend.app.desktop.runs.context_snapshot import context_snapshot_for_run


async def repaired_execution_for_run(session, run, checkpointer):
    if run.kind != "main" or run.context_revision_id is None:
        return None
    snapshot = context_snapshot_for_run(run)
    if snapshot is not None:
        revision, views = snapshot
        if revision.projection_status is not ContextRevisionProjectionStatus.REPAIRED:
            return None
        return tuple(views["execution"])
    repository = ContextRevisionRepository()
    revision = await repository.get_by_id(session, run.context_revision_id)
    await repository.get_input(session, revision.ref)
    if revision.projection_status is not ContextRevisionProjectionStatus.REPAIRED:
        return None
    ref = revision.ref
    if (ref.context_id, ref.execution_thread_id, ref.checkpoint_ns, ref.checkpoint_id) != (
        run.task_id, run.execution_thread_id, run.checkpoint_ns or "", run.context_checkpoint_id
    ) or not ref.is_runnable or revision.projection_status not in {
        ContextRevisionProjectionStatus.VALID, ContextRevisionProjectionStatus.REPAIRED, ContextRevisionProjectionStatus.APPROVED
    }:
        raise ValueError("Run 来源与可运行 Context execution 投影不一致")
    view = await ContextRevisionReader(repository, checkpointer).read(session, ref, "execution")
    return view.messages
