"""本文件验证会话文件模式由独立事务持久化，旧 UI 快照不能覆盖已提交的模式。

输入为同一任务的连续模式更新、旧界面状态快照和模拟持久会话。
输出为最终数据库状态及实时模式读取器观察到的常驻模式。
具体工作流为先保存更宽模式，再提交携带旧模式的 UI 状态，最后读取执行策略。
示例：运行 python -m pytest backend/tests/test_session_access_mode_persistence.py。
"""

import asyncio
from types import SimpleNamespace

from backend.app.desktop.models import SessionAccessModeUpdate
from backend.app.desktop.service import DesktopService
from focus.security.policy import AccessMode


class _Session:
    def __init__(self):
        self.task = SimpleNamespace(ui_state={"access_mode": "workspace-write", "input": "old"})
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def scalar(self, _query):
        return self.task

    async def get(self, _model, _task_id):
        return self.task

    async def commit(self):
        self.commits += 1


def test_stale_ui_state_cannot_revert_committed_session_mode():
    session = _Session()
    service = DesktopService.__new__(DesktopService)
    service.session_factory = lambda: session

    async def scenario():
        await service.set_session_access_mode("task-1", "danger-full-access")
        await service.save_ui_state("task-1", {"access_mode": "workspace-write", "input": "new"})
        return await service._session_mode_resolver("task-1", AccessMode.WORKSPACE_WRITE)()

    observed = asyncio.run(scenario())
    assert observed is AccessMode.DANGER_FULL_ACCESS
    assert session.task.ui_state == {"access_mode": "danger-full-access", "input": "new"}
    assert session.commits == 2


def test_mode_update_request_accepts_only_canonical_modes():
    assert SessionAccessModeUpdate(access_mode="read-only").access_mode == "read-only"
    for legacy in ("workspace", "full"):
        try:
            SessionAccessModeUpdate(access_mode=legacy)
        except ValueError:
            continue
        raise AssertionError(f"legacy mode accepted: {legacy}")
