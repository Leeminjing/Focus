"""Windows recovery through Focus's public transport/service; no physical acceptance."""
import asyncio
import ctypes
import os
from pathlib import Path

import pytest

from backend.app.desktop.browser.transport import BrowserError, BrowserTransport

pytestmark = pytest.mark.skipif(os.name != 'nt' or os.getenv('FOCUS_BROWSER_RECOVERY_REAL') != '1', reason='opt-in Windows recovery EXE')


def executable():
    path = Path(os.environ['FOCUS_BROWSER_RECOVERY_EXE'])
    assert path.is_file()
    return path


@pytest.mark.parametrize('override,enabled', [(None, True), ('0', False), ('1', True)])
def test_bundled_browser_default_and_rollback_without_external_executable(tmp_path, monkeypatch, override, enabled):
    monkeypatch.delenv('FOCUS_DISTINCT_BROWSER_EXE', raising=False)
    monkeypatch.delenv('FOCUS_DISTINCT_BROWSER_RECOVERY', raising=False)
    if override is not None: monkeypatch.setenv('FOCUS_DISTINCT_BROWSER_RECOVERY', override)

    async def scenario():
        first = BrowserTransport(cache_root=tmp_path / 'cache')
        replacement = None
        try:
            created = await first.call('create', label='Bundled browser recovery switch')
            assert first.recovery is enabled
            if enabled:
                assert Path(first.endpoint).is_file()
                options = dict(endpoint=first.endpoint, expected_host_id=first.host_id,
                               artifact_sha256=first.artifact_sha256, cache_root=tmp_path / 'cache')
                await first.disconnect()
                replacement = BrowserTransport(**options)
                snapshot = await replacement.call('snapshot')
                browser = snapshot['browsers'][0]
                assert browser['browser_id'] == created['browser_id'] and browser['state'] == 'manual'
                assert browser['selected_page_id'] == created['page_id']
                await replacement.call('close', browser_id=created['browser_id'])
            else:
                assert first.endpoint is None
                await first.call('close', browser_id=created['browser_id'])
        finally:
            for owner in (replacement, first):
                if owner: await owner.disconnect()
            if first.process: await asyncio.to_thread(first.process.wait, 10)
    asyncio.run(scenario())


def test_cancelled_reconnect_does_not_keep_the_next_connection_busy(tmp_path):
    async def scenario():
        first = BrowserTransport(executable(), recovery=True, cache_root=tmp_path / 'cache')
        cancelled = replacement = None
        try:
            browser = await first.call('create', label='Owned cancellation check')
            options = dict(recovery=True, cache_root=tmp_path / 'cache', endpoint=first.endpoint,
                           expected_host_id=first.host_id, artifact_sha256=first.artifact_sha256)
            cancelled = BrowserTransport(**options)
            attempt = asyncio.create_task(cancelled.start())
            await asyncio.sleep(.1)  # Original owner still holds the pipe.
            attempt.cancel()
            with pytest.raises(asyncio.CancelledError): await attempt
            await first.disconnect()
            # The cancelled handshake may finish after the owner leaves. It
            # must close, letting the next authenticated client use the host.
            await asyncio.sleep(.2)
            replacement = BrowserTransport(**options)
            await replacement.start()
            assert (await replacement.call('snapshot'))['browsers'][0]['browser_id'] == browser['browser_id']
            await replacement.call('close', browser_id=browser['browser_id'])
        finally:
            await first.disconnect()
            if cancelled: await cancelled.disconnect()
            if replacement: await replacement.disconnect()
    asyncio.run(scenario())


