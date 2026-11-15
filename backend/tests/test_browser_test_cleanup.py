"""Nested pytest teardown must leave no native host or WebView children."""
import json
import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from backend.tests.browser_process_support import reap_browser_processes


@pytest.mark.skipif(os.name != 'nt' or os.getenv('FOCUS_BROWSER_RECOVERY_REAL') != '1',
                    reason='opt-in real Windows browser cleanup')
@pytest.mark.parametrize('mode', ['recovery', 'stdio', 'gateway'])
@pytest.mark.parametrize('fail', [False, True], ids=['passed', 'failed'])
def test_teardown_reaps_hosts_without_touching_a_neighbour(tmp_path, mode, fail):
    from backend.app.desktop.browser.host_artifact import stage

    executable = Path(__file__).resolve().parents[2] / 'desktop/resources/distinct-browser/distinct-browser.exe'
    neighbour_image, _ = stage(executable, tmp_path / 'neighbour')
    neighbour = subprocess.Popen([str(neighbour_image), '--reconnect'], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
    work = tmp_path / 'nested'
    work.mkdir()
    report = work / 'processes.json'
    test = work / 'test_leak.py'
    test.write_text('''import asyncio, json, os
from pathlib import Path
import psutil
import pytest
from backend.app.desktop.browser.transport import BrowserTransport

def test_leak(tmp_path):
    async def scenario():
        transport = BrowserTransport(recovery=MODE != 'stdio')
        try:
            await transport.call('create', label='Owned failing cleanup regression')
            process = psutil.Process(transport.pid)
            REPORT.write_text(json.dumps([{'pid': p.pid, 'birth': p.create_time()}
                                         for p in [process, *process.children(recursive=True)]]))
            if FAIL:
                pytest.fail('intentional assertion after browser creation')
        finally:
            await transport.disconnect()
    asyncio.run(scenario())
'''.replace('MODE', repr(mode)).replace('FAIL', repr(fail))
       .replace('REPORT', 'Path(' + repr(str(report)) + ')'), encoding='utf-8')
    if mode == 'gateway':
        # A separate interpreter has no pytest monkeypatches, just like Gateway.
        test.write_text('''import os, subprocess, sys
def test_gateway():
    result = subprocess.run([sys.executable, '-c', CODE], env=os.environ.copy())
    assert result.returncode == 0, 'intentional Gateway failure'
'''.replace('CODE', repr(test.read_text(encoding='utf-8') + '\ntest_leak(None)\n')), encoding='utf-8')
    environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2]),
                   'PYTHONUTF8': '1'}
    try:
        bootstrap = json.loads(neighbour.stdout.readline())
        assert bootstrap['pid'] == neighbour.pid
        result = subprocess.run([sys.executable, '-m', 'pytest', '-p',
                                 'backend.tests.browser_process_support', str(test), '-q',
                                 '--basetemp', str(work / 'temporary')],
                                cwd=work, env=environment, capture_output=True, text=True, timeout=45,
                                check=False)
        assert result.returncode == int(fail), result.stdout + result.stderr
        if fail:
            assert 'intentional' in result.stdout, result.stdout + result.stderr
        records = json.loads(report.read_text())
        leaked = []
        for record in records:
            try:
                process = psutil.Process(record['pid'])
                if process.create_time() == record['birth'] and process.is_running():
                    leaked.append(process.pid)
            except psutil.NoSuchProcess:
                pass
        assert not leaked, f'Test leaked native browser/WebView processes: {leaked}'
        assert neighbour.poll() is None, 'Cleanup killed another test/user browser'
    finally:
        reap_browser_processes(tmp_path)
        neighbour.wait(5)
        neighbour.stdout.close()
        neighbour.stderr.close()
