"""本文件对外提供 Windows 文件沙箱特殊边界的可复现探针。

输入为独立 NTFS 测试根和真实受限子进程。
输出为硬链接文件对象、CIM/WMI、子进程管道及受保护目录的实际退出与文件效果。
具体工作流为逐项启动固定 Windows ACL 后端，打印环境相关的观察值，并断言
调用始终应用部分约束且硬链接两名始终对应同一个文件对象。
示例：运行 python -m pytest backend/tests/test_windows_sandbox_special_boundaries.py -s。
"""

import os
import sys
from pathlib import Path

import pytest

from focus.sandbox import ShellExecutionRequest, WindowsAclBackend
from focus.security.execution import CallExecutionBinding
from focus.security.policy import AccessMode
from windows_sandbox_fixture import windows_sandbox_roots


def _binding(workspace: Path) -> CallExecutionBinding:
    return CallExecutionBinding(
        run_id="special-run", session_id="special-session", call_id="special-call",
        agent_id="special-agent", agent_role="main", workspace=workspace,
        mode=AccessMode.WORKSPACE_WRITE, mode_source="execution-profile",
    )


@pytest.fixture
def backend(tmp_path):
    instance = WindowsAclBackend(prepared_registry_path=tmp_path / "prepared.json")
    yield instance
    assert instance.close() == ()


@pytest.mark.skipif(os.name != "nt", reason="需要真实 Windows NTFS")
def test_hard_link_alias_reports_actual_external_effect(windows_sandbox_roots, backend, capsys):
    roots = windows_sandbox_roots
    alias = roots.workspace_a / "outside-alias.txt"
    os.link(roots.outside_file, alias)
    assert alias.stat().st_ino == roots.outside_file.stat().st_ino
    result = backend.run(ShellExecutionRequest(
        _binding(roots.workspace_a), sys.executable,
        ("-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('through alias')", str(alias)),
    ))
    content = roots.outside_file.read_text(encoding="utf-8")
    print(f"hardlink: exit={result.exit_code}, external={content!r}")
    assert result.backend_applied and result.enforcement == "partial"
    assert (result.exit_code == 0) == (content == "through alias")


@pytest.mark.skipif(os.name != "nt", reason="需要真实 Windows")
def test_cim_and_grandchild_pipe_report_compatibility(windows_sandbox_roots, backend):
    roots = windows_sandbox_roots
    cim = backend.run(ShellExecutionRequest(
        _binding(roots.workspace_a), "powershell.exe",
        ("-NoProfile", "-Command", "Get-CimInstance Win32_OperatingSystem | Select-Object -ExpandProperty Caption"),
        timeout_seconds=20,
    ))
    print(f"cim: status={cim.status}, exit={cim.exit_code}, stderr={cim.stderr[:240]!r}")
    script = (
        "import subprocess,sys; p=subprocess.run([sys.executable,'-c',"
        "'print(\"GRANDCHILD_PIPE_OK\")'],capture_output=True,text=True); "
        "print(p.stdout.strip()); print('CHILD_EXIT',p.returncode)"
    )
    pipe = backend.run(ShellExecutionRequest(
        _binding(roots.workspace_a), sys.executable, ("-c", script),
    ))
    print(f"pipe: status={pipe.status}, exit={pipe.exit_code}, stdout={pipe.stdout[:240]!r}, stderr={pipe.stderr[:240]!r}")
    assert cim.backend_applied and cim.enforcement == "partial"
    assert pipe.backend_applied and pipe.enforcement == "partial"


@pytest.mark.skipif(os.name != "nt", reason="需要真实 Windows")
@pytest.mark.parametrize("directory_kind", ["WindowsApps", "Package"])
def test_appcontainer_directories_report_host_and_restricted_observations(
    windows_sandbox_roots, backend, directory_kind,
):
    if directory_kind == "WindowsApps":
        folder = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"
    else:
        package_root = Path(os.environ["LOCALAPPDATA"]) / "Packages"
        if not package_root.is_dir():
            pytest.skip("此 Windows 安装没有用户 Package 目录")
        folder = next((path for path in package_root.iterdir() if path.is_dir()), package_root)
    if not folder.is_dir():
        pytest.skip(f"此 Windows 安装没有 {directory_kind} 目录")
    try:
        host = "allowed" if any(folder.iterdir()) else "empty"
    except OSError as error:
        host = f"denied:{error.winerror}"
    code = (
        "from pathlib import Path; import sys; p=Path(sys.argv[1]); "
        "print('entries', len(list(p.iterdir())))"
    )
    result = backend.run(ShellExecutionRequest(
        _binding(windows_sandbox_roots.workspace_a), sys.executable,
        ("-c", code, str(folder)),
    ))
    print(f"appcontainer:{directory_kind}: host={host}, restricted_exit={result.exit_code}, stderr={result.stderr[:240]!r}")
    assert result.backend_applied and result.enforcement == "partial"
