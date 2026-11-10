"""Browser interaction/layout check for the real settings markup and storage pane.

Run with Python Playwright installed; optional argument selects screenshot folder.
The API is a deterministic fixture; database and backup behaviour have separate
isolated PostgreSQL tests.
"""
from pathlib import Path
import sys
import tempfile

from playwright.sync_api import sync_playwright

desktop = Path(__file__).resolve().parents[2] / "src/renderer"
output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="focus-storage-ui-"))
output.mkdir(parents=True, exist_ok=True)
source = (desktop / "index.html").read_text(encoding="utf-8")
dialog = source[source.index('<dialog id="settingsDialog"'):]
dialog = dialog[:dialog.index("</dialog>") + len("</dialog>")]

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1100, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_content(f'<!doctype html><html lang="zh-CN"><body>{dialog}</body></html>')
    for sheet in ("shared/styles/styles.css", "shared/styles/tokens.css", "shared/styles/base.css", "shared/styles/icons.css", "shared/styles/components.css", "shared/styles/views.css"):
        page.add_style_tag(path=str(desktop / sheet))
    page.add_script_tag(path=str(desktop / "features/settings/storage-settings.js"))
    page.evaluate("""async () => {
      document.querySelectorAll('[data-settings-pane]').forEach(pane => pane.hidden = pane.dataset.settingsPane !== 'data');
      document.querySelectorAll('[data-settings-tab]').forEach(tab => {
        tab.classList.toggle('is-active', tab.dataset.settingsTab === 'data');
        tab.setAttribute('aria-selected', String(tab.dataset.settingsTab === 'data'));
      });
      window.calls = [];
      const snapshot = { policy: { journal_retention_days: 7, backup_retention_days: 30,
        backup_keep_count: 3, automatic_cleanup: false, backup_interval_hours: 0 },
        database_bytes: 100000000, journal_bytes: 10000000, journal_events: 12000,
        backup_available: true, backup_directory: 'C:/Users/example/.focus/backups',
        backups: { bytes: 300000000, entries: [{ id: 'update-1792200000000-aabbccdd', bytes: 300000000,
          created_at: '2026-10-17T00:00:00Z', protected_reason: 'accepted_recovery_point', kind: 'update' }] } };
      window.controller = FocusStorageSettings.create({ host: document.querySelector('#storageSettingsHost'),
        english: () => Boolean(window.english), escapeHtml: value => {
          const span = document.createElement('span'); span.textContent = String(value); return span.innerHTML;
        }, api: async (url, options = {}) => {
          calls.push({ url, body: options.body ? JSON.parse(options.body) : null });
          if (url.endsWith('/policy')) { snapshot.policy = JSON.parse(options.body); return snapshot.policy; }
          if (url.endsWith('/preview')) return { token: `fixture-token-${calls.length}`, expires_in_seconds: 600, journal_events: 200, backup_ids: [], backup_bytes: 0 };
          if (url.endsWith('/cleanup')) {
            if (window.expireCleanup) throw Error('预览已过期，请重新预览');
            return { journal_events_removed: 200, backups_removed: [] };
          }
          if (url.endsWith('/backup')) return { id: 'regular-fixture-backup' };
          return snapshot;
        } });
      document.querySelector('#settingsDialog').showModal();
      await controller.load();
    }""")
    page.wait_for_load_state("networkidle")
    host = page.locator("#storageSettingsHost")
    assert host.get_by_text("长期存储维护", exact=True).is_visible()
    assert host.get_by_role("region", name="存储容量").count() == 1
    assert host.get_by_role("group", name="新建数据库备份范围").get_by_text("工作区文件、升级依赖快照").is_visible()
    usage = host.locator(".storage-usage")
    assert usage.locator(":scope > div").count() == 2
    journal = usage.get_by_text("其中 Journal 日志", exact=True)
    assert journal.bounding_box()["x"] > usage.get_by_text("数据库总量", exact=True).bounding_box()["x"]
    assert host.get_by_role("button", name="执行本次清理").is_disabled()
    page.screenshot(path=str(output / "storage-capacity-desktop.png"), full_page=True)
    host.get_by_role("button", name="预览安全清理").click()
    page.wait_for_selector(".storage-preview")
    count = host.locator('[data-storage-field="backup_keep_count"]')
    count.fill("4")
    assert count.evaluate("el => el === document.activeElement"), "policy editing lost focus"
    assert page.locator(".storage-preview").count() == 0
    assert host.get_by_role("button", name="执行本次清理").is_disabled()
    host.get_by_role("button", name="预览安全清理").click()
    assert host.get_by_text("请先保存策略，再预览清理", exact=True).is_visible()
    host.get_by_role("button", name="保存策略").click()
    host.get_by_role("button", name="预览安全清理").click()
    page.wait_for_selector(".storage-preview")
    assert host.get_by_role("region", name="本次最多清理").is_visible()
    assert host.locator(".storage-cleanup-steps > li").count() == 2
    assert host.get_by_role("button", name="执行本次清理").is_enabled()
    host.get_by_role("region", name="安全清理").scroll_into_view_if_needed()
    page.screenshot(path=str(output / "storage-desktop.png"), full_page=True)
    page.evaluate("window.expireCleanup = true")
    host.get_by_role("button", name="执行本次清理").click()
    assert host.locator('[data-storage-status="cleanup"]').inner_text() == "预览已过期，请重新预览"
    assert page.locator(".storage-preview").count() == 0
    assert host.get_by_role("button", name="执行本次清理").is_disabled()
    page.evaluate("window.expireCleanup = false")
    host.get_by_role("button", name="预览安全清理").click()
    # The preview token remains private UI state and only the token is sent on execution.
    assert "fixture-token" not in host.inner_text()
    host.get_by_role("button", name="执行本次清理").click()
    assert host.locator('[data-storage-status="cleanup"]').inner_text() == "已清理 200 条日志、0 个备份"
    cleanup = page.evaluate("calls.filter(row => row.url.endsWith('/cleanup'))")
    assert len(cleanup) == 2 and cleanup[0]["body"]["token"] != cleanup[1]["body"]["token"]
    assert list(cleanup[1]["body"]) == ["token"]
    host.get_by_role("button", name="立即备份数据库").click()
    assert host.get_by_text("数据库备份已完成：regular-fixture-backup", exact=True).is_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    host.get_by_role("region", name="存储容量").scroll_into_view_if_needed()
    page.screenshot(path=str(output / "storage-mobile.png"), full_page=True)
    assert page.locator("#settingsDialog").evaluate("el => el.scrollWidth <= el.clientWidth + 1"), "settings overflow horizontally"
    page.evaluate("english = true; controller.render()")
    assert host.get_by_role("button", name="Back up database now").is_visible()
    assert host.get_by_role("region", name="Storage usage").count() == 1
    assert host.get_by_role("group", name="New database backup scope").get_by_text("Workspace files and upgrade dependency snapshots").is_visible()
    host.get_by_role("region", name="Safe cleanup").scroll_into_view_if_needed()
    page.screenshot(path=str(output / "storage-cleanup-narrow-en.png"), full_page=True)
    assert not errors, errors
    browser.close()
print(f"Storage browser checks passed; screenshots: {output}")
