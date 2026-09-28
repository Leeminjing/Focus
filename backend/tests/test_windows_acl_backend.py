"""本文件对外提供 Windows ACL 后端的失败关闭与真实文件效果测试。

输入为受治理调用绑定、独立 NTFS 工作区和可替换的 Node 适配器路径。
输出为缺失后端时目标未运行、独立状态通道不被目标输出伪造，以及两种受限模式的文件效果，
包括嵌套根拒绝、跨根重命名、目录联接越界、只读新建文件及四种解释器的写删效果。
具体工作流为先验证启动前失败零执行、启动后控制故障仍保留目标事实，再尝试越界写入，
每个测试结束时撤销该测试会话的私有临时授权，并核对清理失败只产生独立告警。
示例：运行 python -m pytest backend/tests/test_windows_acl_backend.py。
"""

import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from focus.sandbox import SandboxUnavailable, ShellExecutionRequest, WindowsAclBackend
from focus.security.execution import CallExecutionBinding
from focus.security.policy import AccessMode
from windows_sandbox_fixture import held_outside_handle, windows_sandbox_roots


def _binding(workspace: Path, mode: AccessMode = AccessMode.WORKSPACE_WRITE) -> CallExecutionBinding:
    return CallExecutionBinding(
        run_id="run-1", session_id="session-1", call_id="call-1",
        agent_id="agent-1", agent_role="main", workspace=workspace,
        mode=mode, mode_source="execution-profile",
    )


@pytest.fixture
def acl_backend(tmp_path):
    backend = WindowsAclBackend(prepared_registry_path=tmp_path / "prepared.json")
    yield backend
    assert backend.close() == ()


def test_missing_runner_fails_before_target_can_run(tmp_path):
    marker = tmp_path / "ran.txt"
    backend = WindowsAclBackend(runner_path=tmp_path / "absent.js")
    request = ShellExecutionRequest(_binding(tmp_path), "cmd.exe", ("/c", f"echo ran > {marker}"))
    with pytest.raises(SandboxUnavailable, match="runner"):
        backend.run(request)
    assert not marker.exists()


def test_missing_node_fails_before_target_can_run(tmp_path):
    marker = tmp_path / "ran.txt"
    backend = WindowsAclBackend(node_binary=tmp_path / "absent.exe")
    request = ShellExecutionRequest(_binding(tmp_path), "cmd.exe", ("/c", f"echo ran > {marker}"))
    with pytest.raises(SandboxUnavailable, match="Node"):
        backend.run(request)
    assert not marker.exists()


def test_missing_temp_root_fails_before_target_can_run(tmp_path):
    marker = tmp_path / "ran.txt"
    backend = WindowsAclBackend(temp_root=tmp_path / "absent")
    request = ShellExecutionRequest(_binding(tmp_path), "cmd.exe", ("/c", f"echo ran > {marker}"))
    with pytest.raises(SandboxUnavailable, match="临时根"):
        backend.run(request)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL 授权失败验收")
def test_grant_failure_does_not_start_target(windows_sandbox_roots, tmp_path):
    roots = windows_sandbox_roots
    helper = tmp_path / "failing-grant.mjs"
    helper.write_text("process.stderr.write('grant failed'); process.exit(5)", encoding="utf-8")
    backend = WindowsAclBackend(
        grant_helper_path=helper, prepared_registry_path=tmp_path / "prepared.json",
    )
    marker = roots.workspace_a / "must-not-run.txt"
    request = ShellExecutionRequest(
        _binding(roots.workspace_a), sys.executable,
        ("-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran')", str(marker)),
    )
    with pytest.raises(SandboxUnavailable, match="授权失败"):
        backend.run(request)
    assert not marker.exists()
    assert backend.close() == ()