def test_named_transport_retains_real_page_and_requires_new_lease_after_handback(tmp_path):
    async def scenario():
        events = []
        first = BrowserTransport(executable(), events.append, recovery=True, cache_root=tmp_path / 'cache')
        second = None
        try:
            created = await first.call('create', label='Focus recovery transport')
            bid, page = created['browser_id'], created['page_id']
            token = (await first.call('acquire', browser_id=bid, client_id='before'))['token']
            await first.call('observe', browser_id=bid, token=token)
            endpoint, host_id, artifact = first.endpoint, first.host_id, first.artifact_sha256
            from backend.app.desktop.browser.host_artifact import stage
            from backend.app.desktop.browser.win32_pipe import ReconnectClient
            cached, _ = stage(None, tmp_path / 'cache', artifact)
            with pytest.raises(ConnectionError, match='saved browser host'):
                await asyncio.to_thread(ReconnectClient, endpoint, cached,
                    expected_host_id='wrong-host', expected_sha256=artifact)
            # No application RPC for longer than the native 30-second deadline.
            await asyncio.sleep(32)
            assert (await first.call('snapshot'))['host_id'] == host_id
            await first.disconnect()
            second = BrowserTransport(executable(), events.append, recovery=True, cache_root=tmp_path / 'cache',
                endpoint=endpoint, expected_host_id=host_id, artifact_sha256=artifact)
            await second.start()
            snapshot = await second.call('snapshot')
            browser = snapshot['browsers'][0]
            assert second.host_id == host_id and first.connection_id != second.connection_id
            assert browser['browser_id'] == bid and browser['selected_page_id'] == page and browser['state'] == 'manual'
            assert browser['pages'][0]['hwnd'] == created['hwnd']
            with pytest.raises(BrowserError) as lost:
                await second.call('observe', browser_id=bid, token=token)
            assert lost.value.code == 'invalid_token'
            user32 = ctypes.WinDLL('user32')
            user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
            user32.SendMessageW(int(created['hwnd']), 0x0111, 101, 0)  # Automated native handback.
            fresh = (await second.call('acquire', browser_id=bid, client_id='after'))['token']
            assert fresh != token
            view = await second.call('observe', browser_id=bid, token=fresh)
            assert view['page_id'] == page
            await second.call('close', browser_id=bid)
        finally:
            await first.disconnect()
            if second:
                for browser in (await second.call('snapshot'))['browsers'] if not second.failure else []:
                    await second.call('close', browser_id=browser['browser_id'])
                await second.disconnect()
    asyncio.run(scenario())


@pytest.mark.usefixtures('isolated_postgres_database')
def test_recovery_keeps_cancelled_download_and_resolved_permission_at_source_page(tmp_path):
    from backend.tests.test_context_browser_real import setup, wait_for, error_code
    from backend.tests.browser_site_fixture import SiteFixture
    from backend.app.desktop.browser.service import ContextBrowsers
    from focus.runtime.stream_bridge.memory import MemoryStreamBridge

    async def scenario():
        engine, seed, (a, b, _) = await setup()
        fixture = SiteFixture()
        first = ContextBrowsers(seed.sessions, MemoryStreamBridge(), executable(), recovery=True, cache_root=tmp_path / 'cache')
        second, resources = None, []
        try:
            ra, rb = first.resource(a, 'download-before-exit'), first.resource(b, 'permission-before-exit')
            resources.extend((ra, rb))
            for resource in resources:
                await resource.start()
                view = await resource.observe()
                await resource.act(view['observation_id'], {'type': 'navigate', 'url': fixture.url()})
                await resource.observe()
            download_page, permission_page = ra.observation['page_id'], rb.observation['page_id']
            try:
                await ra.act(ra.observation['observation_id'], {'type': 'click', 'x': 80, 'y': 100}, 'css_viewport')
            except BrowserError as error:
                assert error.code in {'invalid_token', 'page_changed'}
            await wait_for(lambda: bool(ra.browser.downloads))
            try:
                await rb.act(rb.observation['observation_id'], {'type': 'click', 'x': 80, 'y': 160}, 'css_viewport')
            except BrowserError as error:
                assert error.code in {'invalid_token', 'page_changed'}
            await wait_for(lambda: bool(rb.browser.permissions))
            download_id = next(iter(ra.browser.downloads))
            permission_id = next(iter(rb.browser.permissions))
            assert rb.browser.permissions[permission_id]['state'] == 'pending'
            for resource in resources: await resource.finish()
            await first.close()  # Native disconnect cleanup; no permission choice is automated.
            second = ContextBrowsers(seed.sessions, MemoryStreamBridge(), executable(), recovery=True, cache_root=tmp_path / 'cache')
            await second.start()
            sa, sb = await second.snapshot(a), await second.snapshot(b)
            download = (await second.downloads(a))['downloads'][0]
            permission = (await second.downloads(b))['permissions'][0]
            assert sa['state'] == sb['state'] == 'manual'
            assert download['download_id'] == download_id and download['page_id'] == download_page
            assert download['state'] in {'cancelled', 'interrupted'}
            assert permission['request_id'] == permission_id and permission['page_id'] == permission_page
            assert permission['state'] == 'denied' and permission['reason'] == 'disconnected'
            assert not (await second.downloads(a))['permissions'] and not (await second.downloads(b))['downloads']
            assert fixture.requests.count('/slow-file') == 1
            renewed = second.resource(a, 'after-disconnect')
            resources.append(renewed); await renewed.start()
            await error_code('control_unavailable', renewed.observe())
        finally:
            for resource in resources: await resource.finish()
            owner = second or first
            for browser in tuple(owner.browsers.values()):
                if browser.state not in {'closed', 'disconnected'}:
                    await owner.transport_for(browser).call('close', browser_id=browser.browser_id)
            await owner.close(); fixture.close(); await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.usefixtures('runtime_postgres_database')
