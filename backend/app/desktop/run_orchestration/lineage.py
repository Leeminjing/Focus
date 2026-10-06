"""本文件对外提供 validate_parent_chain 的执行父链准入校验。

输入为同一事务和待受理 DesktopRun；输出为通过或明确的父链 ValueError。
具体工作流为遍历持久 parent_run_id，拒绝缺失、环与跨工作区/Loop/Round 链；不猜测未知历史来源，
Loop 的权威合法性由组合根注入的上层政策负责。本模块不启动执行、不依赖 Agent Loop。
示例：await validate_parent_chain(session, run)。
"""

from backend.app.desktop.models import DesktopRun, DesktopThread


async def validate_parent_chain(session, run):
    context = await session.get(DesktopThread, run.task_id)
    seen = {run.run_id}
    parent_id = run.parent_run_id
    while parent_id:
        if parent_id in seen:
            raise ValueError("Run 父链存在环")
        seen.add(parent_id)
        parent = await session.get(DesktopRun, parent_id)
        if parent is None:
            raise ValueError("Run 父执行不存在")
        if (parent.loop_id, parent.round_id) != (run.loop_id, run.round_id):
            raise ValueError("Run 父链跨 Loop 或 Round")
        parent_context = await session.get(DesktopThread, parent.task_id)
        if context is None or parent_context is None or context.workspace_id != parent_context.workspace_id:
            raise ValueError("Run 父链跨工作区或 Context 不存在")
        parent_id = parent.parent_run_id