@pytest.mark.skipif(os.name != "nt", reason="Windows 授权协议失败关闭验收")
def test_invalid_grant_response_is_infrastructure_failure(windows_sandbox_roots, tmp_path):
    roots = windows_sandbox_roots
    helper = tmp_path / "invalid-grant.mjs"
    helper.write_text("process.stdout.write('{}')", encoding="utf-8")
    backend = WindowsAclBackend(
        grant_helper_path=helper, prepared_registry_path=tmp_path / "prepared.json",
    )
    marker = roots.workspace_a / "must-not-run.txt"
    request = ShellExecutionRequest(
        _binding(roots.workspace_a), sys.executable,
        ("-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran')", str(marker)),
    )
    with pytest.raises(SandboxUnavailable, match="边界字段"):
        backend.run(request)
    assert not marker.exists()
    assert backend.close() == ()


@pytest.mark.skipif(os.name != "nt", reason="Windows 受限令牌验收")
def test_workspace_write_allows_inside_and_denies_outside(windows_sandbox_roots, acl_backend):
    roots = windows_sandbox_roots
    inside = roots.workspace_a / "generated.txt"
    script = (
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[1]).write_text('generated', encoding='utf-8')\n"
        "try:\n"
        "    Path(sys.argv[2]).write_text('escaped', encoding='utf-8')\n"
        "except OSError:\n"
        "    print('OUTSIDE_DENIED')\n"
    )
    result = acl_backend.run(ShellExecutionRequest(
        _binding(roots.workspace_a), sys.executable,
        ("-c", script, str(inside), str(roots.outside_file)),
    ))
    assert result.exit_code == 0, result.stderr
    assert result.enforcement == "partial"
    assert inside.read_text(encoding="utf-8") == "generated"
    assert roots.outside_file.read_text(encoding="utf-8") == "outside unchanged"
    assert "OUTSIDE_DENIED" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows 受限令牌验收")
def test_read_only_reads_and_prints_but_denies_workspace_and_temp_writes(windows_sandbox_roots, acl_backend):
    roots = windows_sandbox_roots
    script = (
        "from pathlib import Path\n"
        "import sys\n"
        "print(Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
        "for path in sys.argv[1:]:\n"
        "    try:\n"
        "        Path(path).write_text('changed', encoding='utf-8')\n"
        "    except OSError:\n"
        "        print('WRITE_DENIED')\n"
    )
    temp_file = roots.session_a_temp / "ordinary.txt"
    temp_file.write_text("temp unchanged", encoding="utf-8")
    workspace_file = roots.workspace_a / "ordinary.txt"
    result = acl_backend.run(ShellExecutionRequest(
        _binding(roots.workspace_a, AccessMode.READ_ONLY), sys.executable,
        ("-c", script, str(workspace_file), str(temp_file)),
    ))
    assert result.exit_code == 0, result.stderr
    assert "workspace unchanged" in result.stdout
    assert result.stdout.count("WRITE_DENIED") == 2
    assert workspace_file.read_text(encoding="utf-8") == "workspace unchanged"
    assert temp_file.read_text(encoding="utf-8") == "temp unchanged"


@pytest.mark.skipif(os.name != "nt", reason="Windows 只读新建文件验收")
def test_read_only_cannot_create_new_workspace_or_temp_file(windows_sandbox_roots, acl_backend):
    roots = windows_sandbox_roots
    targets = (roots.workspace_a / "new-file.txt", roots.session_a_temp / "new-file.txt")
    script = (
        "from pathlib import Path\n"
        "import sys\n"
        "for name in sys.argv[1:]:\n"
        "    try:\n"
        "        Path(name).write_text('created', encoding='utf-8')\n"
        "    except OSError:\n"
        "        print('CREATE_DENIED')\n"
    )
    result = acl_backend.run(ShellExecutionRequest(
        _binding(roots.workspace_a, AccessMode.READ_ONLY), sys.executable,
        ("-c", script, *(str(path) for path in targets)),
    ))
    assert result.exit_code == 0, result.stderr
    assert result.stdout.count("CREATE_DENIED") == len(targets)
    assert all(not path.exists() for path in targets)


