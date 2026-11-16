"""本文件对外提供 repaired_execution_for_run 的只读执行投影解析。

输入为持久 Main Run、会话和 checkpointer；输出为与该 Run 精确来源一致的修复消息，其他合法投影返回 None。
具体工作流为从单一持久输入端口读取 Run owner 持有的精确来源，核验执行身份并按需投影 execution view。
仅消费已有非成功修复，不生成工具结果、不修改 checkpoint 或冻结来源。resume 调用方使用原中断任务。
示例：messages = await repaired_execution_for_run(session, run, checkpointer)。
"""

from backend.app.desktop.contexts.evolution import (
    ContextRevisionProjectionStatus,
    ContextRevisionReader,
    ContextRevisionRepository,
)


async def repaired_execution_for_run(session, run, checkpointer):
    if run.kind != "main" or run.context_revision_id is None:
        return None
    repository = ContextRevisionRepository()
    revision = await repository.input_for_run(session, run)
    if revision.projection_status is not ContextRevisionProjectionStatus.REPAIRED:
        return None
    view = await ContextRevisionReader(repository, checkpointer).read(session, revision.ref, "execution")
    return view.messages
