"""持久字段的跨层常量与线程标题约束。"""

MAIN_RUN_EQUIPMENT_KEY = "_main_run_equipment"
"""desktop_threads.ui_state 中主运行装备快照的键名（跨服务共享的存储约定）。"""

THREAD_TITLE_LIMIT = 200
"""desktop_threads.title 的列宽；从散文派生的标题必须先经 bounded_thread_title 收进该界。"""


def bounded_thread_title(text: str) -> str:

    return str(text or "")[:THREAD_TITLE_LIMIT]


"""桌面领域共享的值对象、约束或纯函数。"""

from backend.app.desktop.persistence.contracts import MAIN_RUN_EQUIPMENT_KEY

_MAIN_RUNTIME_EQUIPMENT_KEY = MAIN_RUN_EQUIPMENT_KEY