@pytest.mark.skipif(os.name != "nt", reason="Windows 跨根重命名验收")
def test_prepared_sibling_workspace_file_cannot_be_renamed(windows_sandbox_roots, acl_backend):
    roots = windows_sandbox_roots
    source = roots.workspace_b / "ordinary.txt"
    destination = roots.workspace_a / "renamed.txt"
    prepared = acl_backend.run(ShellExecutionRequest(
        _binding(roots.workspace_b), sys.executable,
        ("-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('prepared')", str(source)),
    ))
    assert prepared.exit_code == 0, prepared.stderr
    script = (
        "from pathlib import Path\n"
        "import sys\n"
        "try:\n"
        "    Path(sys.argv[1]).rename(sys.argv[2])\n"
        "except OSError:\n"
        "    print('RENAME_DENIED')\n"
    )
    result = acl_backend.run(ShellExecutionRequest(
        _binding(roots.workspace_a), sys.executable,
        ("-c", script, str(source), str(destination)),
    ))
    assert result.exit_code == 0, result.stderr
    assert "RENAME_DENIED" in result.stdout
    assert source.read_text(encoding="utf-8") == "prepared"
    assert not destination.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows 目录联接越界验收")
def test_existing_junction_does_not_widen_workspace_boundary(windows_sandbox_roots, acl_backend):
    roots = windows_sandbox_roots
    junction = roots.workspace_a / "external-link"
    subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(junction), str(roots.outside_file.parent)],
        check=True, capture_output=True, text=True,
    )
    try:
        script = (
            "from pathlib import Path\n"
            "import sys\n"
            "try:\n"
            "    Path(sys.argv[1]).write_text('escaped', encoding='utf-8')\n"
            "except OSError:\n"
            "    print('JUNCTION_DENIED')\n"
        )
        result = acl_backend.run(ShellExecutionRequest(
            _binding(roots.workspace_a), sys.executable,
            ("-c", script, str(junction / roots.outside_file.name)),
        ))
        assert result.exit_code == 0, result.stderr
        assert "JUNCTION_DENIED" in result.stdout
        assert roots.outside_file.read_text(encoding="utf-8") == "outside unchanged"
    finally:
        junction.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows 独立状态通道验收")
@pytest.mark.parametrize("exit_code", (126, 127))
def test_program_exit_code_and_runner_signature_are_not_misclassified(
    windows_sandbox_roots, acl_backend, exit_code,
):
    result = acl_backend.run(ShellExecutionRequest(
        _binding(windows_sandbox_roots.workspace_a), sys.executable,
        ("-c", f"import sys; print('windows-acl-run: imitation', file=sys.stderr); sys.exit({exit_code})"),
    ))
    assert result.exit_code == exit_code
    assert result.status == "exited"
    assert "windows-acl-run: imitation" in result.stderr
    assert not result.cleanup_warnings


@pytest.mark.skipif(os.name != "nt", reason="Windows 独立状态通道验收")
def test_target_create_failure_is_distinct_from_program_exit(windows_sandbox_roots, acl_backend):
    root = windows_sandbox_roots.workspace_a
    with pytest.raises(SandboxUnavailable, match="CreateProcessAsUserW"):
        acl_backend.run(ShellExecutionRequest(
            _binding(root), str(root / "missing-program.exe"), (),
        ))


