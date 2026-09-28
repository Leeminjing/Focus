"""本文件对外提供结构化文件写工具的实际文件边界测试。

输入为受治理模式、工作区路径以及可选的 Windows 链接目标。
输出为只读和越界拒绝时零文件副作用、工作区与共享临时区可写的证据。
具体工作流为直接调用文件工具以检查最终 IO 边界，先验证拒绝，再核对允许的文件内容。
示例：运行 python -m pytest backend/tests/test_structured_file_sandbox.py。
"""

import os
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from backend.tests.runtime_context_support import tool_runtime
from focus.security.policy import AccessMode
from focus.tools.builtins.workspace_tools import write_file


def _runtime(workspace: Path, mode: AccessMode):
    return tool_runtime(
        agent_id="agent-1", task_id="task-1", workspace=str(workspace),
        permissions=("read", "write"), access_mode=mode,
    )


def test_read_only_rejects_before_creating_parent(tmp_path):
    target = tmp_path / "new-directory" / "result.txt"
    with pytest.raises(PermissionError, match="FILE_POLICY_DENIED"):
        write_file.func(
            path=str(target), content="escaped", runtime=_runtime(tmp_path, AccessMode.READ_ONLY),
        )
    assert not target.parent.exists()


def test_workspace_write_rejects_home_target_before_io(tmp_path):
    outside = Path.home() / f"focus-denied-{uuid.uuid4().hex}.txt"
    with pytest.raises(PermissionError, match="FILE_POLICY_DENIED"):
        write_file.func(
            path=str(outside), content="escaped", runtime=_runtime(tmp_path, AccessMode.WORKSPACE_WRITE),
        )
    assert not outside.exists()


def test_workspace_and_shared_platform_temp_are_writable(tmp_path):
    inside = tmp_path / "result.txt"
    shared = Path(tempfile.gettempdir()) / f"focus-structured-{uuid.uuid4().hex}.txt"
    runtime = _runtime(tmp_path, AccessMode.WORKSPACE_WRITE)
    try:
        write_file.func(path=str(inside), content="inside", runtime=runtime)
        write_file.func(path=str(shared), content="shared", runtime=runtime)
        assert inside.read_text() == "inside"
        assert shared.read_text() == "shared"
    finally:
        shared.unlink(missing_ok=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows 目录联接目标验收")
def test_existing_directory_link_resolves_outside_before_write(tmp_path):
    link = tmp_path / "linked-home"
    creation = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(link), str(Path.home())],
        capture_output=True, text=True, errors="replace", check=False,
    )
    if creation.returncode != 0:
        pytest.skip(f"当前 Windows 身份不能建立目录联接: {creation.stderr}")
    target = link / f"focus-denied-{uuid.uuid4().hex}.txt"
    try:
        with pytest.raises(PermissionError, match="FILE_POLICY_DENIED"):
            write_file.func(
                path=str(target), content="escaped", runtime=_runtime(tmp_path, AccessMode.WORKSPACE_WRITE),
            )
        assert not target.exists()
    finally:
        assert os.path.isjunction(link)
        link.rmdir()
