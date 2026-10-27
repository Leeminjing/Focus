"""Windows codec derived from distinct-browser e0222b2 (v0.6.1) demo/reconnect_client.py.

The caller persists the bootstrap endpoint path and supplies its trusted EXE.
No lease, action, or failed request is ever retried by this client.
"""
from concurrent.futures import Future
from contextlib import contextmanager
import base64
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import queue
import threading
import time
import uuid
from .errors import BrowserError

K, A, D = C.WinDLL('kernel32', use_last_error=True), C.WinDLL('advapi32', use_last_error=True), C.WinDLL('crypt32', use_last_error=True)
INVALID = C.c_void_p(-1).value


class OVERLAPPED(C.Structure):
    _fields_ = [('Internal', C.c_size_t), ('InternalHigh', C.c_size_t), ('Offset', W.DWORD), ('OffsetHigh', W.DWORD), ('hEvent', W.HANDLE)]


class BLOB(C.Structure):
    _fields_ = [('size', W.DWORD), ('data', C.c_void_p)]


class SID_ENTRY(C.Structure):
    _fields_ = [('sid', C.c_void_p), ('attributes', W.DWORD)]


class GROUPS(C.Structure):
    _fields_ = [('count', W.DWORD), ('groups', SID_ENTRY * 1)]