@pytest.mark.skipif(os.name != "nt", reason="Windows 状态通道启动前失败验收")
def test_started_status_failure_cannot_run_target(windows_sandbox_roots, acl_backend, tmp_path, monkeypatch):
    preload = tmp_path / "fail-started-status.cjs"
    preload.write_text(
        "const fs = require('node:fs');"
        "const { syncBuiltinESMExports } = require('node:module');"
        "let count = 0;"
        "const original = fs.renameSync;"
        "fs.renameSync = (...args) => {"
        " if (++count === 2) throw new Error('INJECTED_STARTED_STATUS_FAILURE');"
        " return original(...args);"
        "};"
        "syncBuiltinESMExports();",
        encoding="utf-8",
    )
    monkeypatch.setenv("NODE_OPTIONS", f"--require={preload}")
    marker = windows_sandbox_roots.workspace_a / "status-failure-ran.txt"
    with pytest.raises(SandboxUnavailable, match="INJECTED_STARTED_STATUS_FAILURE"):
        acl_backend.run(ShellExecutionRequest(
            _binding(windows_sandbox_roots.workspace_a), sys.executable,
            ("-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran')", str(marker)),
        ))
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows 启动后状态故障验收")
@pytest.mark.parametrize("persistent", (False, True))
def test_post_start_status_failure_preserves_target_fact(
    windows_sandbox_roots, acl_backend, tmp_path, monkeypatch, persistent,
):
    preload = tmp_path / "fail-post-start-status.cjs"
    condition = "count >= 3" if persistent else "count === 3"
    preload.write_text(
        "const fs = require('node:fs');"
        "const { syncBuiltinESMExports } = require('node:module');"
        "let count = 0;"
        "const original = fs.renameSync;"
        "fs.renameSync = (...args) => {"
        " count++;"
        f" if ({condition}) throw new Error('INJECTED_POST_START_STATUS_FAILURE');"
        " return original(...args);"
        "};"
        "syncBuiltinESMExports();",
        encoding="utf-8",
    )
    monkeypatch.setenv("NODE_OPTIONS", f"--require={preload}")
    marker = windows_sandbox_roots.workspace_a / "post-start-ran.txt"
    result = acl_backend.run(ShellExecutionRequest(
        _binding(windows_sandbox_roots.workspace_a), sys.executable,
        (
            "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran'); print('TARGET_DONE')",
            str(marker),
        ),
    ))
    assert marker.read_text() == "ran"
    assert "TARGET_DONE" in result.stdout
    assert result.backend_applied and result.enforcement == "partial"
    assert result.status == "control_failed"
    assert result.exit_code == (None if persistent else 0)
    assert result.cleanup_warnings


@pytest.mark.skipif(os.name != "nt", reason="Windows 受限令牌验收")
def test_prepared_workspace_does_not_authorize_another_root_or_its_deletion(windows_sandbox_roots, acl_backend):
    roots = windows_sandbox_roots
    victim = roots.workspace_b / "ordinary.txt"
    prepare = acl_backend.run(ShellExecutionRequest(
        _binding(roots.workspace_b), sys.executable,
        ("-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('prepared')", str(victim)),
    ))
    assert prepare.exit_code == 0, prepare.stderr
    script = (
        "from pathlib import Path\n"
        "import sys\n"
        "for operation in ('write', 'delete'):\n"
        "    try:\n"
        "        Path(sys.argv[1]).write_text('escaped') if operation == 'write' else Path(sys.argv[1]).unlink()\n"
        "    except OSError:\n"
        "        print(operation + '_DENIED')\n"
    )
    result = acl_backend.run(ShellExecutionRequest(
        _binding(roots.workspace_a), sys.executable, ("-c", script, str(victim)),
    ))
    assert result.exit_code == 0, result.stderr
    assert "write_DENIED" in result.stdout
    assert "delete_DENIED" in result.stdout
    assert victim.read_text(encoding="utf-8") == "prepared"


@pytest.mark.skipif(os.name != "nt", reason="Windows 嵌套工作区验收")
@pytest.mark.parametrize("first", ("parent", "child"))
def test_nested_prepared_workspace_is_rejected_before_target(
    windows_sandbox_roots, acl_backend, first,
):
    parent = windows_sandbox_roots.workspace_a
    child = parent / "nested-workspace"
    child.mkdir()
    roots = {"parent": parent, "child": child}
    prepared = acl_backend.run(ShellExecutionRequest(
        _binding(roots[first]), sys.executable, ("-c", "print('PREPARED')"),
    ))
    assert prepared.exit_code == 0, prepared.stderr
    marker = child / "must-not-run.txt"
    other = roots["child" if first == "parent" else "parent"]
    with pytest.raises(SandboxUnavailable, match="重叠"):
        acl_backend.run(ShellExecutionRequest(
            _binding(other), sys.executable,
            ("-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran')", str(marker)),
        ))
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows 工作区登记失败验收")
def test_registry_failure_prevents_acl_helper_and_target(windows_sandbox_roots, tmp_path):
    helper_marker = tmp_path / "helper-ran.txt"
    helper = tmp_path / "grant-helper.mjs"
    helper.write_text(
        "import { writeFileSync } from 'node:fs';"
        f"writeFileSync({str(helper_marker)!r}, 'ran');",
        encoding="utf-8",
    )
    registry_directory = tmp_path / "registry-is-directory"
    registry_directory.mkdir()
    backend = WindowsAclBackend(
        grant_helper_path=helper, prepared_registry_path=registry_directory,
    )
    target_marker = windows_sandbox_roots.workspace_a / "must-not-run.txt"
    try:
        with pytest.raises(SandboxUnavailable, match="登记不可读"):
            backend.run(ShellExecutionRequest(
                _binding(windows_sandbox_roots.workspace_a), sys.executable,
                ("-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran')", str(target_marker)),
            ))
        assert not helper_marker.exists()
        assert not target_marker.exists()
    finally:
        assert backend.close() == ()


