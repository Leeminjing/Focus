"""Real Windows transport/OS boundary for conservative dead-endpoint cleanup."""
import asyncio
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from backend.app.desktop.browser.transport import BrowserError, BrowserTransport

pytestmark = pytest.mark.skipif(os.name != 'nt' or os.getenv('FOCUS_BROWSER_RECOVERY_REAL') != '1',
    reason='opt-in Windows recovery EXE')


def transport(cache, **options):
    return BrowserTransport(os.environ['FOCUS_BROWSER_RECOVERY_EXE'], recovery=True, cache_root=cache, **options)


def restore_options(source):
    return dict(endpoint=source.endpoint, expected_host_id=source.host_id, artifact_sha256=source.artifact_sha256)


def terminate_owned(source):
    from backend.app.desktop.browser.win32_pipe import K, private_file
    descriptor = private_file(Path(source.endpoint))
    process = K.OpenProcess(0x101001, False, source.pid)
    assert process and descriptor['pid'] == source.pid
    try:
        image, size = C.create_unicode_buffer(32768), W.DWORD(32768)
        times = [C.c_ulonglong() for _ in range(4)]
        assert K.QueryFullProcessImageNameW(process, 0, image, C.byref(size))
        assert Path(image.value).resolve() == (Path(source.cache_root) / source.artifact_sha256 / 'distinct-browser.exe').resolve()
        assert K.GetProcessTimes(process, *(C.byref(item) for item in times))
        assert str(times[0].value) == descriptor['process_created']
        K.TerminateProcess.argtypes = [W.HANDLE, W.UINT]; K.TerminateProcess.restype = W.BOOL
        assert K.TerminateProcess(process, 0) and K.WaitForSingleObject(process, 10000) == 0
    finally:
        K.CloseHandle(process)


@pytest.mark.parametrize('unrelated_file', [False, True])
def test_failed_reconnect_removes_only_verified_dead_endpoint(tmp_path, unrelated_file):
    async def scenario():
        first, live = transport(tmp_path / 'cache'), transport(tmp_path / 'cache')
        recovery = None
        try:
            await first.call('create', label='Owned dead-endpoint regression')
            other = await live.call('create', label='Owned live-neighbour regression')
            endpoint, options = Path(first.endpoint), restore_options(first)
            image = Path(first.cache_root) / first.artifact_sha256 / 'distinct-browser.exe'
            sentinel = endpoint.parent / 'unrelated.txt'
            if unrelated_file: sentinel.write_text('retain this file', encoding='utf-8')
            terminate_owned(first)
            await first.disconnect()
            assert endpoint.exists()  # Forced exit leaves the actual native descriptor.
            from backend.app.desktop.browser.win32_pipe import ReconnectClient
            with pytest.raises(ConnectionError):
                await asyncio.to_thread(ReconnectClient, endpoint, image)  # No saved identity pins: read-only failure.
            assert endpoint.exists()
            with pytest.raises(ConnectionError):
                await asyncio.to_thread(ReconnectClient, endpoint, image, expected_sha256=first.artifact_sha256,
                                        expected_host_id='wrong-host')
            assert endpoint.exists()
            alternate = endpoint.with_name('retained.json')
            endpoint.rename(alternate)
            try:
                with pytest.raises(ConnectionError):
                    await asyncio.to_thread(ReconnectClient, alternate, image, expected_sha256=first.artifact_sha256,
                                            expected_host_id=first.host_id)
                assert alternate.exists()  # Private files outside the fixed endpoint name are never removed.
            finally:
                if alternate.exists(): alternate.rename(endpoint)
            if not unrelated_file:
                blocked = transport(tmp_path / 'cache', **options)
                try:
                    with endpoint.open('rb'):  # Another reader denies DELETE sharing.
                        with pytest.raises(BrowserError) as error: await blocked.start()
                        assert error.value.code == 'host_unrecoverable' and endpoint.exists()
                finally:
                    await blocked.disconnect()
            recovery = transport(tmp_path / 'cache', **options)
            with pytest.raises(BrowserError) as error: await recovery.start()
            assert error.value.code == 'host_unrecoverable'
            assert not endpoint.exists(), 'Failed recovery must retire its verified dead descriptor'
            assert endpoint.parent.exists() is unrelated_file
            if unrelated_file: assert sentinel.read_text(encoding='utf-8') == 'retain this file'
            assert image.is_file() and Path(live.endpoint).is_file()
            assert (await live.call('snapshot'))['browsers'][0]['browser_id'] == other['browser_id']
        finally:
            await first.disconnect()
            if recovery: await recovery.disconnect()
            if live.pid and not live.failure:
                for item in (await live.call('snapshot'))['browsers']:
                    await live.call('close', browser_id=item['browser_id'])
            await live.disconnect()
    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['busy', 'wrong_host', 'wrong_checksum', 'changed_birth', 'dead_pid_live_pipe', 'broadened_acl', 'junction'])
