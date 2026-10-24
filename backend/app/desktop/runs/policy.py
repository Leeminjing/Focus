"""桌面领域共享的值对象、约束或纯函数。"""

from __future__ import annotations

_RUN_DISPATCH_EQUIPMENT_KEY = "_durable_dispatch_execution"


_RUN_FINALIZATION_RETRY_INITIAL_SECONDS = 1.0


_RUN_FINALIZATION_RETRY_MAX_SECONDS = 30.0


_TERMINAL_STATUSES = frozenset({"success", "error", "interrupted"})


_ACCESS_DECISIONS = frozenset({"approve", "reject", "cancel"})
