"""本文件对外提供 Windows 沙箱测试夹具的自检用例。

输入为 NTFS 上的两个工作区、两个会话临时目录、外部普通文件和可控宿主句柄。
输出为目录彼此独立、普通文件无硬链接别名、可写宿主句柄可继承且退出后关闭的证据。
具体工作流为创建夹具，核对实际路径和文件身份，再持有并释放外部文件句柄。
示例：运行 python -m pytest backend/tests/test_windows_sandbox_fixture.py。
"""

import os

import pytest

from windows_sandbox_fixture import held_outside_handle, windows_sandbox_roots


@pytest.mark.skipif(os.name != "nt", reason="真实 Windows 文件效果验收")
def test_roots_are_separate_and_files_have_distinct_identities(windows_sandbox_roots):
    roots = windows_sandbox_roots
    assert roots.workspace_a != roots.workspace_b
    assert roots.temp_root not in roots.workspace_a.parents
    assert roots.temp_root not in roots.workspace_b.parents
    assert roots.outside_file.read_text(encoding="utf-8") == "outside unchanged"
    assert roots.outside_file.stat().st_nlink == 1


@pytest.mark.skipif(os.name != "nt", reason="真实 Windows 文件效果验收")
def test_host_writable_handle_is_controlled(windows_sandbox_roots):
    with held_outside_handle(windows_sandbox_roots) as handle:
        assert handle > 0
        assert os.get_handle_inheritable(handle)
    assert windows_sandbox_roots.outside_file.read_text(encoding="utf-8") == "outside unchanged"