def test_real_gateway_restarts_preserve_pages_and_interrupt_old_run(tmp_path):
    import json
    import shutil
    import socket
    import subprocess
    import sys
    import threading
    import time
    import uuid
    import httpx
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from backend.app.desktop.persistence.models import DesktopThread
    from backend.app.desktop.browser.service import BINDING_KEY
    from backend.app.desktop.browser.win32_pipe import ReconnectClient, K, W
    from backend.tests.browser_fixture import BrowserFixture

    release = threading.Event()
    fixture = BrowserFixture(slow_release=release)
    source = tmp_path / 'current' / 'distinct-browser.exe'
    source.parent.mkdir(); shutil.copyfile(executable(), source)
    cache, audit = tmp_path / 'cache', tmp_path / 'model.jsonl'
    nonce = uuid.uuid4().hex
    environment = {**os.environ, 'PYTHONUTF8': '1',
        'FOCUS_DISTINCT_BROWSER_EXE': str(source), 'FOCUS_DISTINCT_BROWSER_CACHE': str(cache),
        'FOCUS_DESKTOP_SESSION': nonce, 'FOCUS_RECOVERY_SITE': fixture.url('').rstrip('/'), 'FOCUS_RECOVERY_AUDIT': str(audit)}
    environment.pop('FOCUS_DISTINCT_BROWSER_RECOVERY', None)  # Real Gateway exercises the Windows default.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    http = httpx.Client(base_url=f'http://127.0.0.1:{port}', headers={'X-Focus-Session': nonce}, timeout=20, trust_env=False)
    process, output, competitor, saved, native_process = None, None, None, None, None
    user32 = ctypes.WinDLL('user32')
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(W.DWORD)]
    user32.IsWindow.argtypes = [ctypes.c_void_p]

    def wait(work, predicate, seconds=35):
        deadline = time.monotonic() + seconds
        while True:
            value = work()
            if predicate(value): return value
            assert time.monotonic() < deadline, value
            time.sleep(.05)

    def boot(index):
        nonlocal process, output
        output = (tmp_path / f'gateway-{index}.log').open('w', encoding='utf-8')
        process = subprocess.Popen([sys.executable, '-u', 'backend/tests/browser_recovery_server.py', str(port)],
            env=environment, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
            text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 35
        while True:
            try:
                health = http.get('/desktop/api/bootstrap', timeout=8)
                if health.status_code == 200: return
            except httpx.HTTPError: pass
            assert process.poll() is None and time.monotonic() < deadline, (tmp_path / f'gateway-{index}.log').read_text(encoding='utf-8')[-4000:]
            time.sleep(.1)

    def stop(tree=False):
        if not process or process.poll() is not None: return
        if tree:
            result = subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            assert result.returncode == 0
        else:
            process.stdin.write('quit\n'); process.stdin.flush()
        process.wait(20); process.stdin.close(); output.close()

    def request(method, path, **kwargs):
        response = http.request(method, '/desktop/api' + path, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()

    def state(context):
        return request('GET', f'/contexts/{context}/browser')

    def inspect_ui(context, name, reconnect=False, initial=False):
        from playwright.sync_api import sync_playwright, expect
        policy = asyncio.get_event_loop_policy()
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1050}, bypass_csp=True)
                # Supply only Electron's runtime bridge. HTML, API and SSE are real.
                page.add_init_script('window.focusDesktop={runtime:()=>(' + json.dumps({
                    'apiBase': f'http://127.0.0.1:{port}', 'session': nonce}) + ')}')
                page.goto(f'http://127.0.0.1:{port}/desktop/', wait_until='domcontentloaded')
                page.wait_for_function("typeof switchTask === 'function' && state.tasks.length >= 3")
                page.evaluate('id => switchTask(id)', context)
                expect(page.get_by_role('button', name='浏览器绑定', exact=True)).to_be_visible()
                page.get_by_role('button', name='浏览器绑定', exact=True).click()
                panel = page.locator('dialog[open]')
                expect(panel).to_be_visible()
                if reconnect:
                    expect(panel.get_by_role('button', name='重新连接原宿主', exact=True)).to_be_visible()
                    expect(panel).to_have_css('opacity', '1')
                    page.screenshot(path=str(tmp_path / 'focus-v06-connection-busy.png'), full_page=True)
                    panel.get_by_role('button', name='重新连接原宿主', exact=True).click()
                    expect(panel).not_to_be_visible()
                    page.get_by_role('button', name='浏览器绑定', exact=True).click()
                    panel = page.locator('dialog[open]')
                if initial:
                    expect(panel.locator('[data-browser-recovery]')).to_contain_text('关闭 Focus 后浏览器仍会运行')
                    expect(panel.locator('[data-browser-recovery]')).to_contain_text('重新观察')
                else:
                    expect(panel.locator('[data-browser-recovery]')).to_contain_text('原宿主' if reconnect else '结果未知')
                    expect(page.locator('[data-browser-status]').first).to_contain_text('原会话已恢复')
                expect(panel).to_have_css('opacity', '1')
                page.screenshot(path=str(tmp_path / name), full_page=True)
                browser.close()
        finally:
            asyncio.set_event_loop_policy(policy)

    def run(context, case):
        return request('POST', f'/tasks/{context}/main/runs', json={
            'message': 'RECOVERY_CASE=' + nonce + '_' + case,
            'model_name': 'deepseek-v4-flash', 'access_mode': 'danger-full-access'})['run_id']

    def settled(identity):
        result = wait(lambda: request('GET', '/runs/' + identity), lambda value: value['status'] in {'success', 'error', 'interrupted'})
        assert result['status'] == 'success', result
        return result

    async def binding(context):
        engine = create_async_engine(os.environ['FOCUS_DATABASE_URL'])
        try:
            async with async_sessionmaker(engine)() as session:
                return dict((await session.get(DesktopThread, context)).ui_state[BINDING_KEY])
        finally: await engine.dispose()

    try:
        boot(1)
        work = tmp_path / 'workspace'; work.mkdir()
        workspace = request('POST', '/workspaces', json={'path': str(work)})['workspace_id']
        contexts = [request('POST', f'/workspaces/{workspace}/threads', json={'thread_id': uuid.uuid4().hex, 'title': title})['task_id']
                    for title in ('Recovery A', 'Shared A', 'Recovery B')]
        a, shared, b = contexts
        for context in contexts:
            request('PUT', f'/tasks/{context}/access-mode', json={'access_mode': 'danger-full-access'})
        first_a = request('PUT', f'/contexts/{a}/browser', json={})
        request('PUT', f'/contexts/{shared}/browser', json={'source_context_id': a})
        first_b = request('PUT', f'/contexts/{b}/browser', json={})
        settled(run(a, 'A')); settled(run(b, 'B'))
        original_pages = request('GET', f'/contexts/{a}/browser/pages')
        assert len(original_pages['pages']) == 2
        original_b = request('GET', f'/contexts/{b}/browser/pages')
        saved = asyncio.run(binding(a))
        assert set(saved) == {'host_id', 'browser_id', 'transport', 'endpoint', 'artifact_sha256'}
        image = cache / saved['artifact_sha256'] / 'distinct-browser.exe'
        assert image.is_file() and image != source
        inspect_ui(a, 'focus-v06-default-lifecycle.png', initial=True)
        stop()
        assert all(user32.IsWindow(int(page['hwnd'])) for page in original_pages['pages'])
        source.unlink()  # An installation may move/remove the original image; the live cache must suffice.

        competitor = ReconnectClient(saved['endpoint'], image, expected_host_id=saved['host_id'], expected_sha256=saved['artifact_sha256'])
        boot(2)
        blocked = state(a)
        assert blocked['browser_id'] == first_a['browser_id'] and blocked['state'] == 'disconnected'
        assert blocked['detail']['reason'] == 'connection_busy', blocked
        competitor.disconnect(); competitor = None
        inspect_ui(a, 'focus-v06-restored.png', reconnect=True)
        restored = state(a)
        assert restored['state'] == 'manual' and restored['recovered']
        assert state(shared)['browser_id'] == restored['browser_id'] == first_a['browser_id']
        assert state(b)['browser_id'] == first_b['browser_id']
        current_pages = request('GET', f'/contexts/{a}/browser/pages')
        assert {p['page_id']: p['hwnd'] for p in current_pages['pages']} == {p['page_id']: p['hwnd'] for p in original_pages['pages']}
        assert request('GET', f'/contexts/{b}/browser/pages')['pages'] == original_b['pages']
        hwnd = int(next(page['hwnd'] for page in current_pages['pages'] if page['page_id'] == current_pages['selected_page_id']))
        user32.SendMessageW(hwnd, 0x0111, 101, 0)
        wait(lambda: state(a), lambda value: value['state'] == 'available')
        held_run = run(a, 'HOLD')
        assert fixture.slow_started.wait(10)
        assert request('GET', '/runs/' + held_run)['status'] == 'running'
        pid = W.DWORD(); user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        native_process = K.OpenProcess(0x101001, False, pid.value)
        assert native_process
        stop(tree=True)
        assert K.WaitForSingleObject(native_process, 0) == 258 and user32.IsWindow(hwnd)
        calls_before = sum(1 for line in audit.read_text(encoding='utf-8').splitlines() if json.loads(line)['case'].endswith('_HOLD'))
        boot(3)
        assert request('GET', '/runs/' + held_run)['status'] == 'interrupted'
        interrupted = state(a)
        assert interrupted['browser_id'] == first_a['browser_id'] and interrupted['interrupted_operation']['result_unknown']
        assert interrupted['state'] in {'taking_over', 'manual'}
        release.set()
        wait(lambda: state(a), lambda value: value['state'] == 'manual' and value['interrupted_operation'].get('settled'))
        inspect_ui(a, 'focus-v06-interrupted.png')
        assert calls_before == sum(1 for line in audit.read_text(encoding='utf-8').splitlines() if json.loads(line)['case'].endswith('_HOLD'))
        user32.SendMessageW(hwnd, 0x0111, 101, 0)
        wait(lambda: state(a), lambda value: value['state'] == 'available')
        settled(run(a, 'RESUME'))
        assert state(b)['state'] == 'manual'
        K.TerminateProcess.argtypes = [W.HANDLE, W.UINT]
        assert K.TerminateProcess(native_process, 0); K.CloseHandle(native_process); native_process = None
        wait(lambda: state(a), lambda value: value['state'] == 'disconnected')
        stop(); boot(4)
        lost = state(a)
        assert lost['browser_id'] == first_a['browser_id'] and lost['state'] == 'disconnected'
        assert lost['detail']['reason'] == 'host_unrecoverable'
        assert http.get(f'/desktop/api/contexts/{a}/browser/pages').status_code == 409
        print('PASS real Gateway processes: graceful restart, competing owner, shared multi-page binding, installation-path removal, forced process-tree exit, old Run interrupted, no replay, explicit handback and dead-host refusal')
    finally:
        release.set()
        if competitor: competitor.disconnect()
        if process and process.poll() is None: stop()
        if native_process: K.CloseHandle(native_process)
        if saved:
            # Clean only this test's authenticated host, including assertion failures.
            try:
                owned = ReconnectClient(saved['endpoint'], cache / saved['artifact_sha256'] / 'distinct-browser.exe',
                    expected_host_id=saved['host_id'], expected_sha256=saved['artifact_sha256'])
            except (OSError, ConnectionError):
                pass
            else:
                try:
                    for item in owned.call('snapshot')['browsers']: owned.call('close', browser_id=item['browser_id'])
                finally: owned.disconnect()
        http.close(); fixture.close()


