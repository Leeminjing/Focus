"""验证运行安全上下文中的延迟脱敏回调可执行。

输入为真实安全装配、凭据配置及文件采集端口；输出为工具文件快照脱敏结果。
工作流经 governed context 和 ToolExecutionLedger 创建采集器，避免只检查回调存在。
示例：python -m pytest backend/tests/test_run_security_secret_provider.py。
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from backend.app.desktop.persistence.models import DesktopRun
from backend.app.desktop.runs.security_service import RunSecurityService


def test_governed_context_secret_provider_redacts_tool_file_snapshots(tmp_path):
    run = DesktopRun(
        run_id="run",
        task_id="task",
        agent_id="agent",
        workspace_anchor={"slot_id": "slot"},
    )
    slot = SimpleNamespace(root_path=str(tmp_path))

    class Session:
        async def get(self, model, identity):
            return run if model is DesktopRun else slot

    @asynccontextmanager
    async def sessions():
        yield Session()

    config = SimpleNamespace(
        models=[SimpleNamespace(api_key="test-private-model-value")]
    )
    service = RunSecurityService(
        SimpleNamespace(session_factory=sessions, app_config=config)
    )
    context = service._governed_context(
        thread_id="task",
        run=run,
        workspace_id="workspace",
        workspace_path=str(tmp_path),
        permissions=["read", "write"],
        access_mode="workspace-write",
        checkpoint_ns="",
        agent_role="main",
        model_name=None,
        allow_global_config=False,
        extras={},
    )
    ledger = context["tool_execution_ledger"]
    capture = asyncio.run(ledger.prepare_file_capture(run.run_id))
    target = tmp_path / "result.txt"
    target.write_text("value test-private-model-value", encoding="utf-8")
    capture.before_write(target)
    target.write_text("updated test-private-model-value", encoding="utf-8")
    capture.after_write(target)
    assert len(capture.changes) == 1
    assert "test-private-model-value" not in str(capture.changes)
    assert "***" in str(capture.changes)
