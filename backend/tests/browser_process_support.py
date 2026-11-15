"""Own native browser hosts for one test, including detached Gateway children.

Production disconnect deliberately preserves windows. Tests instead stage hosts
in their own temporary directory and reap those hosts after all local finalizers.
"""
import ctypes
import os
import subprocess
from pathlib import Path

import psutil
import pytest


def close_browser_windows(processes):
    """Ask owned native windows to close, letting WebView shut down normally."""
    if os.name != 'nt' or not processes:
        return False
    from ctypes import wintypes

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    owned = {process.pid: process for process in processes}
    posted = False

    @callback_type
    def close(hwnd, _):
        nonlocal posted
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = owned.get(pid.value)
        if process and process.is_running() and user32.IsWindowVisible(hwnd):
            posted = bool(user32.PostMessageW(hwnd, 0x0010, 0, 0)) or posted  # WM_CLOSE
        return True

    user32.EnumWindows(close, 0)
    return posted


def reap_browser_processes(root, direct=()):
    """Reap only isolated host images or directly captured test children."""
    root = Path(root).resolve()
    owned = set(direct)
    if any(root.rglob('distinct-browser.exe')):
        for process in psutil.process_iter(['name', 'exe']):
            try:
                if ((process.info['name'] or '').lower() == 'distinct-browser.exe'
                        and process.info['exe']
                        and Path(process.info['exe']).resolve().is_relative_to(root)):
                    owned.add(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    # Capture WebView descendants before terminating a parent. psutil retains
    # creation times and checks for PID reuse before sending a kill.
    tree = dict.fromkeys(owned)
    for process in owned:
        try:
            tree.update(dict.fromkeys(process.children(recursive=True)))
        except psutil.NoSuchProcess:
            pass
    closed_windows = close_browser_windows(owned)
    _, alive = psutil.wait_procs(tree, timeout=3 if closed_windows else .5)
    # Stop parents before descendants so they cannot spawn replacement workers.
    for process in tree:
        if process not in alive:
            continue
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=10)
    assert not alive, f'Test browser processes did not exit: {[item.pid for item in alive]}'


@pytest.fixture(autouse=True)
def isolated_browser_processes(request, monkeypatch):
    if os.name != 'nt':
        yield
        return

    from backend.app.desktop.browser.transport import BrowserTransport

    root = request.getfixturevalue('tmp_path')
    monkeypatch.setenv('FOCUS_DISTINCT_BROWSER_CACHE', str(root / 'browser-runtime'))
    direct = set()
    start = BrowserTransport.start
    process_type = subprocess.Popen

    async def tracked_start(transport):
        try:
            return await start(transport)
        finally:
            process = transport.process
            if isinstance(process, process_type) and process.poll() is None:
                try:
                    direct.add(psutil.Process(process.pid))
                except psutil.NoSuchProcess:
                    pass

    monkeypatch.setattr(BrowserTransport, 'start', tracked_start)
    try:
        yield
    finally:
        reap_browser_processes(root, direct)
