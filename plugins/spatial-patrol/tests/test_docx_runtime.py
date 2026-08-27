import asyncio
import subprocess

from plugins.spatial_patrol.docx_runtime import (
    CommandResult,
    DocumentServerRuntime,
    RuntimeUnavailable,
    _run_command,
)


def test_runtime_is_lazy_until_prepare(tmp_path):
    commands = []

    async def runner(command):
        commands.append(tuple(command))
        return CommandResult(0)

    runtime = DocumentServerRuntime(
        runner=runner, healthcheck=lambda _url: _true(), env_file=tmp_path / "runtime.env"
    )
    assert runtime.snapshot.state == "stopped"
    assert commands == []


def test_prepare_starts_docker_once_and_becomes_ready(tmp_path):
    commands = []

    async def runner(command):
        commands.append(tuple(command))
        return CommandResult(0)

    runtime = DocumentServerRuntime(
        runner=runner, healthcheck=lambda _url: _true(), env_file=tmp_path / "runtime.env"
    )
    asyncio.run(runtime.prepare())
    asyncio.run(runtime.prepare())
    assert runtime.snapshot.state == "ready"
    assert sum(command[:2] == ("docker", "version") for command in commands) == 1


def test_prepare_converts_docker_failure_to_plugin_error(tmp_path):
    async def runner(_command):
        return CommandResult(1, stderr="docker missing")

    runtime = DocumentServerRuntime(
        runner=runner, healthcheck=lambda _url: _false(), env_file=tmp_path / "runtime.env"
    )
    try:
        asyncio.run(runtime.prepare())
        raise AssertionError("prepare should fail")
    except RuntimeUnavailable as error:
        assert "docker missing" in str(error)
    assert runtime.snapshot.state == "unavailable"


def test_subprocess_exception_is_converted_to_plugin_error(tmp_path):
    async def runner(_command):
        raise subprocess.SubprocessError("process could not start")

    runtime = DocumentServerRuntime(
        runner=runner, healthcheck=lambda _url: _false(), env_file=tmp_path / "runtime.env"
    )
    try:
        asyncio.run(runtime.prepare())
        raise AssertionError("prepare should fail")
    except RuntimeUnavailable as error:
        assert "process could not start" in str(error)
    assert runtime.snapshot.state == "unavailable"


def test_default_runner_uses_threaded_subprocess(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = b"ok"
        stderr = b""

    def run(command, **options):
        calls.append((command, options))
        return Result()

    monkeypatch.setattr(subprocess, "run", run)
    result = asyncio.run(_run_command(["docker", "version"]))
    assert result == CommandResult(0, "ok", "")
    assert calls[0][0] == ["docker", "version"]
    assert calls[0][1]["capture_output"] is True


def test_unhealthy_document_server_stays_a_plugin_error(tmp_path):
    async def runner(_command):
        return CommandResult(0)

    runtime = DocumentServerRuntime(
        runner=runner, healthcheck=lambda _url: _false(), startup_timeout=0,
        env_file=tmp_path / "runtime.env",
    )
    try:
        asyncio.run(runtime.prepare())
        raise AssertionError("unhealthy server should fail")
    except RuntimeUnavailable as error:
        assert "健康检查超时" in str(error)
    assert runtime.snapshot.state == "unavailable"
    assert "健康检查超时" in runtime.snapshot.error


def test_idle_stop_never_stops_an_active_session(tmp_path):
    commands = []

    async def runner(command):
        commands.append(tuple(command))
        return CommandResult(0)

    runtime = DocumentServerRuntime(
        runner=runner, healthcheck=lambda _url: _true(), idle_seconds=0.01,
        env_file=tmp_path / "runtime.env",
    )
    async def scenario():
        await runtime.prepare()
        runtime.session_opened("session-a")
        runtime.session_opened("session-a")
        await asyncio.sleep(0.03)
        assert not any("stop" in command for command in commands)
        runtime.session_closed("session-a")
        await asyncio.sleep(0.03)
        assert any("stop" in command for command in commands)
    asyncio.run(scenario())


async def _true():
    return True


async def _false():
    return False
