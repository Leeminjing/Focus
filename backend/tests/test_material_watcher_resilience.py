"""材料监测的读取预算、线程隔离、变更合并及恢复/重试回归测试。"""

import asyncio
import hashlib
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from backend.app.desktop import service as service_module
from backend.app.desktop.service import DesktopService


def _watch_service(tmp_path):
    path = tmp_path / "material.txt"
    path.write_bytes(b"original")
    material = SimpleNamespace(
        material_id="material-one", relative_path=path.name,
        digest=hashlib.sha256(b"original").hexdigest(), needs_confirmation=False,
    )
    rows = [(material, None, SimpleNamespace(path=str(tmp_path)))]
    session = _Session()
    session.execute = AsyncMock(return_value=SimpleNamespace(all=lambda: rows))
    session.commit = AsyncMock()
    service = object.__new__(DesktopService)
    service.session_factory = lambda: session
    service._material_watch_failures = set()
    return service, session, material, path


def _run_cycles(service, monkeypatch, count, before_cycle=lambda _: None):
    cycle = 0

    async def tick(_delay):
        nonlocal cycle
        cycle += 1
        if cycle > count:
            raise asyncio.CancelledError
        before_cycle(cycle)

    monkeypatch.setattr(service_module.asyncio, "sleep", tick)
    asyncio.run(service._watch_materials())


def test_material_watcher_does_not_rehash_unchanged_files(tmp_path, monkeypatch):
    service, session, _, _ = _watch_service(tmp_path)
    digest = Mock(wraps=service._digest)
    monkeypatch.setattr(service, "_digest", digest)

    _run_cycles(service, monkeypatch, 3)

    assert digest.call_count == 1
    session.commit.assert_not_awaited()


def test_material_watcher_hashes_off_event_loop(tmp_path, monkeypatch):
    service, _, _, _ = _watch_service(tmp_path)
    loop_thread = threading.get_ident()
    digest_threads = []
    original_digest = service._digest

    def digest(path):
        digest_threads.append(threading.get_ident())
        return original_digest(path)

    monkeypatch.setattr(service, "_digest", digest)
    _run_cycles(service, monkeypatch, 1)

    assert digest_threads
    assert all(thread != loop_thread for thread in digest_threads)


def test_material_watcher_coalesces_burst_changes(tmp_path, monkeypatch):
    service, session, material, path = _watch_service(tmp_path)
    saved_digests = []

    async def save(*_args):
        material.digest = hashlib.sha256(path.read_bytes()).hexdigest()
        saved_digests.append(material.digest)

    service._save_material_version = AsyncMock(side_effect=save)

    def edit(cycle):
        if cycle in (2, 3):
            path.write_bytes(b"changed" * cycle)

    _run_cycles(service, monkeypatch, 5, edit)

    assert saved_digests == [hashlib.sha256(b"changed" * 3).hexdigest()]
    session.commit.assert_awaited_once()


def test_slow_material_digest_keeps_event_loop_responsive(tmp_path, monkeypatch):
    service, _, _, _ = _watch_service(tmp_path)
    started, release = threading.Event(), threading.Event()
    released_by_loop = []
    original_digest = service._digest
    sleep = asyncio.sleep
    cycles = 0

    def slow_digest(path):
        started.set()
        released_by_loop.append(release.wait(timeout=2))
        return original_digest(path)

    async def tick(_delay):
        nonlocal cycles
        cycles += 1
        if cycles > 1:
            raise asyncio.CancelledError

    async def run():
        watcher = asyncio.create_task(service._watch_materials())
        try:
            while not started.is_set() and not watcher.done():
                await sleep(0)
            release.set()
            await watcher
        finally:
            release.set()
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    monkeypatch.setattr(service, "_digest", slow_digest)
    monkeypatch.setattr(service_module.asyncio, "sleep", tick)
    asyncio.run(run())

    assert released_by_loop == [True]


