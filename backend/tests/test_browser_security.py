"""Focus/v0.6.1 OS boundary; automated protocol input is not physical acceptance."""
import asyncio
import ctypes as C
import json
import os
from pathlib import Path
import subprocess

import pytest

from backend.app.desktop.browser.transport import BrowserError, BrowserTransport

pytestmark = pytest.mark.skipif(os.name != 'nt' or os.getenv('FOCUS_BROWSER_RECOVERY_REAL') != '1',
    reason='opt-in Windows native recovery artifact')


@pytest.mark.parametrize('case', ['missing_user', 'missing_logon', 'wrong_user', 'wrong_logon', 'peer_account', 'peer_logon'])
def test_identity_rejection_preserves_endpoint_and_original_page(tmp_path, monkeypatch, case):
    async def scenario():
        from backend.app.desktop.browser import win32_pipe as codec
        first = BrowserTransport(os.environ['FOCUS_BROWSER_RECOVERY_EXE'], recovery=True, cache_root=tmp_path / 'cache')
        restored = rejected = None
        original = endpoint = None
        try:
            created = await first.call('create', label='Owned v0.6.1 identity boundary')
            endpoint = Path(first.endpoint)
            original = endpoint.read_bytes()
            await first.disconnect()
            if case.startswith(('missing_', 'wrong_')):
                descriptor = json.loads(original)
                field = 'user_sid' if case.endswith('user') else 'logon_sid'
                if case.startswith('missing_'): descriptor.pop(field)
                else: descriptor[field] = 'S-1-5-18'
                endpoint.write_text(json.dumps(descriptor), encoding='utf-8')
            options = dict(endpoint=endpoint, expected_host_id=first.host_id,
                           artifact_sha256=first.artifact_sha256, recovery=True, cache_root=tmp_path / 'cache')
            checked_bytes = endpoint.read_bytes()
            with monkeypatch.context() as scoped:
                if case.startswith('peer_'):
                    # Keep descriptor and caller real; vary only the OS-token
                    # identity returned for the pinned process, before pipe I/O.
                    token_information, sid_text = codec.token_information, codec.sid_text
                    changed = set()
                    kind = 1 if case == 'peer_account' else 28
                    def token_info(requested, process=None):
                        buffer = token_information(requested, process)
                        if process is not None and requested == kind:
                            sid = (codec.SID_ENTRY.from_buffer(buffer).sid if kind == 1
                                   else codec.GROUPS.from_buffer(buffer).groups[0].sid)
                            changed.add(sid)
                        return buffer
                    scoped.setattr(codec, 'token_information', token_info)
                    scoped.setattr(codec, 'sid_text', lambda sid: 'S-1-5-18' if sid in changed else sid_text(sid))
                rejected = BrowserTransport(**options)
                with pytest.raises(BrowserError) as error: await rejected.start()
                assert error.value.code == 'host_unrecoverable'
                assert rejected.process is None and rejected._native is None
                if case.startswith(('missing_', 'wrong_')): assert '显式更换' in str(error.value)
            assert endpoint.read_bytes() == checked_bytes
            endpoint.write_bytes(original)
            restored = BrowserTransport(**options)
            page = (await restored.call('snapshot'))['browsers'][0]
            assert page['browser_id'] == created['browser_id'] and page['selected_page_id'] == created['page_id']
            assert page['pages'][0]['hwnd'] == created['hwnd'] and page['state'] == 'manual'
        finally:
            if endpoint and endpoint.exists() and original: endpoint.write_bytes(original)
            for owner in (rejected, restored, first):
                if owner is restored and owner._native and not owner.failure:
                    for page in (await owner.call('snapshot'))['browsers']:
                        await owner.call('close', browser_id=page['browser_id'])
                if owner: await owner.disconnect()
    asyncio.run(scenario())


