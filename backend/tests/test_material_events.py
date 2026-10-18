"""Directory events, bounded invalidation and independent compensation scheduling."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.app.desktop.material_events import MaterialChangeFeed, path_key
from backend.app.desktop.service import DesktopService
from backend.app.desktop.state_changes import ChangeSignal
from backend.app.desktop.material_watch import MaterialFileMonitor


def test_event_overflow_requires_full_reconciliation():
    feed = MaterialChangeFeed()
    feed.invalidate(str(index) for index in range(5000))
    assert feed.take() is None
    assert feed.take() == set()


def test_native_directory_events_include_atomic_replace_and_delete(tmp_path):
    async def run():
        target = tmp_path / "protected.txt"
        target.write_text("first")
        feed = MaterialChangeFeed()
        await feed.set_paths([target])
        try:
            # Allow the native watcher to register before the first mutation.
            await asyncio.sleep(0.2)
            replacement = tmp_path / "replacement.tmp"
            replacement.write_text("second")
            replacement.replace(target)
            assert await feed.signal.wait(0, 3) > 0
            observed = feed.take()
            assert observed is None or path_key(target) in observed
            generation = feed.signal.generation
            target.unlink()
            assert await feed.signal.wait(generation, 3) > generation
            assert path_key(target) in feed.take()
        finally:
            await feed.close()
    asyncio.run(run())


def test_idle_controller_does_not_query_every_two_seconds(tmp_path):
    async def run():
        service = object.__new__(DesktopService)
        service.state_changes = SimpleNamespace(materials=ChangeSignal())
        service._scan_materials = AsyncMock(return_value=({}, set()))
        service._digest = lambda _path: "digest"
        task = asyncio.create_task(service._watch_materials())
        try:
            await asyncio.sleep(0.05)
            assert service._scan_materials.await_count == 1
            service.state_changes.materials.publish()
            await asyncio.sleep(0.05)
            assert service._scan_materials.await_count == 2
            await asyncio.sleep(0.05)
            assert service._scan_materials.await_count == 2
        finally:
            task.cancel()
            await task
    asyncio.run(run())


def test_unrelated_event_cannot_shorten_material_stability_interval(tmp_path):
    path = tmp_path / "protected.txt"
    path.write_text("original")
    now = 0
    digests = []
    def digest(path):
        digests.append(path.read_text())
        return digests[-1]
    monitor = MaterialFileMonitor(digest, clock=lambda: now)
    assert monitor.digest("material", path) == "original"
    path.write_text("intermediate content")
    assert monitor.digest("material", path) is None
    now = 0.25
    assert monitor.digest("material", path) is None
    assert digests == ["original"]
    now = 2
    assert monitor.digest("material", path) == "intermediate content"


def test_failed_pending_scan_applies_backoff_instead_of_zero_timeout_loop(monkeypatch):
    import backend.app.desktop.service as module
    async def run():
        now = [0.0]
        waits = []
        service = object.__new__(DesktopService)
        service.state_changes = SimpleNamespace(materials=ChangeSignal())
        service._digest = lambda _path: "digest"
        service._scan_materials = AsyncMock(side_effect=[({"one": "/one.txt"}, {"one"}), OSError("database offline")])
        class Feed:
            def __init__(self):
                self.signal = ChangeSignal()
            def take(self):
                return set()
            async def set_paths(self, _paths):
                pass
            async def close(self):
                pass
        async def wait(_tasks, **_kwargs):
            await asyncio.sleep(0)
            waits.append(now[0])
            now[0] = 2.0
            if len(waits) > 1:
                raise asyncio.CancelledError
        monkeypatch.setattr(module, "monotonic", lambda: now[0])
        monkeypatch.setattr(module, "MaterialChangeFeed", Feed)
        monkeypatch.setattr(module.asyncio, "wait", wait)
        original_wait = ChangeSignal.wait
        timeouts = []
        async def record_wait(self, generation, timeout=60):
            timeouts.append(timeout)
            return await original_wait(self, generation, timeout)
        monkeypatch.setattr(ChangeSignal, "wait", record_wait)
        await service._watch_materials()
        assert service._scan_materials.await_count == 2
        assert timeouts[-2:] == [3.0, 3.0]
        assert waits == [0.0, 2.0]
    asyncio.run(run())
