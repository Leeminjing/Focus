"""Commit-driven desktop invalidation, shared across PostgreSQL writer processes.

Notifications carry no task content. Generations coalesce bursts without losing changes
arriving during a snapshot query. Reconnecting invalidates every local subscriber;
clients still reconcile periodically because LISTEN/NOTIFY is not a durable log.
"""

import asyncio
import logging

import asyncpg

logger = logging.getLogger(__name__)


class ChangeSignal:
    def __init__(self):
        self.generation = 0
        self._event = asyncio.Event()

    def publish(self):
        self.generation += 1
        event, self._event = self._event, asyncio.Event()
        event.set()

    async def wait(self, generation, timeout=60):
        event = self._event
        if generation != self.generation:
            return self.generation
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            pass
        return self.generation


class DesktopStateChanges:
    def __init__(self, sessions):
        self.tasks = ChangeSignal()
        self.materials = ChangeSignal()
        self._sessions = sessions
        self._task = None

    def start(self):
        self._task = asyncio.create_task(self._listen(), name="desktop-state-changes")

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _listen(self):
        delay = 1
        while True:
            connection = None
            try:
                engine = self._sessions.kw["bind"]
                args, options = engine.dialect.create_connect_args(engine.url)
                options.pop("prepared_statement_cache_size", None)
                options.pop("prepared_statement_name_func", None)
                options["server_settings"] = {**options.get("server_settings", {}),
                                              "application_name": "focus-desktop-state-listener"}
                connection = await asyncpg.connect(*args, **options, timeout=10)
                disconnected = asyncio.Event()
                connection.add_termination_listener(lambda _connection: disconnected.set())
                for channel, signal in (("focus_tasks_changed", self.tasks),
                                        ("focus_materials_changed", self.materials)):
                    await connection.add_listener(channel, self._callback(signal))
                    signal.publish()
                delay = 1
                await disconnected.wait()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Log only the exception class: driver messages may contain credentials.
                logger.warning("desktop state listener unavailable: %s", type(error).__name__)
            finally:
                if connection is not None:
                    try:
                        await connection.close(timeout=2)
                    except Exception:
                        connection.terminate()
            await asyncio.sleep(delay)
            delay = min(60, delay * 2)

    @staticmethod
    def _callback(signal):
        def notify(_connection, _pid, _channel, _payload):
            signal.publish()
        return notify