for library, name, args, result in [
    (K, 'CreateFileW', [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE),
    (K, 'CloseHandle', [W.HANDLE], W.BOOL),
    (K, 'CreateEventW', [C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE),
    (K, 'ReadFile', [W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p], W.BOOL),
    (K, 'WriteFile', [W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p], W.BOOL),
    (K, 'WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD),
    (K, 'CancelIoEx', [W.HANDLE, C.c_void_p], W.BOOL),
    (K, 'GetOverlappedResult', [W.HANDLE, C.POINTER(OVERLAPPED), C.POINTER(W.DWORD), W.BOOL], W.BOOL),
    (K, 'OpenProcess', [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
    (K, 'GetCurrentProcess', [], W.HANDLE),
    (K, 'GetProcessTimes', [W.HANDLE, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p], W.BOOL),
    (K, 'QueryFullProcessImageNameW', [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)], W.BOOL),
    (K, 'GetNamedPipeServerProcessId', [W.HANDLE, C.POINTER(W.ULONG)], W.BOOL),
    (K, 'GetFileInformationByHandle', [W.HANDLE, C.c_void_p], W.BOOL),
    (K, 'GetFinalPathNameByHandleW', [W.HANDLE, W.LPWSTR, W.DWORD, W.DWORD], W.DWORD),
    (K, 'SetFileInformationByHandle', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
    (K, 'WaitNamedPipeW', [W.LPCWSTR, W.DWORD], W.BOOL),
    (K, 'LocalFree', [C.c_void_p], C.c_void_p),
    (A, 'OpenProcessToken', [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL),
    (A, 'GetTokenInformation', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
    (A, 'GetSecurityInfo', [W.HANDLE, C.c_int, W.DWORD, C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p), C.c_void_p, C.POINTER(C.c_void_p)], W.DWORD),
    (A, 'GetSecurityDescriptorControl', [C.c_void_p, C.POINTER(W.WORD), C.POINTER(W.DWORD)], W.BOOL),
    (A, 'GetAce', [C.c_void_p, W.DWORD, C.POINTER(C.c_void_p)], W.BOOL),
    (A, 'EqualSid', [C.c_void_p, C.c_void_p], W.BOOL),
    (A, 'ConvertSidToStringSidW', [C.c_void_p, C.POINTER(W.LPWSTR)], W.BOOL),
    (D, 'CryptUnprotectData', [C.POINTER(BLOB), C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p, W.DWORD, C.POINTER(BLOB)], W.BOOL),
]:
    fn = getattr(library, name); fn.argtypes, fn.restype = args, result


def checked(value):
    if not value:
        raise C.WinError(C.get_last_error())
    return value


def token_information(kind, process=None):
    token, size = W.HANDLE(), W.DWORD()
    checked(A.OpenProcessToken(K.GetCurrentProcess() if process is None else process, 8, C.byref(token)))
    try:
        A.GetTokenInformation(token, kind, None, 0, C.byref(size))
        buffer = C.create_string_buffer(size.value)
        checked(A.GetTokenInformation(token, kind, buffer, size, C.byref(size)))
        return buffer
    finally:
        K.CloseHandle(token)


def logon(process=None):
    buffer = token_information(28, process)  # TokenLogonSid
    assert GROUPS.from_buffer(buffer).count == 1
    return buffer


def sid_text(sid):
    text = W.LPWSTR()
    checked(A.ConvertSidToStringSidW(sid, C.byref(text)))
    try: return text.value
    finally: K.LocalFree(C.cast(text, C.c_void_p))


@contextmanager
def _private_handle(path, *, directory=False, deleting=False):
    # Cleanup holds exclusive handles, including DELETE access. Validation and
    # deletion target the same object, never a path reopened after validation.
    handle = K.CreateFileW(str(path), 0x80000000 | (0x10000 if deleting else 0),
        0 if deleting else 1, None, 3, 0x02200000, None)
    if handle == INVALID:
        raise C.WinError(C.get_last_error())
    descriptor, acl = C.c_void_p(), C.c_void_p()
    try:
        info = C.create_string_buffer(64)
        checked(K.GetFileInformationByHandle(handle, info))
        attributes = W.DWORD.from_buffer(info).value
        if attributes & 0x400:
            raise PermissionError('Recovery endpoint cannot be a reparse point')
        if bool(attributes & 0x10) != directory:
            raise PermissionError('Recovery endpoint has an unexpected file type')
        status = A.GetSecurityInfo(handle, 1, 4, None, None, C.byref(acl), None, C.byref(descriptor))
        if status:
            raise C.WinError(status)
        control, revision = W.WORD(), W.DWORD()
        checked(A.GetSecurityDescriptorControl(descriptor, C.byref(control), C.byref(revision)))
        if not acl.value or not control.value & 0x1000:
            raise PermissionError('Endpoint must have an explicit protected DACL')
        identity = token_information(1)  # TokenUser; secondary logon can share a logon SID.
        count = W.WORD.from_address(acl.value + 4).value
        if count != 1:
            raise PermissionError('Endpoint must grant only this account access')
        ace = C.c_void_p(); checked(A.GetAce(acl, 0, C.byref(ace)))
        if C.c_ubyte.from_address(ace.value).value != 0:
            raise PermissionError('Endpoint must use an account allow ACE')
        if not A.EqualSid(ace.value + 8, SID_ENTRY.from_buffer(identity).sid):
            session = logon()
            if A.EqualSid(ace.value + 8, GROUPS.from_buffer(session).groups[0].sid):
                raise BrowserError('host_unrecoverable', '旧版恢复端点不符合 v0.6.1 账户隔离要求；请显式更换浏览器，未修改原页面或端点')
            raise PermissionError('Endpoint belongs to a different account')
        yield handle
    finally:
        if descriptor.value: K.LocalFree(descriptor)
        K.CloseHandle(handle)


def _read_descriptor(handle):
    content, read = C.create_string_buffer(16385), W.DWORD()
    checked(K.ReadFile(handle, content, len(content), C.byref(read), None))
    if read.value > 16384:
        raise ValueError('Oversized recovery descriptor')
    return json.loads(content.raw[:read.value])


def private_file(path, directory=False):
    with _private_handle(path, directory=directory) as handle:
        return None if directory else _read_descriptor(handle)


def _handle_path(handle):
    name = C.create_unicode_buffer(32768)
    length = checked(K.GetFinalPathNameByHandleW(handle, name, len(name), 0))
    if length >= len(name) or not name.value.startswith('\\\\?\\'):
        raise PermissionError('Cleanup endpoint has an unsupported final path')
    return Path(name.value[4:])


@contextmanager
def _endpoint_root(path):
    # Anchor the application's actual directory, including Windows packaged-app
    # LocalAppData virtualization. Pin its name without requiring a private DACL
    # on the shared Hosts root; each target directory/file still requires one.
    handle = K.CreateFileW(str(path), 0x80, 3, None, 3, 0x02200000, None)
    if handle == INVALID:
        raise C.WinError(C.get_last_error())
    try:
        info = C.create_string_buffer(64)
        checked(K.GetFileInformationByHandle(handle, info))
        attributes = W.DWORD.from_buffer(info).value
        if attributes & 0x400 or not attributes & 0x10:
            raise PermissionError('Cleanup root cannot be a reparse point or file')
        yield _handle_path(handle)
    finally:
        K.CloseHandle(handle)


def _discard_exited_endpoint(endpoint, description):
    """Best effort after a pinned client's OS check proves the PID has exited.

    Never scan directories, decrypt credentials, remove cache/data, or infer death
    from an access denial, identity mismatch or busy connection. A surviving pipe
    also vetoes cleanup, including descriptors with a corrupted PID.
    """
    try:
        identity = str(uuid.UUID(description['host_id'])).upper()
        root = (Path(os.environ['LOCALAPPDATA']) / 'DistinctBrowser' / 'Hosts').absolute()
        expected = root / identity / 'endpoint.json'
        if endpoint != expected or type(description['pid']) is not int or not 0 < description['pid'] <= 0xffffffff:
            return
        with _endpoint_root(root) as actual, _private_handle(expected.parent, directory=True, deleting=True) as directory:
            if _handle_path(directory) != actual / identity:
                return
            with _private_handle(expected, deleting=True) as file:
                if _handle_path(file) != actual / identity / 'endpoint.json':
                    return
                if _read_descriptor(file) != description:
                    return
                # NMPWAIT_NOWAIT. Only an absent local pipe permits deletion;
                # busy, inaccessible and all other outcomes preserve metadata.
                if K.WaitNamedPipeW(description['pipe_name'], 1) or C.get_last_error() != 2:
                    return
                disposition = C.c_ubyte(1)  # FILE_DISPOSITION_INFO.DeleteFile
                checked(K.SetFileInformationByHandle(file, 4, C.byref(disposition), C.sizeof(disposition)))
            # Windows rejects a nonempty directory. No recursive deletion.
            K.SetFileInformationByHandle(directory, 4, C.byref(disposition), C.sizeof(disposition))
    except (OSError, ValueError, KeyError, TypeError):
        pass  # Recovery must still fail; cleanup never broadens permissions.


class ReconnectClient:
    def __init__(self, endpoint, executable, *, expected_sha256=None, expected_host_id=None, heartbeat=True):
        self.endpoint, self.executable = Path(endpoint).absolute(), Path(executable).resolve()
        if self.endpoint.drive.startswith('\\\\'):
            raise ConnectionError('Recovery endpoints must be local files')
        try:
            private_file(self.endpoint.parent, directory=True)
            description = private_file(self.endpoint)
        except FileNotFoundError as error:
            raise ConnectionError('Host endpoint is unavailable; no browser was recreated') from error
        account, session = token_information(1), logon()
        if (description.get('user_sid') != sid_text(SID_ENTRY.from_buffer(account).sid)
                or description.get('logon_sid') != sid_text(GROUPS.from_buffer(session).groups[0].sid)):
            raise BrowserError('host_unrecoverable', '恢复端点账户或登录身份不符（包括旧版端点）；请显式更换浏览器，未重建页面')
        if description['protocol_version'] != 1 or os.path.normcase(os.path.normpath(description['executable'])) != os.path.normcase(str(self.executable)):
            raise ConnectionError('Host executable/protocol does not match the trusted caller configuration')
        if expected_sha256 and hashlib.sha256(self.executable.read_bytes()).hexdigest() != expected_sha256:
            raise ConnectionError('Host artifact checksum does not match')
        self.host_id = description['host_id']
        self.pid = description['pid']
        if expected_host_id is not None and self.host_id != expected_host_id:
            raise ConnectionError('Endpoint does not identify the saved browser host')
        self.connection_id = None
        if description['pipe_name'] != r'\\.\pipe\DistinctBrowser-' + self.host_id:
            raise ConnectionError('Invalid host endpoint')
        self._peer = K.OpenProcess(0x101000, False, description['pid'])
        if not self._peer:
            if expected_host_id and expected_sha256 and C.get_last_error() == 87:
                _discard_exited_endpoint(self.endpoint, description)
            raise ConnectionError('Original host process is unavailable; no browser was recreated')
        self._pipe = None
        try:
            status = K.WaitForSingleObject(self._peer, 0)
            if status != 258:
                if status == 0 and expected_host_id and expected_sha256:
                    _discard_exited_endpoint(self.endpoint, description)
                raise ConnectionError('Original host process has exited; no browser was recreated')
            times = [C.c_ulonglong() for _ in range(4)]
            image, size = C.create_unicode_buffer(32768), W.DWORD(32768)
            try:
                checked(K.GetProcessTimes(self._peer, *(C.byref(t) for t in times)))
                checked(K.QueryFullProcessImageNameW(self._peer, 0, image, C.byref(size)))
            except OSError as error:
                raise ConnectionError('Original host process identity is unavailable') from error
            if str(times[0].value) != description['process_created'] or Path(image.value).resolve() != self.executable or K.WaitForSingleObject(self._peer, 0) != 258:
                raise ConnectionError('Original host is no longer running')
            account, session = token_information(1, self._peer), logon(self._peer)
            if (description['user_sid'] != sid_text(SID_ENTRY.from_buffer(account).sid)
                    or description['logon_sid'] != sid_text(GROUPS.from_buffer(session).groups[0].sid)):
                raise ConnectionError('Host process account/logon identity does not match')
            self._pipe = K.CreateFileW(description['pipe_name'], 0x0012018b, 0, None, 3, 0x40000000, None)
            if self._pipe == INVALID:
                self._pipe = None
                raise C.WinError(C.get_last_error())
            pid = W.ULONG(); checked(K.GetNamedPipeServerProcessId(self._pipe, C.byref(pid)))
            if pid.value != description['pid']:
                raise ConnectionError('Pipe server identity does not match')
            protected = C.create_string_buffer(base64.b64decode(description['credential_dpapi'], validate=True))
            encrypted, plain = BLOB(len(protected) - 1, C.addressof(protected)), BLOB()
            checked(D.CryptUnprotectData(C.byref(encrypted), None, None, None, None, 1, C.byref(plain)))
            try:
                credential = C.string_at(plain.data, plain.size).decode('ascii')
            finally:
                K.LocalFree(plain.data)
            self._stopping = threading.Event()
            self._lock, self._write_lock = threading.Lock(), threading.Lock()
            self._event_lock = threading.Lock()
            self._event_handler = self._closed_handler = None
            self.events, self._pending, self._serial = queue.Queue(maxsize=128), {}, 0
            self._send({'host_id': self.host_id, 'credential': credential, 'protocol_version': 1})
            self._reader = threading.Thread(target=self._read, daemon=True); self._reader.start()
            try: ready = self.events.get(timeout=6)
            except queue.Empty as error: raise ConnectionError('Host handshake was rejected or timed out') from error
            if ready.get('event') != 'ready' or ready.get('host_id') != self.host_id:
                raise ConnectionError('Invalid recovered host greeting')
            self.connection_id = ready['connection_id']
            self._heartbeat = threading.Thread(target=self._ping, daemon=True) if heartbeat else None
            if self._heartbeat: self._heartbeat.start()
        except BaseException:
            if hasattr(self, '_stopping'): self.disconnect()
            else:
                if self._pipe: K.CloseHandle(self._pipe)
                K.CloseHandle(self._peer)
            raise

    def _io(self, writing, data=None):
        buffer = C.create_string_buffer(data) if writing else C.create_string_buffer(65536)
        count = W.DWORD()
        event = checked(K.CreateEventW(None, True, False, None))
        operation = OVERLAPPED(hEvent=event)
        try:
            ok = (K.WriteFile if writing else K.ReadFile)(self._pipe, buffer, len(data) if writing else len(buffer), C.byref(count), C.byref(operation))
            if not ok and C.get_last_error() == 997:
                deadline = time.monotonic() + (10 if writing else 60)
                while K.WaitForSingleObject(event, 25) != 0:
                    if self._stopping.is_set() or time.monotonic() > deadline or K.WaitForSingleObject(self._peer, 0) == 0:
                        K.CancelIoEx(self._pipe, C.byref(operation))
                        K.GetOverlappedResult(self._pipe, C.byref(operation), C.byref(count), True)
                        raise ConnectionError('Host connection ended')
                ok = K.GetOverlappedResult(self._pipe, C.byref(operation), C.byref(count), False)
            checked(ok)
            if not count.value: raise ConnectionError('Host pipe closed')
            return count.value if writing else buffer.raw[:count.value]
        finally:
            K.CloseHandle(event)

    def _send(self, message):
        with self._write_lock:
            data = (json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8')
            if len(data) - 1 > 1024 * 1024:
                raise ValueError('Request exceeds the host frame limit')
            while data:
                data = data[self._io(True, data):]

    def _read(self):
        data = b''
        try:
            while not self._stopping.is_set():
                data += self._io(False)
                if len(data) > 36 * 1024 * 1024: raise ValueError('Oversized host response')
                while b'\n' in data:
                    line, data = data.split(b'\n', 1)
                    response = json.loads(line)
                    if response.get('host_id') != self.host_id:
                        raise ConnectionError('Response from a different host')
                    if self.connection_id is None:
                        if response.get('event') != 'ready': raise ConnectionError('Missing connection greeting')
                        self.connection_id = response['connection_id']
                    elif response.get('connection_id') != self.connection_id:
                        raise ConnectionError('Response from a different connection')
                    if 'event' in response:
                        with self._event_lock:
                            if self._event_handler: self._event_handler(response)
                            else: self.events.put_nowait(response)
                    else:
                        with self._lock: future = self._pending.pop(response['id'], None)
                        if future:
                            if response['ok']: future.set_result(response['result'])
                            else:
                                error = response['error']
                                future.set_exception(BrowserError(error['code'], error['message'], error.get('detail')))
        except Exception:
            pass  # Pending callers receive connection loss below; no replay.
        finally:
            self._stopping.set()
            with self._lock:
                for future in self._pending.values(): future.set_exception(ConnectionError('Host disconnected; requests are not replayed'))
                self._pending.clear()
            with self._event_lock:
                if self._closed_handler: self._closed_handler()

    def subscribe(self, on_event, on_closed):
        """Drain the snapshot buffer, then preserve event/response read order."""
        with self._event_lock:
            while True:
                try: on_event(self.events.get_nowait())
                except queue.Empty: break
            self._event_handler, self._closed_handler = on_event, on_closed
            if self.closed: on_closed()

    def _ping(self):
        while not self._stopping.wait(5):
            try: self.call('ping')
            except BrowserError as error:
                if error.code == 'busy': continue
                return
            except Exception: return

    @property
    def closed(self):
        return self._stopping.is_set()

    def submit(self, method, **params):
        with self._lock:
            if self._stopping.is_set(): raise ConnectionError('Client disconnected')
            if len(self._pending) >= 128: raise BrowserError('busy', '浏览器请求队列已满，请求未接受')
            self._serial += 1
            identity = str(self._serial); future = Future(); self._pending[identity] = future
        try: self._send({'id': identity, 'method': method, 'params': params})
        except BaseException:
            with self._lock: self._pending.pop(identity, None)
            raise
        return future

    def call(self, method, **params):
        return self.submit(method, **params).result(timeout=60)

    def disconnect(self):
        self._stopping.set()
        if self._pipe: K.CancelIoEx(self._pipe, None)
        for name in ('_reader', '_heartbeat'):
            thread = getattr(self, name, None)
            if thread and thread is not threading.current_thread(): thread.join(12)
        if self._pipe: K.CloseHandle(self._pipe); self._pipe = None
        if self._peer: K.CloseHandle(self._peer); self._peer = None
