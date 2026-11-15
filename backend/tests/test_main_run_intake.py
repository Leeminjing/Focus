"""验证主输入受理的重放幂等与派发身份合同。

输入为真实请求模型及可控服务端口；输出为分流顺序、原始载荷及通知断言。
工作流分别覆盖已有 Run 重放及新 Run 准备，确保 HTTP 拆分保留行为。
示例：python -m pytest backend/tests/test_main_run_intake.py。
"""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.app.desktop.api.schemas import MainRunCreate
from backend.app.desktop.runs.intake import MainRunIntake


def desktop_ports(existing=None):
    return SimpleNamespace(
        run_by_idempotency=AsyncMock(return_value=existing),
        start_main_run=AsyncMock(
            return_value=SimpleNamespace(payload={"run_id": "new"})
        ),
        notify_run_dispatch=Mock(),
    )




def test_existing_direct_run_wakes_dispatch_without_preparing_another_run():
    existing = {"run_id": "existing"}
    desktop = desktop_ports(existing)
    result = asyncio.run(
        MainRunIntake(desktop).submit(
            "task", MainRunCreate(message="hello"), "key"
        )
    )
    assert result is existing
    desktop.run_by_idempotency.assert_awaited_once_with("direct-user:key")
    desktop.start_main_run.assert_not_awaited()
    desktop.notify_run_dispatch.assert_called_once_with()


@pytest.mark.parametrize("material_inputs", [None, []])
def test_new_direct_run_preserves_identity_and_optional_material_input(material_inputs):
    desktop = desktop_ports()
    body = MainRunCreate(message="hello", material_inputs=material_inputs)
    result = asyncio.run(MainRunIntake(desktop).submit("task", body, "key"))
    assert result == {"run_id": "new"}
    submitted = desktop.start_main_run.await_args.kwargs
    assert submitted["task_id"] == "task"
    assert submitted["material_inputs"] == material_inputs
    assert submitted["run_identity"] == {
        "message_id": uuid.uuid5(
            uuid.NAMESPACE_URL, "focus:direct-user-message:key"
        ).hex,
        "origin": "direct_user",
        "idempotency_key": "direct-user:key",
    }
    desktop.notify_run_dispatch.assert_called_once_with()
