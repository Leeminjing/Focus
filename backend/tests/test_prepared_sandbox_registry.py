"""本文件对外提供持久工作区登记和私有临时清理告警测试。

输入为独立工作区、注册表文件及模拟的临时授权撤销失败。
输出为重建后仍可查询的已准备根，以及不会被静默忽略的清理告警。
具体工作流为登记去重并重建注册表，然后模拟释放失败并核对诊断与安全清理范围。
示例：运行 python -m pytest backend/tests/test_prepared_sandbox_registry.py。
"""

import shutil
from pathlib import Path

from focus.sandbox.prepared import PreparedWorkspaceRegistry
from focus.sandbox.session_temp import SessionTempGrant, SessionTempRegistry


def test_prepared_workspaces_persist_and_deduplicate(tmp_path):
    first = tmp_path / "workspace-a"
    second = tmp_path / "workspace-b"
    first.mkdir()
    second.mkdir()
    path = tmp_path / "prepared.json"
    registry = PreparedWorkspaceRegistry(path)
    registry.record(first)
    registry.record(first)
    registry.record(second)
    assert PreparedWorkspaceRegistry(path).list() == (str(first.resolve()), str(second.resolve()))


def test_temp_release_failure_reports_warning(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    temp_dir = root / "focus-sandbox-test"
    temp_dir.mkdir()
    registry = SessionTempRegistry()
    registry._grants[("session", "workspace")] = SessionTempGrant(
        workspace=root, temp_dir=temp_dir,
        write_sid="S-1-4-1", temp_write_sid="S-1-4-2",
    )

    def fail_release(*args):
        raise RuntimeError("simulated ACL failure")

    monkeypatch.setattr(registry, "_invoke", fail_release)
    try:
        warnings = registry.close("node", Path("helper.js"), root)
        assert len(warnings) == 1
        assert "撤销临时授权失败" in warnings[0]
        assert temp_dir.exists()
    finally:
        assert temp_dir.resolve().is_relative_to(root)
        shutil.rmtree(temp_dir)