def test_material_watcher_defers_file_changed_during_read(tmp_path, monkeypatch):
    service, session, material, path = _watch_service(tmp_path)
    original_digest = service._digest
    calls = 0

    def changing_digest(path):
        nonlocal calls
        calls += 1
        digest = original_digest(path)
        if calls == 1:
            path.write_bytes(b"final content after concurrent write")
        return digest

    async def save(*_args):
        material.digest = original_digest(path)

    service._save_material_version = AsyncMock(side_effect=save)
    monkeypatch.setattr(service, "_digest", changing_digest)

    def check_first_scan(cycle):
        if cycle == 2:
            service._save_material_version.assert_not_awaited()
            session.commit.assert_not_awaited()

    _run_cycles(service, monkeypatch, 3, check_first_scan)

    service._save_material_version.assert_awaited_once()
    assert material.digest == hashlib.sha256(path.read_bytes()).hexdigest()


def test_material_watcher_restores_deleted_file_off_event_loop(tmp_path, monkeypatch):
    service, session, material, path = _watch_service(tmp_path)
    session.scalar = AsyncMock(return_value=SimpleNamespace(object_id="protected-blob"))
    service._git_bytes = AsyncMock(return_value=b"protected content")
    service._save_material_version = AsyncMock()
    loop_thread = threading.get_ident()
    restore_threads = []
    restore = service_module.restore_material_file

    def record_restore(path, content):
        restore_threads.append(threading.get_ident())
        return restore(path, content)

    monkeypatch.setattr(service_module, "restore_material_file", record_restore)

    def delete(cycle):
        if cycle == 2:
            path.unlink()

    _run_cycles(service, monkeypatch, 4, delete)

    assert path.read_bytes() == b"protected content"
    assert material.digest == hashlib.sha256(b"protected content").hexdigest()
    assert material.needs_confirmation is True
    assert len(restore_threads) == 1 and restore_threads[0] != loop_thread
    session.commit.assert_awaited_once()
    service._save_material_version.assert_not_awaited()


def test_material_watcher_retries_failed_version_protection(tmp_path, monkeypatch):
    service, session, material, path = _watch_service(tmp_path)
    saved = False

    async def save(*_args):
        nonlocal saved
        if not saved:
            saved = True
            raise OSError("temporary snapshot failure")
        material.digest = hashlib.sha256(path.read_bytes()).hexdigest()

    service._save_material_version = AsyncMock(side_effect=save)
    warning = Mock()
    monkeypatch.setattr(service_module.logger, "warning", warning)

    def edit(cycle):
        if cycle == 2:
            path.write_bytes(b"changed content")

    _run_cycles(service, monkeypatch, 5, edit)

    assert service._save_material_version.await_count == 2
    warning.assert_called_once()
    session.commit.assert_awaited_once()
    assert service._material_watch_failures == set()


def test_material_watcher_releases_removed_material_state(tmp_path, monkeypatch):
    service, session, _, _ = _watch_service(tmp_path)
    digest = Mock(wraps=service._digest)
    monkeypatch.setattr(service, "_digest", digest)
    rows = session.execute.return_value

    def remove_then_protect(cycle):
        session.execute.return_value = SimpleNamespace(all=lambda: []) if cycle == 2 else rows

    _run_cycles(service, monkeypatch, 4, remove_then_protect)

    assert digest.call_count == 2


class _Rows:
    def all(self):
        material = SimpleNamespace(material_id="material-denied", relative_path="material.txt")
        workspace = SimpleNamespace(path="C:/denied-workspace")
        return [(material, None, workspace)]


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _statement):
        return _Rows()


def test_material_watcher_logs_persistent_failure_only_once(monkeypatch):
    """同一材料持续无权限时不得每两秒重复打印完整异常。"""

    service = object.__new__(DesktopService)
    service.session_factory = _Session

    sleep_count = 0

    async def two_cycles_then_cancel(_delay):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count > 2:
            raise asyncio.CancelledError

    def deny_access(_path):
        raise PermissionError("workspace denied")

    monkeypatch.setattr(service_module.asyncio, "sleep", two_cycles_then_cancel)
    monkeypatch.setattr(service_module.Path, "stat", deny_access)
    warning = Mock()
    monkeypatch.setattr(service_module.logger, "warning", warning)

    asyncio.run(service._watch_materials())

    assert warning.call_count == 1
    assert warning.call_args.args == (
        "材料监测失败，跳过该材料: material_id=%s",
        "material-denied",
    )
    assert warning.call_args.kwargs == {"exc_info": True}
