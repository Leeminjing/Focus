"""Lazy, plugin-owned ONLYOFFICE Community Edition runtime.

Importing this module has no side effects. Docker is touched only by ``prepare``.
Every failure is converted to ``RuntimeUnavailable`` so callers can degrade the
DOCX panel without affecting the Focus process.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable, Sequence

import httpx


PLUGIN_DIR = Path(__file__).resolve().parent
COMPOSE_FILE = PLUGIN_DIR / "onlyoffice" / "compose.yaml"
RUNTIME_DIR = PLUGIN_DIR / "onlyoffice" / ".runtime"
ENV_FILE = RUNTIME_DIR / "documentserver.env"


class RuntimeUnavailable(RuntimeError):
    """The optional DOCX editor runtime could not be prepared."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class RuntimeSnapshot:
    state: str = "stopped"
    error: str | None = None
    last_used: float | None = None
    document_server_url: str = "http://127.0.0.1:18080"

    def payload(self) -> dict:
        return asdict(self)


Runner = Callable[[Sequence[str]], Awaitable[CommandResult]]
HealthCheck = Callable[[str], Awaitable[bool]]


async def _run_command(command: Sequence[str]) -> CommandResult:
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    # Focus deliberately runs a SelectorEventLoop on Windows, where asyncio's
    # subprocess transport is unavailable. Keep Docker work off that loop while
    # retaining the runtime's async boundary.
    result = await asyncio.to_thread(
        subprocess.run,
        list(command),
        capture_output=True,
        check=False,
        creationflags=creationflags,
    )
    return CommandResult(
        result.returncode,
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
    )


async def _healthcheck(origin: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{origin}/healthcheck")
        return response.status_code == 200 and response.text.strip().lower() == "true"
    except httpx.HTTPError:
        return False


class DocumentServerRuntime:
    def __init__(
        self,
        *,
        runner: Runner = _run_command,
        healthcheck: HealthCheck = _healthcheck,
        document_server_url: str = "http://127.0.0.1:18080",
        idle_seconds: float = 900,
        startup_timeout: float = 120,
        compose_file: Path = COMPOSE_FILE,
        env_file: Path = ENV_FILE,
    ) -> None:
        self._runner = runner
        self._healthcheck = healthcheck
        self._idle_seconds = idle_seconds
        self._startup_timeout = startup_timeout
        self._compose_file = compose_file
        self._env_file = env_file
        self._lock = asyncio.Lock()
        self._active_sessions: set[str] = set()
        self._idle_task: asyncio.Task | None = None
        self.snapshot = RuntimeSnapshot(document_server_url=document_server_url)

    @property
    def jwt_secret(self) -> str:
        return self._ensure_env()["DOCX_DS_JWT_SECRET"]

    @property
    def bridge_secret(self) -> str:
        return self._ensure_env()["DOCX_BRIDGE_SECRET"]

    def _ensure_env(self) -> dict[str, str]:
        values: dict[str, str] = {}
        if self._env_file.is_file():
            for line in self._env_file.read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition("=")
                if separator and key:
                    values[key] = value
            if values.get("DOCX_DS_JWT_SECRET") and values.get("DOCX_BRIDGE_SECRET"):
                return values
        self._env_file.parent.mkdir(parents=True, exist_ok=True)
        values = {
            "DOCX_DS_PORT": self.snapshot.document_server_url.rsplit(":", 1)[-1],
            "DOCX_DS_JWT_SECRET": values.get("DOCX_DS_JWT_SECRET", secrets.token_urlsafe(48)),
            "DOCX_BRIDGE_SECRET": secrets.token_urlsafe(48),
        }
        self._env_file.write_text(
            "".join(f"{key}={value}\n" for key, value in values.items()),
            encoding="utf-8",
        )
        return values

    def _compose_command(self, *args: str) -> list[str]:
        self._ensure_env()
        return [
            "docker", "compose", "--env-file", str(self._env_file),
            "-f", str(self._compose_file), *args,
        ]

    async def _checked(self, command: Sequence[str], label: str) -> CommandResult:
        try:
            result = await self._runner(command)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeUnavailable(f"{label}失败: {exc}") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeUnavailable(f"{label}失败: {detail or f'exit {result.returncode}'}")
        return result

    async def prepare(self) -> RuntimeSnapshot:
        """Start on first use and wait until healthy; safe to call concurrently."""
        async with self._lock:
            self.snapshot.last_used = time.time()
            if self.snapshot.state == "ready" and await self._healthcheck(
                self.snapshot.document_server_url
            ):
                return self.snapshot
            self.snapshot.state = "preparing"
            self.snapshot.error = None
            try:
                await self._checked(["docker", "version"], "Docker 检查")
                await self._checked(
                    self._compose_command("up", "-d", "--remove-orphans"),
                    "Document Server 启动",
                )
                deadline = asyncio.get_running_loop().time() + self._startup_timeout
                while asyncio.get_running_loop().time() < deadline:
                    if await self._healthcheck(self.snapshot.document_server_url):
                        self.snapshot.state = "ready"
                        self._arm_idle_stop()
                        return self.snapshot
                    await asyncio.sleep(1)
                raise RuntimeUnavailable("Document Server 健康检查超时")
            except Exception as exc:
                error = exc if isinstance(exc, RuntimeUnavailable) else RuntimeUnavailable(str(exc))
                self.snapshot.state = "unavailable"
                self.snapshot.error = str(error)
                raise error

    def session_opened(self, session_id: str) -> None:
        self._active_sessions.add(session_id)
        self.snapshot.last_used = time.time()
        self._cancel_idle_stop()

    def session_closed(self, session_id: str) -> None:
        self._active_sessions.discard(session_id)
        self.snapshot.last_used = time.time()
        self._arm_idle_stop()

    def _cancel_idle_stop(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    def _arm_idle_stop(self) -> None:
        self._cancel_idle_stop()
        if self._idle_seconds > 0 and self.snapshot.state == "ready":
            self._idle_task = asyncio.create_task(self._stop_after_idle())

    async def _stop_after_idle(self) -> None:
        try:
            await asyncio.sleep(self._idle_seconds)
            if not self._active_sessions:
                await self.stop()
        except asyncio.CancelledError:
            return

    async def stop(self) -> RuntimeSnapshot:
        async with self._lock:
            if self._active_sessions:
                return self.snapshot
            try:
                await self._checked(self._compose_command("stop"), "Document Server 停止")
                self.snapshot.state = "stopped"
                self.snapshot.error = None
            except RuntimeUnavailable as exc:
                self.snapshot.state = "unavailable"
                self.snapshot.error = str(exc)
            return self.snapshot


runtime = DocumentServerRuntime()
