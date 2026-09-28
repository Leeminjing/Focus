"""本文件对外提供 WindowsSandboxRoots、windows_sandbox_roots 和 held_outside_handle。

输入为系统临时根；它必须位于 NTFS，且不使用硬链接或额外特殊授权。
输出为两个独立工作区、两个会话临时目录、一个外部普通文件及可控的宿主可写句柄。
具体工作流为核对磁盘类型，创建互不重叠的真实目录和文件，按需打开可继承句柄，
在退出句柄上下文时释放该句柄。
示例：with held_outside_handle(windows_sandbox_roots) as handle: assert handle > 0。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest


@dataclass(frozen=True)
class WindowsSandboxRoots:
    workspace_a: Path
    workspace_b: Path
    temp_root: Path
    session_a_temp: Path
    session_b_temp: Path
    outside_file: Path


def _filesystem_name(path: Path) -> str:
    volume = ctypes.create_unicode_buffer(64)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        str(Path(path.anchor)), None, 0, None, None, None, volume, len(volume)
    )
    if not ok:
        raise ctypes.WinError()
    return volume.value


def _create_roots(base: Path) -> WindowsSandboxRoots:
    workspace_a = base / "workspace-a"
    workspace_b = base / "workspace-b"
    temp_root = base / "private-temp"
    session_a_temp = temp_root / "session-a"
    session_b_temp = temp_root / "session-b"
    outside_file = base / "outside" / "ordinary.txt"
    for directory in (workspace_a, workspace_b, session_a_temp, session_b_temp, outside_file.parent):
        directory.mkdir(parents=True)
    outside_file.write_text("outside unchanged", encoding="utf-8")
    for workspace in (workspace_a, workspace_b):
        (workspace / "ordinary.txt").write_text("workspace unchanged", encoding="utf-8")
    return WindowsSandboxRoots(
        workspace_a.resolve(), workspace_b.resolve(), temp_root.resolve(),
        session_a_temp.resolve(), session_b_temp.resolve(), outside_file.resolve(),
    )


def _assert_independent(roots: WindowsSandboxRoots) -> None:
    directories = (
        roots.workspace_a, roots.workspace_b, roots.session_a_temp,
        roots.session_b_temp, roots.outside_file.parent,
    )
    for index, left in enumerate(directories):
        for right in directories[index + 1:]:
            assert not left.is_relative_to(right)
            assert not right.is_relative_to(left)
    files = (roots.workspace_a / "ordinary.txt", roots.workspace_b / "ordinary.txt", roots.outside_file)
    assert all(path.stat().st_nlink == 1 for path in files)
    assert len({(path.stat().st_dev, path.stat().st_ino) for path in files}) == len(files)


@pytest.fixture
def windows_sandbox_roots() -> Iterator[WindowsSandboxRoots]:
    if os.name != "nt":
        pytest.skip("真实 Windows 文件沙箱验收需要 Windows")
    temp_root = Path(tempfile.gettempdir()).resolve()
    if _filesystem_name(temp_root) != "NTFS":
        pytest.skip("普通边界验收需要 NTFS")
    base = (temp_root / f"focus-acl-{uuid.uuid4().hex}").resolve()
    base.mkdir()
    try:
        roots = _create_roots(base)
        _assert_independent(roots)
        yield roots
    finally:
        if base.parent != temp_root or not base.name.startswith("focus-acl-"):
            raise RuntimeError("拒绝清理非测试临时目录")
        shutil.rmtree(base)


@contextmanager
def held_outside_handle(roots: WindowsSandboxRoots) -> Iterator[int]:
    import msvcrt

    descriptor = os.open(roots.outside_file, os.O_RDWR | os.O_BINARY)
    handle = msvcrt.get_osfhandle(descriptor)
    os.set_handle_inheritable(handle, True)
    try:
        yield handle
    finally:
        os.close(descriptor)