@pytest.mark.usefixtures('isolated_postgres_database')
def test_service_restart_restores_shared_contexts_pages_and_never_upgrades_legacy_binding(tmp_path, monkeypatch):
    from backend.tests.test_context_browser_real import setup, wait_for, error_code
    from backend.tests.browser_fixture import BrowserFixture
    from backend.app.desktop.browser.service import ContextBrowsers, BINDING_KEY
    from backend.app.desktop.persistence.models import DesktopThread
    from focus.runtime.stream_bridge.memory import MemoryStreamBridge
    from types import SimpleNamespace
    import uuid

    async def scenario():
        engine, seed, (a, b, c) = await setup()
        first = ContextBrowsers(seed.sessions, MemoryStreamBridge(), executable(), recovery=True, cache_root=tmp_path / 'cache')
        second, disabled, resources, fixture = None, None, [], BrowserFixture()
        user32 = ctypes.WinDLL('user32')
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.IsWindow.argtypes = [ctypes.c_void_p]
        try:
            ra, rb = first.resource(a, 'first-A'), first.resource(b, 'first-B')
            resources.extend((ra, rb))
            await ra.start(); await rb.start()
            old_view = None
            initial = {}
            for resource, name in ((ra, 'A'), (rb, 'B')):
                view = await resource.observe()
                await resource.act(view['observation_id'], {'type': 'navigate', 'url': fixture.url(name)})
                view = await resource.observe()
                await resource.act(view['observation_id'], {'type': 'click', 'x': 80, 'y': 40}, 'css_viewport')
                await resource.act(view['observation_id'], {'type': 'text', 'text': 'persist ' + name})
                initial[name] = await asyncio.to_thread(fixture.wait, lambda item: item['name'] == name and item['text'] == 'persist ' + name)
                old_view = view
            view = await ra.observe()
            await ra.act(view['observation_id'], {'type': 'click', 'x': 80, 'y': 220}, 'css_viewport')
            await asyncio.to_thread(fixture.wait, lambda item: item['name'] == 'Child')
            pages = await ra.list_pages()
            assert len(pages['pages']) == 2
            aid, bid = ra.browser.browser_id, rb.browser.browser_id
            await ra.finish(); await rb.finish()
            await first.replace(c, a)
            async with seed.sessions.begin() as session:
                context = await session.get(DesktopThread, a)
                legacy = uuid.uuid4().hex
                session.add(DesktopThread(task_id=legacy, workspace_id=context.workspace_id, thread_id=uuid.uuid4().hex,
                    title='Legacy browser', ui_state={BINDING_KEY: {'browser_id': 'legacy', 'host_id': 'old-stdio'}}))
            await first.close()
            monkeypatch.setenv('FOCUS_DISTINCT_BROWSER_RECOVERY', '0')
            disabled = ContextBrowsers(seed.sessions, MemoryStreamBridge(), executable(), cache_root=tmp_path / 'cache')
            await disabled.start()
            inactive = await disabled.snapshot(a)
            assert not disabled.recovery and inactive['browser_id'] == aid and inactive['detail']['reason'] == 'recovery_disabled'
            await error_code('disconnected', disabled.ensure(a))
            assert disabled.transport.pid is None and all(user32.IsWindow(int(page['hwnd'])) for page in pages['pages'])
            await disabled.close()
            monkeypatch.delenv('FOCUS_DISTINCT_BROWSER_RECOVERY')
            second = ContextBrowsers(seed.sessions, MemoryStreamBridge(), executable(), cache_root=tmp_path / 'cache')
            assert second.recovery
            await second.start()
            sa, sb, sc = await second.snapshot(a), await second.snapshot(b), await second.snapshot(c)
            assert sa['browser_id'] == sc['browser_id'] == aid and sb['browser_id'] == bid
            assert set(sa['shared_context_ids']) == {a, c} and sa['recovered'] and sa['state'] == 'manual'
            assert sb['state'] == 'manual' and second.transport.host_id == first.transport.host_id
            actual_pages = await second.pages(a)
            assert actual_pages['selected_page_id'] == pages['selected_page_id']
            assert {p['page_id']: p['hwnd'] for p in actual_pages['pages']} == {p['page_id']: p['hwnd'] for p in pages['pages']}
            before = await second.transport.call('snapshot')
            assert (await second.snapshot(legacy))['detail']['reason'] == 'legacy_session'
            await error_code('disconnected', second.ensure(legacy))
            assert len((await second.transport.call('snapshot'))['browsers']) == len(before['browsers']) == 2
            resumed = second.resource(a, 'new-A')
            resources.append(resumed)
            await resumed.start()
            await error_code('control_unavailable', resumed.observe())
            user32.SendMessageW(int(resumed.browser.hwnd), 0x0111, 101, 0)
            await wait_for(lambda: resumed.browser.state == 'available')
            await second.present(a, SimpleNamespace(client_id='test', sequence=1, page_id=view['page_id']))
            fresh = await resumed.observe()
            assert fresh['observation_id'] != old_view['observation_id']
            await error_code('observation_required', resumed.act(old_view['observation_id'], {'type': 'key', 'key': 'Tab'}))
            await resumed.act(fresh['observation_id'], {'type': 'click', 'x': 80, 'y': 40}, 'css_viewport')
            await resumed.act(fresh['observation_id'], {'type': 'key', 'key': 'End'})
            await resumed.act(fresh['observation_id'], {'type': 'text', 'text': '-after'})
            current = await asyncio.to_thread(fixture.wait, lambda item: item['name'] == 'A' and item['text'] == 'persist A-after')
            assert current['cookie'] == initial['A']['cookie'] and current['storage'] == initial['A']['storage']
            assert (await second.snapshot(b))['state'] == 'manual'
        finally:
            if disabled: await disabled.close()
            for resource in resources: await resource.finish()
            owner = second or first
            for browser in tuple(owner.browsers.values()):
                if browser.state not in {'closed', 'disconnected'}:
                    await owner.transport_for(browser).call('close', browser_id=browser.browser_id)
            await owner.close()
            fixture.close(); await engine.dispose()
    asyncio.run(scenario())
