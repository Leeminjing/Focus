"""Bounded directory event feed; file stability and protection stay with the monitor."""

import asyncio
import logging
import os
from pathlib import Path
from time import monotonic

from watchfiles import awatch

from backend.app.desktop.persistence.state_changes import ChangeSignal

logger = logging.getLogger(__name__)


def path_key(path):
    return os.path.normcase(os.path.abspath(path))


class MaterialChangeFeed:
    def __init__(self):
        self.signal = ChangeSignal()
        self._dirty = set()
        self._full = False
        self._directories = ()
        self._task = None
        self._stop = None
        self._retry_at = 0
        self._paths = set()

    async def set_paths(self, paths):
        paths = tuple(paths)
        self._paths = {path_key(path) for path in paths}
        def directories():
            result = set()
            for path in paths:
                parent = Path(path).parent
                try:
                    while not parent.is_dir() and parent != parent.parent:
                        parent = parent.parent
                except OSError:
                    # One inaccessible directory must not disable healthy watches.
                    continue
                result.add(path_key(parent))
                # Observe removal/replacement of the watched directory as well.
                result.add(path_key(parent.parent))
            return tuple(sorted(result))

        targets = await asyncio.to_thread(directories)
        if monotonic() >= self._retry_at and (targets != self._directories or (
                targets and (self._task is None or self._task.done()))):
            await self.close()
            self._directories = targets
            if targets:
                self._stop = asyncio.Event()
                self._task = asyncio.create_task(self._watch(targets, self._stop), name="material-file-events")

    def invalidate(self, paths=(), *, full=False):
        changed = self._full or full
        self._full |= full
        if not self._full:
            for path in paths:
                changed = True
                self._dirty.add(path_key(path))
                if len(self._dirty) > 4096:
                    self._full = True
                    break
        if self._full:
            self._dirty.clear()
        if changed:
            self.signal.publish()

    def take(self):
        paths, full = self._dirty, self._full
        self._dirty, self._full = set(), False
        return None if full else paths

    async def _watch(self, directories, stop):
        try:
            registered = False
            async for changes in awatch(*directories, stop_event=stop, watch_filter=None,
                                        recursive=False, debounce=250, step=50, rust_timeout=1000,
                                        yield_on_timeout=True, force_polling=False, ignore_permission_denied=True):
                if not registered:
                    registered = True
                    # Close the initial scan -> native registration race.
                    self.invalidate(full=True)
                relevant = (path_key(path) for _change, path in changes
                            if any(key == path_key(path) or key.startswith(path_key(path) + os.sep)
                                   for key in self._paths))
                self.invalidate(relevant)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("material event listener unavailable: %s", type(error).__name__)
            self._retry_at = monotonic() + 60
            self.invalidate(full=True)

    async def close(self):
        if self._task is not None:
            self._stop.set()
            await self._task
            self._task = None
