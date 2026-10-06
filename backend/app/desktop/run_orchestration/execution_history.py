"""本文件对外提供 repaired_execution_for_run 的只读执行投影解析。

输入为持久 Main Run、会话和 checkpointer；输出为与该 Run 精确来源一致的已发布修复消息，其他合法投影返回 None。
具体工作流为读取不可变 revision，核对 Context/thread/namespace/checkpoint 和可运行状态，然后复用 execution reader；
仅消费已有非成功修复，不生成工具结果、不修改旧 checkpoint、revision 或冻结来源。resume 调用方使用原中断任务。
示例：messages = await repaired_execution_for_run(session, run, checkpointer)。
"""

from backend.app.desktop.context_evolution import ContextRevisionReader, ContextRevisionRepository, ContextRevisionProjectionStatus


async def repaired_execution_for_run(session, run, checkpointer):
    if run.kind != "main" or run.context_revision_id is None:
        return None
    repository = ContextRevisionRepository()
    revision = await repository.get_by_id(session, run.context_revision_id)
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
