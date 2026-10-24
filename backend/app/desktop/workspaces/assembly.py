"""桌面领域共享的值对象、约束或纯函数。"""

from __future__ import annotations

_ASSEMBLY_WORKSPACE_DISPLAY = "无工作区模式"


_ASSEMBLY_THREAD_ID = "focus-assembly"


def _is_assembly_thread(thread_id: str) -> bool:
    return thread_id == _ASSEMBLY_THREAD_ID or thread_id.startswith(
        _ASSEMBLY_THREAD_ID + ":"
    )