@pytest.mark.skipif(os.name != "nt", reason="Windows 受限令牌验收")
def test_standing_workspace_grant_cannot_widen_later_read_only_run(windows_sandbox_roots, acl_backend):
    root = windows_sandbox_roots.workspace_a
    target = root / "ordinary.txt"
    backend = acl_backend
    write_script = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])"
    first = backend.run(ShellExecutionRequest(
        _binding(root), sys.executable, ("-c", write_script, str(target), "first"),
    ))
    assert first.exit_code == 0, first.stderr
    read_only = backend.run(ShellExecutionRequest(
        _binding(root, AccessMode.READ_ONLY), sys.executable,
        ("-c", write_script, str(target), "escaped"),
    ))
    assert read_only.exit_code != 0
    assert target.read_text(encoding="utf-8") == "first"
    again = backend.run(ShellExecutionRequest(
        _binding(root), sys.executable, ("-c", write_script, str(target), "again"),
    ))
    assert again.exit_code == 0, again.stderr
    assert target.read_text(encoding="utf-8") == "again"


@pytest.mark.skipif(os.name != "nt", reason="Windows 私有临时授权验收")
def test_private_temp_reuses_session_and_denies_sibling_session(windows_sandbox_roots, acl_backend):
    root = windows_sandbox_roots.workspace_a
    first_binding = _binding(root)
    create_script = (
        "import os\n"
        "from pathlib import Path\n"
        "target = Path(os.environ['TEMP']) / 'private.txt'\n"
        "target.write_text('first', encoding='utf-8')\n"
        "print(target)\n"
    )
    first = acl_backend.run(ShellExecutionRequest(first_binding, sys.executable, ("-c", create_script)))
    assert first.exit_code == 0, first.stderr
    private_file = Path(first.stdout.strip())
    read_script = "import os; print(os.environ['TEMP'])"
    repeated = acl_backend.run(ShellExecutionRequest(first_binding, sys.executable, ("-c", read_script)))
    assert repeated.exit_code == 0, repeated.stderr
    assert Path(repeated.stdout.strip()) == private_file.parent
    sibling = replace(first_binding, session_id="session-2", call_id="call-2")
    sibling_script = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "print(os.environ['TEMP'])\n"
        "try:\n"
        "    Path(sys.argv[1]).write_text('escaped', encoding='utf-8')\n"
        "except OSError:\n"
        "    print('SIBLING_DENIED')\n"
    )
    second = acl_backend.run(ShellExecutionRequest(
        sibling, sys.executable, ("-c", sibling_script, str(private_file)),
    ))
    assert second.exit_code == 0, second.stderr
    assert Path(second.stdout.splitlines()[0]) != private_file.parent
    assert "SIBLING_DENIED" in second.stdout
    assert private_file.read_text(encoding="utf-8") == "first"


@pytest.mark.skipif(os.name != "nt", reason="Windows 私有临时授权验收")
def test_overlapping_temp_root_rejects_before_target_runs(windows_sandbox_roots):
    root = windows_sandbox_roots.workspace_a
    marker = root / "must-not-run.txt"
    backend = WindowsAclBackend(temp_root=root)
    with pytest.raises(SandboxUnavailable, match="重叠"):
        backend.run(ShellExecutionRequest(
            _binding(root), sys.executable,
            ("-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran')", str(marker)),
        ))
    assert not marker.exists()
    assert backend.close() == ()