def test_rejected_reconnect_preserves_live_or_untrusted_endpoint(tmp_path, case):
    async def scenario():
        first = transport(tmp_path / 'cache')
        rejected, original, alias, endpoint = None, None, None, None
        child = None
        acl_changed = False
        flags = subprocess.CREATE_NO_WINDOW
        try:
            created = await first.call('create', label='Owned cleanup boundary ' + case)
            endpoint = Path(first.endpoint)
            original = endpoint.read_bytes()
            options = restore_options(first)
            if case == 'wrong_host': options['expected_host_id'] = 'wrong-host'
            elif case == 'wrong_checksum': options['artifact_sha256'] = 'f' * 64
            elif case in {'changed_birth', 'dead_pid_live_pipe'}:
                description = json.loads(original)
                if case == 'changed_birth': description['process_created'] = str(int(description['process_created']) + 1)
                else:
                    child = subprocess.Popen([sys.executable, '-c', 'pass'], stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
                    child.wait(5)
                    description['pid'] = child.pid  # Dead PID, but the original native pipe is still alive.
                endpoint.write_text(json.dumps(description), encoding='utf-8')
            elif case == 'broadened_acl':
                subprocess.run(['icacls', str(endpoint), '/grant', '*S-1-5-32-545:(R)'],
                    check=True, capture_output=True, creationflags=flags)
                acl_changed = True
            elif case == 'junction':
                alias = tmp_path / 'endpoint-junction'
                subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-Command',
                    "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:FOCUS_CLEANUP_LINK -Target $env:FOCUS_CLEANUP_TARGET | Out-Null"],
                    env={**os.environ, 'FOCUS_CLEANUP_LINK': str(alias), 'FOCUS_CLEANUP_TARGET': str(endpoint.parent)},
                    check=True, capture_output=True, creationflags=flags)
                options['endpoint'] = alias / 'endpoint.json'
            rejected = transport(tmp_path / 'cache', **options)
            with pytest.raises(BrowserError) as error: await rejected.start()
            assert error.value.code == ('connection_busy' if case == 'busy' else 'host_unrecoverable')
            assert endpoint.exists() and endpoint.parent.exists()
            assert (await first.call('snapshot'))['browsers'][0]['browser_id'] == created['browser_id']
        finally:
            if rejected: await rejected.disconnect()
            if endpoint and endpoint.exists() and original is not None:
                if acl_changed:
                    subprocess.run(['icacls', str(endpoint), '/remove:g', '*S-1-5-32-545'],
                        check=True, capture_output=True, creationflags=flags)
                endpoint.write_bytes(original)
            if alias and alias.exists(): alias.rmdir()  # Remove only this directory junction, never its target.
            if first.pid and not first.failure:
                for item in (await first.call('snapshot'))['browsers']:
                    await first.call('close', browser_id=item['browser_id'])
            await first.disconnect()
    asyncio.run(scenario())
