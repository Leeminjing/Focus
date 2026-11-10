"""Real-page regression for empty conversations and the archive destination.

Uses deterministic API fixtures; no database, model, or user data is touched.
Run: python desktop/tests/e2e/conversation-empty.browser.py [screenshot directory]
"""
from pathlib import Path
import os
import sys
import tempfile

from playwright.sync_api import expect, sync_playwright

DESKTOP = Path(__file__).resolve().parents[2] / "src/renderer"
OUTPUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="focus-empty-qa-"))
OUTPUT.mkdir(parents=True, exist_ok=True)
HARNESS = r"""
(() => {
  const base = window.fetch;
  const reply = value => new Response(JSON.stringify(value), {headers: {'Content-Type': 'application/json'}});
  const free = ['global', 'workspace'].map(scope => ({task_id: 'free-' + scope, title: scope,
    harness_mode: 'assembly', workspace_id: 'workspace', workspace_path: 'C:/workspace',
    assembly_target: {scope, workspace_id: scope === 'workspace' ? 'workspace' : null}, active_run: null}));
  window.emptyHarness = { calls: [], archived: ['archive-view', 'archive-delete'], hold: false, release: null };
  window.fetch = async (url, options = {}) => {
    const path = new URL(String(url), 'http://focus.test').pathname;
    emptyHarness.calls.push({path, method: options.method || 'GET'});
    if (path === '/desktop/api/sessions/archived') {
      if (emptyHarness.hold) await new Promise(resolve => { emptyHarness.release = resolve; });
      return reply(emptyHarness.archived.map(context_id => ({context_id, title: context_id, workspace_name: '测试工作区'})));
    }
    if (/\/contexts\/archive-[^/]+(?:\/unarchive)?$/.test(path)) {
      emptyHarness.archived = emptyHarness.archived.filter(id => !path.includes('/' + id));
      return reply({});
    }
    if (path.startsWith('/desktop/api/history/')) return reply({context_id: path.split('/').at(-1),
      title: '归档只读记录', lifecycle: 'archived', workspace_name: '测试工作区', entries: [], materials: []});
    if (path === '/desktop/api/settings/models') return reply({models: [], editable_fields: [], shipped_models: []});
    if (path === '/desktop/api/plugins') return reply({plugins: []});
    if (/\/tasks\/[^/]+\/changes$/.test(path)) return reply({files: []});
    if (path === '/desktop/api/sandbox/status') return reply({});
    if (path === '/desktop/api/shell-recovery') return reply({runs: []});
    if (path === '/desktop/api/settings/storage') return reply({policy: {journal_retention_days: 7,
      backup_retention_days: 30, backup_keep_count: 3, backup_interval_hours: 0}, journal_events: 0, backups: {entries: []}});
    const response = await base(url, options);
    if (path === '/desktop/api/bootstrap' || path === '/desktop/api/tasks') {
      const body = await response.json();
      if (Array.isArray(body)) body.push(...free); else body.tasks.push(...free);
      return reply(body);
    }
    if (/\/tasks\/[^/]+$/.test(path)) {
      const body = await response.json(); body.messages = []; return reply(body);
    }
    return response;
  };
})();
"""

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, **(
        {"executable_path": os.environ["FOCUS_BROWSER_EXECUTABLE"]} if os.environ.get("FOCUS_BROWSER_EXECUTABLE") else {}))
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script((DESKTOP / "../../tests/fixtures/context-ui-test-preload.cjs").read_text(encoding="utf-8") + HARNESS)
        page.goto((DESKTOP / "index.html").as_uri())
        page.wait_for_load_state("networkidle")
        page.evaluate("state.inspector.open = false")
        page.evaluate("switchTask('root')")
        expect(page.locator('#conversation .message')).to_have_count(0)
        page.get_by_role('button', name='输入任务', exact=True).click()
        expect(page.locator('#mainInput')).to_be_focused()
        page.locator('#mainInput').fill('普通会话草稿')
        page.screenshot(path=str(OUTPUT / 'empty-conversation.png'), full_page=True)

        # Production task switching keeps workspace and both assembly targets isolated.
        for task, draft in [('free-global', '全局独立草稿'), ('free-workspace', '工作区独立草稿')]:
            page.evaluate('(id) => switchTask(id)', task)
            expect(page.locator('#mainInput')).to_have_value('')
            page.locator('#mainInput').fill(draft)
        for task, draft in [('root', '普通会话草稿'), ('free-global', '全局独立草稿'), ('free-workspace', '工作区独立草稿')]:
            page.evaluate('(id) => switchTask(id)', task)
            expect(page.locator('#mainInput')).to_have_value(draft)

        # A slow archive request must not delay the promised tab and focus destination.
        page.evaluate("""async () => {
          await openSettings(); selectSettingsTab('models');
          await FocusSurfaceTransition.visible(settingsDialog, false, {modal: true});
          emptyHarness.hold = true; renderNoActiveTask();
        }""")
        page.get_by_role('button', name='查看已归档会话', exact=True).click()
        target = page.get_by_role('region', name='已归档的会话', exact=True)
        expect(target).to_be_focused()
        expect(page.get_by_role('tab', name='数据', exact=True)).to_have_attribute('aria-selected', 'true')
        expect(page.get_by_role('button', name='查看已归档会话', exact=True)).not_to_have_attribute('aria-selected', 'true')
        page.wait_for_function('() => Boolean(emptyHarness.release)')
        page.evaluate('emptyHarness.hold = false; emptyHarness.release()')
        expect(page.locator('#archivedSessions .archived-session-item')).to_have_count(2)
        page.wait_for_function("() => document.querySelector('#storageSettingsHost [data-storage-action]') !== null")
        page.wait_for_function("() => Number(getComputedStyle(document.querySelector('#settingsDialog')).opacity) >= 0.99")
        expect(target).to_be_focused()
        assert target.evaluate('el => { const r=el.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }')
        page.screenshot(path=str(OUTPUT / 'archive-destination.png'), full_page=True)

        page.locator('[data-context-id="archive-view"] [data-action="view-history"]').click()
        expect(page.locator('.history-reader')).to_be_visible()
        expect(page.locator('#mainInput')).to_have_count(0)
        expect(page.locator('[data-action="history-continue"]')).to_have_count(0)
        page.evaluate("openSettings({tab: 'data', section: 'archived'})")
        page.locator('[data-context-id="archive-view"] [data-action="unarchive-context"]').click()
        expect(page.locator('#archivedSessions .archived-session-item')).to_have_count(1)
        assert page.evaluate("emptyHarness.calls.some(c => c.path.endsWith('/archive-view/unarchive') && c.method === 'POST')")

        # Cancel really performs no DELETE; accepting the same confirmation does.
        confirmations = []
        def cancel(dialog):
            confirmations.append(dialog.message)
            dialog.dismiss()
        page.on('dialog', cancel)
        page.locator('[data-context-id="archive-delete"] [data-action="delete-context"]').click()
        assert confirmations and '不可恢复' in confirmations[-1]
        assert not page.evaluate("emptyHarness.calls.some(c => c.method === 'DELETE')")
        page.remove_listener('dialog', cancel)
        page.once('dialog', lambda dialog: dialog.accept())
        page.locator('[data-context-id="archive-delete"] [data-action="delete-context"]').click()
        page.wait_for_function("() => emptyHarness.calls.some(c => c.method === 'DELETE')")
        expect(page.locator('#settingsDialog')).not_to_be_visible()

        # No archived rows still gives an accessible destination, including English.
        page.evaluate("FocusI18n.setLocale('en-US'); renderNoActiveTask()")
        page.get_by_role('button', name='View archived conversations', exact=True).click()
        expect(page.locator('#settingsDialog [data-settings-section="archived"]')).to_be_focused()
        expect(page.locator('#archivedSessions .archived-session-item')).to_have_count(0)
        expect(page.locator('#archivedSessions')).to_contain_text('No archived')

        # A late load must leave a user's subsequent tab choice and focus intact.
        page.evaluate("""() => { emptyHarness.hold = true; emptyHarness.release = null;
          emptyHarness.pending = openSettings({tab: 'data', section: 'archived'}); }""")
        page.wait_for_function('() => Boolean(emptyHarness.release)')
        page.get_by_role('tab', name='General', exact=True).click()
        page.evaluate('async () => { emptyHarness.hold = false; emptyHarness.release(); await emptyHarness.pending; }')
        page.wait_for_function("() => !document.querySelector('[data-settings-pane=general]').hidden")
        expect(page.get_by_role('tab', name='General', exact=True)).to_be_focused()
        assert not errors, errors
    finally:
        browser.close()
print(f'Empty conversation and archive checks passed; screenshots: {OUTPUT}')