def test_legacy_logon_acl_is_rejected_even_for_dead_host_and_not_cleaned(tmp_path):
    async def scenario():
        from backend.app.desktop.browser import win32_pipe as codec
        from backend.tests.test_browser_endpoint_cleanup import terminate_owned
        first = BrowserTransport(os.environ['FOCUS_BROWSER_RECOVERY_EXE'], recovery=True, cache_root=tmp_path / 'cache')
        endpoint = original = None
        try:
            await first.call('create', label='Owned legacy endpoint cleanup refusal')
            endpoint = Path(first.endpoint)
            original = endpoint.read_bytes()
            terminate_owned(first)
            await first.disconnect()
            account = codec.token_information(1)
            session = codec.logon()
            user = codec.sid_text(codec.SID_ENTRY.from_buffer(account).sid)
            logon = codec.sid_text(codec.GROUPS.from_buffer(session).groups[0].sid)
            def acl(*args):
                subprocess.run(['icacls', str(endpoint), *args], check=True, capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            # Account owns this exact test file. Emulate v0.6.0's protected,
            # single logon ACE, preserving its original bytes and directory.
            acl('/grant:r', f'*{logon}:(F)')
            try:
                acl('/remove:g', '*' + user)
                with pytest.raises(BrowserError, match='显式更换') as error:
                    codec.ReconnectClient(endpoint, Path(first.cache_root) / first.artifact_sha256 / 'distinct-browser.exe',
                        expected_host_id=first.host_id, expected_sha256=first.artifact_sha256)
                assert error.value.code == 'host_unrecoverable'
                assert endpoint.read_bytes() == original and endpoint.parent.exists()
            finally:
                acl('/grant:r', f'*{user}:(F)')
                acl('/remove:g', '*' + logon)
            # The same confirmed-dead endpoint is eligible only after restoring
            # the account ACL; normal conservative cleanup remains intact.
            with pytest.raises(ConnectionError):
                codec.ReconnectClient(endpoint, Path(first.cache_root) / first.artifact_sha256 / 'distinct-browser.exe',
                    expected_host_id=first.host_id, expected_sha256=first.artifact_sha256)
            assert not endpoint.exists()
        finally:
            await first.disconnect()
    asyncio.run(scenario())


@pytest.mark.skipif(not os.getenv('FOCUS_BROWSER_LEGACY_EXE'), reason='optional pinned v0.6.0 native fixture')
def test_actual_v060_host_is_refused_without_replacing_its_page(tmp_path):
    """A raw owner-only fixture creates a real old page; Focus never connects.

    The raw peer deliberately lacks modern validation and is not reusable client
    code. Its target is pinned by old artifact hash and our own Popen PID.
    """
    import base64
    import hashlib
    from backend.app.desktop.browser import win32_pipe as codec
    from backend.app.desktop.browser.host_artifact import stage
    legacy = Path(os.environ['FOCUS_BROWSER_LEGACY_EXE']).resolve()
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == 'd578f6b1cf66dcb861bbde97bd9b8bab79b9014d557a0bc1ddc0dd888684dd31'
    image, digest = stage(legacy, tmp_path / 'cache')
    process = subprocess.Popen([str(image), '--reconnect'], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    handle, buffer, serial = None, b'', 0
    endpoint = original = created = None
    def write(message):
        data = (json.dumps(message) + '\n').encode()
        while data:
            size = codec.W.DWORD()
            content = C.create_string_buffer(data)
            codec.checked(codec.K.WriteFile(handle, content, len(data), C.byref(size), None))
            assert size.value
            data = data[size.value:]
    def read():
        nonlocal buffer
        while b'\n' not in buffer:
            size, content = codec.W.DWORD(), C.create_string_buffer(65536)
            codec.checked(codec.K.ReadFile(handle, content, len(content), C.byref(size), None))
            assert size.value
            buffer += content.raw[:size.value]
        line, buffer = buffer.split(b'\n', 1)
        return json.loads(line)
    def call(method, **params):
        nonlocal serial
        serial += 1
        write(dict(id=str(serial), method=method, params=params))
        while True:
            response = read()
            if response.get('id') == str(serial):
                assert response['ok'], response
                return response['result']
    try:
        bootstrap = json.loads(process.stdout.readline())
        assert bootstrap['pid'] == process.pid
        endpoint = Path(bootstrap['endpoint'])
        original = endpoint.read_bytes()
        description = json.loads(original)
        handle = codec.K.CreateFileW(description['pipe_name'], 0x0012018b, 0, None, 3, 0, None)
        assert handle != codec.INVALID
        pid = codec.W.ULONG()
        codec.checked(codec.K.GetNamedPipeServerProcessId(handle, C.byref(pid)))
        assert pid.value == process.pid
        data = base64.b64decode(description['credential_dpapi'], validate=True)
        protected = C.create_string_buffer(data)
        encrypted, plain = codec.BLOB(len(data), C.addressof(protected)), codec.BLOB()
        codec.checked(codec.D.CryptUnprotectData(C.byref(encrypted), None, None, None, None, 1, C.byref(plain)))
        try:
            write(dict(host_id=bootstrap['host_id'], credential=C.string_at(plain.data, plain.size).decode('ascii'), protocol_version=1))
        finally: codec.K.LocalFree(plain.data)
        assert read()['event'] == 'ready'
        created = call('create', label='Owned actual v0.6.0 migration refusal')
        # Refusal happens before opening the busy old pipe. No focus process or
        # replacement browser is created and the old owner stays operational.
        async def reject():
            owner = BrowserTransport(recovery=True, cache_root=tmp_path / 'cache', endpoint=endpoint,
                expected_host_id=bootstrap['host_id'], artifact_sha256=digest)
            try:
                with pytest.raises(BrowserError, match='显式更换') as error: await owner.start()
                assert error.value.code == 'host_unrecoverable'
                assert owner.process is None and owner._native is None
            finally: await owner.disconnect()
        asyncio.run(reject())
        assert endpoint.read_bytes() == original
        current, current_digest = stage(Path(os.environ['FOCUS_BROWSER_RECOVERY_EXE']), tmp_path / 'cache')
        assert current != image and current_digest != digest
        assert image.is_file() and hashlib.sha256(image.read_bytes()).hexdigest() == digest
        assert stage(None, tmp_path / 'cache', digest) == (image, digest)  # Old cache survives for explicit rollback.
        snapshot = call('snapshot')['browsers']
        assert len(snapshot) == 1 and snapshot[0]['browser_id'] == created['browser_id']
        assert snapshot[0]['pages'][0]['hwnd'] == created['hwnd']
        call('close', browser_id=created['browser_id'])
        created = None
    finally:
        if handle is not None and handle != codec.INVALID:
            if created and process.poll() is None: call('close', browser_id=created['browser_id'])
            codec.K.CloseHandle(handle)
        if process.poll() is None:
            try: process.wait(8)
            except subprocess.TimeoutExpired: process.terminate(); process.wait(5)
        process.stdout.close(); process.stderr.close()