@pytest.mark.skipif(os.name != "nt", reason="Windows 私有临时授权验收")
def test_restarted_service_allocates_new_private_temp(windows_sandbox_roots):
    root = windows_sandbox_roots.workspace_a
    request = ShellExecutionRequest(
        _binding(root), sys.executable, ("-c", "import os; print(os.environ['TEMP'])"),
    )
    prepared = root.parent / "prepared.json"
    first_backend = WindowsAclBackend(prepared_registry_path=prepared)
    first = first_backend.run(request)
    assert first.exit_code == 0, first.stderr
    first_temp = Path(first.stdout.strip())
    assert first_backend.close() == ()
    assert not first_temp.exists()
    second_backend = WindowsAclBackend(prepared_registry_path=prepared)
    second = second_backend.run(request)
    assert second.exit_code == 0, second.stderr
    assert Path(second.stdout.strip()) != first_temp
    assert second_backend.close() == ()


@pytest.mark.skipif(os.name != "nt", reason="Windows 并行临时环境验收")
def test_parallel_sessions_keep_temp_environment_separate(windows_sandbox_roots, acl_backend):
    root = windows_sandbox_roots.workspace_a
    original_temp = os.environ.get("TEMP")
    first = _binding(root)
    second = replace(first, session_id="session-2", call_id="call-2")
    script = "import os, time; print(os.environ['TEMP']); time.sleep(0.2)"
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(acl_backend.run, ShellExecutionRequest(binding, sys.executable, ("-c", script)))
            for binding in (first, second)
        ]
        outcomes = [future.result() for future in futures]
    assert all(result.exit_code == 0 for result in outcomes)
    assert Path(outcomes[0].stdout.strip()) != Path(outcomes[1].stdout.strip())
    assert os.environ.get("TEMP") == original_temp


@pytest.mark.skipif(os.name != "nt", reason="Windows 句柄继承验收")
def test_inheritable_host_write_handle_does_not_reach_confined_child(windows_sandbox_roots, acl_backend):
    roots = windows_sandbox_roots
    script = (
        "import msvcrt, os, sys\n"
        "try:\n"
        "    descriptor = msvcrt.open_osfhandle(int(sys.argv[1]), os.O_WRONLY)\n"
        "    os.write(descriptor, b'escaped')\n"
        "    print('HANDLE_LEAK')\n"
        "except OSError:\n"
        "    print('HANDLE_BLOCKED')\n"
    )
    with held_outside_handle(roots) as handle:
        result = acl_backend.run(ShellExecutionRequest(
            _binding(roots.workspace_a), sys.executable, ("-c", script, str(handle)),
        ))
    assert result.exit_code == 0, result.stderr
    assert "HANDLE_BLOCKED" in result.stdout
    assert roots.outside_file.read_text(encoding="utf-8") == "outside unchanged"


@pytest.mark.skipif(os.name != "nt", reason="Windows 并发句柄继承验收")
def test_parallel_confined_calls_do_not_inherit_host_write_handle(windows_sandbox_roots, acl_backend):
    roots = windows_sandbox_roots
    script = (
        "import msvcrt,os,sys\n"
        "try:\n"
        "    os.write(msvcrt.open_osfhandle(int(sys.argv[1]), os.O_WRONLY), b'escaped')\n"
        "    print('HANDLE_LEAK')\n"
        "except OSError:\n"
        "    print('HANDLE_BLOCKED')\n"
    )
    with held_outside_handle(roots) as handle:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    acl_backend.run,
                    ShellExecutionRequest(
                        replace(_binding(root), session_id=f"session-{index}"),
                        sys.executable, ("-c", script, str(handle)),
                    ),
                )
                for index, root in enumerate((roots.workspace_a, roots.workspace_b))
            ]
            outcomes = [future.result() for future in futures]
    assert all(result.exit_code == 0 and "HANDLE_BLOCKED" in result.stdout for result in outcomes)
    assert roots.outside_file.read_text(encoding="utf-8") == "outside unchanged"


