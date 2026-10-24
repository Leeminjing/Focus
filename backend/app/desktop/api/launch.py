"""桌面 launch HTTP 接口。"""

from __future__ import annotations

from fastapi import Request

from backend.app.desktop.runs.contracts import PreparedRun
from backend.app.desktop.runs.orchestration import RunLauncher
from backend.app.gateway.services import start_run


async def _launch(request: Request, prepared: PreparedRun | None) -> None:
    """桌面运行接口的统一发起入口：委托 services.start_run 创建并执行 run，并挂载 DB 终态同步。

    输入:
        request: Request — FastAPI 请求（提供 app.state 资源）
        prepared: PreparedRun | None — 编排输入；None 表示幂等命中已有 run，无需发起
    """
    await RunLauncher(start_run).launch(prepared, request)
