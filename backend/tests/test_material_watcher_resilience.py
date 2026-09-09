"""材料监测器对持续文件系统故障的回归测试。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from backend.app.desktop import service as service_module
from backend.app.desktop.service import DesktopService


class _Rows:
    def all(self):
        material = SimpleNamespace(material_id="material-denied", relative_path="material.txt")
        workspace = SimpleNamespace(path="C:/denied-workspace")
        return [(material, None, workspace)]


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _statement):
        return _Rows()


def test_material_watcher_logs_persistent_failure_only_once(monkeypatch):
    """同一材料持续无权限时不得每两秒重复打印完整异常。"""

    service = object.__new__(DesktopService)
    service.session_factory = _Session

    sleep_count = 0

    async def two_cycles_then_cancel(_delay):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count > 2:
            raise asyncio.CancelledError

    def deny_access(_path):
        raise PermissionError("workspace denied")

    monkeypatch.setattr(service_module.asyncio, "sleep", two_cycles_then_cancel)
    monkeypatch.setattr(service_module.Path, "exists", deny_access)
    warning = Mock()
    monkeypatch.setattr(service_module.logger, "warning", warning)

    asyncio.run(service._watch_materials())

    assert warning.call_count == 1
    assert warning.call_args.args == (
        "材料监测失败，跳过该材料: material_id=%s",
        "material-denied",
    )
    assert warning.call_args.kwargs == {"exc_info": True}