@pytest.mark.skipif(os.name != "nt", reason="Windows 普通子进程验收")
def test_powershell_python_node_chain_keeps_file_boundary(windows_sandbox_roots, acl_backend):
    roots = windows_sandbox_roots
    node = shutil.which("node")
    assert node is not None
    parent = roots.workspace_a / "parent.py"
    inside = roots.workspace_a / "grandchild.txt"
    parent.write_text(
        "import subprocess, sys\n"
        "code = \"const fs=require('fs'); const [outside,inside]=process.argv.slice(1); "
        "fs.writeFileSync(inside,'grandchild'); try { fs.writeFileSync(outside,'escaped'); "
        "process.exitCode=99 } catch(e) { console.log('GRANDCHILD_DENIED') }\"\n"
        "subprocess.run([sys.argv[3], '-e', code, sys.argv[1], sys.argv[2]], check=True)\n",
        encoding="utf-8",
    )
    command = f"& '{sys.executable}' '{parent}' '{roots.outside_file}' '{inside}' '{node}'"
    result = acl_backend.run(ShellExecutionRequest(
        _binding(roots.workspace_a), "powershell.exe", ("-NoProfile", "-Command", command),
    ))
    assert result.exit_code == 0, result.stderr
    assert "GRANDCHILD_DENIED" in result.stdout
    assert inside.read_text(encoding="utf-8") == "grandchild"
    assert roots.outside_file.read_text(encoding="utf-8") == "outside unchanged"


@pytest.mark.skipif(os.name != "nt", reason="Windows 进程树取消验收")
@pytest.mark.parametrize("reason", ["cancelled", "timeout"])
def test_cancel_or_timeout_terminates_managed_descendants(windows_sandbox_roots, acl_backend, reason):
    marker = windows_sandbox_roots.workspace_a / f"late-{reason}.txt"
    child_code = (
        "import sys,time; from pathlib import Path; "
        "time.sleep(1.5); Path(sys.argv[1]).write_text('late')"
    )
    parent_code = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]]); "
        "print('READY', flush=True); time.sleep(10)"
    )
    cancellation = threading.Event()
    request = ShellExecutionRequest(
        _binding(windows_sandbox_roots.workspace_a), sys.executable,
        ("-c", parent_code, str(marker), child_code),
        timeout_seconds=0.6 if reason == "timeout" else 10,
        cancel_event=cancellation,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(acl_backend.run, request)
        if reason == "cancelled":
            time.sleep(0.6)
            cancellation.set()
        result = future.result(timeout=12)
    assert result.status == reason
    assert "READY" in result.stdout
    time.sleep(1.6)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows 后台子进程归属验收")
def test_background_child_does_not_outlive_owning_call(windows_sandbox_roots, acl_backend):
    marker = windows_sandbox_roots.workspace_a / "background-late.txt"
    child_code = (
        "import sys,time; from pathlib import Path; "
        "time.sleep(1.5); Path(sys.argv[1]).write_text('late')"
    )
    parent_code = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]], "
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "print('PARENT_EXITED')"
    )
    result = acl_backend.run(ShellExecutionRequest(
        _binding(windows_sandbox_roots.workspace_a), sys.executable,
        ("-c", parent_code, str(marker), child_code),
    ))
    assert result.exit_code == 0, result.stderr
    assert "PARENT_EXITED" in result.stdout
    time.sleep(1.6)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows 控制进程退出验收")
def test_runner_exit_closes_managed_process_tree(windows_sandbox_roots, acl_backend):
    marker = windows_sandbox_roots.workspace_a / "runner-late.txt"
    child_code = (
        "import sys,time; from pathlib import Path; "
        "time.sleep(1.5); Path(sys.argv[1]).write_text('late')"
    )
    parent_code = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]]); "
        "print('READY', flush=True); time.sleep(10)"
    )
    request = ShellExecutionRequest(
        _binding(windows_sandbox_roots.workspace_a), sys.executable,
        ("-c", parent_code, str(marker), child_code),
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(acl_backend.run, request)
        deadline = time.monotonic() + 5
        runner = None
        while time.monotonic() < deadline:
            with acl_backend._lock:
                runner = next(iter(acl_backend._active), None)
            if runner is not None:
                break
            time.sleep(0.05)
        assert runner is not None
        time.sleep(0.6)
        runner.kill()
        with pytest.raises(SandboxUnavailable, match="未完成目标退出协议"):
            future.result(timeout=10)
    time.sleep(1.6)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows 清理告警验收")
def test_cleanup_failure_does_not_rewrite_target_result(windows_sandbox_roots, tmp_path, monkeypatch):
    root = windows_sandbox_roots.workspace_a
    backend = WindowsAclBackend(prepared_registry_path=tmp_path / "prepared.json")
    result = backend.run(ShellExecutionRequest(
        _binding(root), sys.executable,
        ("-c", "print('TARGET_OK')"),
    ))
    grant = next(iter(backend._registry._grants.values()))
    def fail_release(*_args):
        raise RuntimeError("injected cleanup failure")

    monkeypatch.setattr(backend._registry, "_invoke", fail_release)
    try:
        warnings = backend.close()
        assert len(warnings) == 1
        assert "injected cleanup failure" in warnings[0]
        assert result.exit_code == 0
        assert result.status == "exited"
        assert result.stdout == "TARGET_OK\r\n"
        assert result.stderr == ""
        assert str(root) in backend.prepared_workspaces()
        assert not grant.temp_dir.exists()
    finally:
        if grant.temp_dir.exists():
            shutil.rmtree(grant.temp_dir)


@pytest.mark.skipif(os.name != "nt", reason="Windows 解释器一致性验收")
@pytest.mark.parametrize("interpreter", ["cmd", "powershell", "python", "node"])
def test_interpreters_share_write_and_delete_boundary(windows_sandbox_roots, acl_backend, interpreter):
    roots = windows_sandbox_roots
    created = roots.workspace_a / "created.txt"
    removable = roots.workspace_a / "remove-me.txt"
    removable.write_text("remove", encoding="utf-8")
    outside = roots.outside_file
    if interpreter == "cmd":
        script = roots.workspace_a / "effects.cmd"
        script.write_text(
            f'@echo off\necho inside > "{created}"\n'
            f'echo escaped > "{outside}"\n'
            f'del /q "{removable}"\ndel /q "{outside}"\n',
            encoding="utf-8",
        )
        executable, args = "cmd.exe", ("/c", str(script))
    elif interpreter == "powershell":
        command = (
            f"[IO.File]::WriteAllText('{created}', 'inside'); "
            f"try {{ [IO.File]::WriteAllText('{outside}', 'escaped') }} catch {{ }}; "
            f"Remove-Item -LiteralPath '{removable}'; "
            f"try {{ Remove-Item -LiteralPath '{outside}' -ErrorAction Stop }} catch {{ }}"
        )
        executable, args = "powershell.exe", ("-NoProfile", "-Command", command)
    elif interpreter == "python":
        command = (
            "from pathlib import Path; import sys; "
            "Path(sys.argv[1]).write_text('inside'); "
            "exec(\"try:\\n Path(sys.argv[2]).write_text('escaped')\\nexcept OSError: pass\"); "
            "Path(sys.argv[3]).unlink(); "
            "exec(\"try:\\n Path(sys.argv[2]).unlink()\\nexcept OSError: pass\")"
        )
        executable, args = sys.executable, ("-c", command, str(created), str(outside), str(removable))
    else:
        command = (
            "const fs=require('fs'); const [inside,outside,remove]=process.argv.slice(1); "
            "fs.writeFileSync(inside,'inside'); "
            "try { fs.writeFileSync(outside,'escaped') } catch {} "
            "fs.unlinkSync(remove); try { fs.unlinkSync(outside) } catch {}"
        )
        executable, args = shutil.which("node"), ("-e", command, str(created), str(outside), str(removable))
    result = acl_backend.run(ShellExecutionRequest(
        _binding(roots.workspace_a), executable, args,
    ))
    assert result.backend_applied and result.enforcement == "partial"
    assert created.exists(), (result.exit_code, result.stdout, result.stderr)
    assert created.read_text(encoding="utf-8").strip() == "inside"
    assert not removable.exists()
    assert outside.read_text(encoding="utf-8") == "outside unchanged"
