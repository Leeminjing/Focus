"""Settings regression with production HTML, CSS, scripts and event handlers.

The API and native notifications are fixtures. Covers tab focus/loading,
diagnostic disclosure, recovery evidence, field persistence and narrow layouts.
Run: python desktop/tests/e2e/settings.browser.py [screenshot directory]
"""
from pathlib import Path
import re
import sys
import tempfile

from playwright.sync_api import sync_playwright, expect

renderer = Path(__file__).resolve().parents[2] / "src/renderer"
output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="focus-settings-ui-"))
output.mkdir(parents=True, exist_ok=True)
source = (renderer / "index.html").read_text(encoding="utf-8")
html = re.sub(r'<script\b[^>]*>[\s\S]*?</script>', '', source)
html = re.sub(r'<meta http-equiv="Content-Security-Policy"[^>]*>', '', html)
html = html.replace('<head>', f'<head><base href="{(renderer / "index.html").as_uri()}">')

with tempfile.TemporaryDirectory(prefix="focus-settings-fixture-") as temp, sync_playwright() as p:
    fixture = Path(temp) / "settings.html"
    fixture.write_text(html, encoding="utf-8")
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1100, "height": 900}, reduced_motion="reduce")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(fixture.as_uri())
    page.evaluate("""() => {
      localStorage.clear();
      window.notificationEnabled = true;
      window.focusDesktop = { notifications: {
        settings: async enabled => {
          if (window.notificationFailure) throw new Error('Notification settings unavailable');
          if (typeof enabled === 'boolean') notificationEnabled = enabled;
          return { enabled: notificationEnabled };
        }, onTasks() {}, onOpen() {}
      } };
    }""")
    # Preserve production ordering; bootstrap is the only removed application code.
    for src in re.findall(r'<script src="([^"]+)"', source):
        script = renderer / src.split('?')[0]
        if not script.exists():
            assert script.name == "patrol-json-editor.bundle.js", f"Missing script: {script}"
            continue
        code = script.read_text(encoding="utf-8")
        if script.name == "app.js":
            code, count = re.subn(r'runUiAction\(async \(\) => \{ await bootstrap\(\); if \(desktopPatrol\) await desktopPatrol.ready\(\); \}\);\s*$', '', code)
            assert count == 1, "Bootstrap entry changed; update this fixture"
        page.add_script_tag(content=code)
    page.evaluate("""() => {
      window.calls = [];
      window.sandboxFailure = false;
      window.recoveryFailure = false;
      window.sandbox = { available: true, enforcement: 'partial', limited_label: '已启用，部分约束',
        prepared_workspaces: ['C:/work/<project>'], limitations: [
          '不保证读取保密、网络隔离、进程隐藏或独立控制台；并非完整虚拟机隔离或独立 Windows 用户身份。',
          'NTFS 硬链接可能使工作区内外路径指向同一文件对象，不保证路径级绝对隔离。'
        ] };
      window.recoveryRuns = [];
      window.storage = { policy: { journal_retention_days: 30, backup_retention_days: 7,
        backup_keep_count: 2, backup_interval_hours: 24, automatic_cleanup: true },
        database_bytes: 10000000, journal_bytes: 100000, journal_events: 120,
        backup_available: true, backup_directory: 'C:/Users/example/.focus/backups',
        backups: { bytes: 1000000, entries: [] } };
      api = async (path, options = {}) => {
        calls.push({ path, options });
        if (path.endsWith('/sessions/archived')) return [];
        if (path.endsWith('/settings/models')) return { models: [], shipped_models: [], editable_fields: [], removed_models: [] };
        if (path.endsWith('/settings/storage')) return storage;
        if (path.endsWith('/sandbox/status')) {
          if (sandboxFailure) throw new Error('Sandbox status unavailable');
          return sandbox;
        }
        if (path.endsWith('/shell-recovery')) {
          if (recoveryFailure) throw new Error('Exit evidence unavailable');
          if (options.method === 'POST') { recoveryRuns = []; return { status: 'interrupted' }; }
          return recoveryRuns;
        }
        throw new Error(`Unexpected API call: ${path}`);
      };
      return openSettings();
    }""")
    page.wait_for_load_state("networkidle")
    expect(page.get_by_role("dialog", name="设置", exact=True)).to_be_visible()
    dialog = page.locator("#settingsDialog")
    tabs = dialog.get_by_role("tab")

    def active(name):
        chosen = dialog.get_by_role("tab", name=name, exact=True)
        expect(chosen).to_be_focused()
        expect(chosen).to_have_attribute("aria-selected", "true")
        assert tabs.evaluate_all("tabs => tabs.filter(t => t.tabIndex === 0).length") == 1
        panel_id = chosen.get_attribute("aria-controls")
        panel = page.locator(f"#{panel_id}")
        expect(panel).to_be_visible()
        expect(panel).to_have_attribute("role", "tabpanel")
        assert panel.get_attribute("aria-labelledby") == chosen.get_attribute("id")
        assert dialog.locator('[role="tabpanel"]:visible').count() == 1

    # Automatic activation, circular arrows, Home/End, and Tab into a panel.
    tabs.first.focus()
    active("常规")
    page.keyboard.press("ArrowRight")
    active("模型")
    assert page.evaluate("calls.some(c => c.path.endsWith('/settings/models'))")
    page.keyboard.press("ArrowRight")
    active("数据")
    expect(page.locator('[data-storage-field="journal_retention_days"]')).to_have_value("30")
    page.keyboard.press("ArrowRight")
    active("常规")
    page.keyboard.press("ArrowLeft")
    active("数据")
    before = page.evaluate("calls.filter(c => c.path.endsWith('/settings/storage')).length")
    page.keyboard.press("Home")
    active("常规")
    page.keyboard.press("End")
    active("数据")
    assert page.evaluate("calls.filter(c => c.path.endsWith('/settings/storage')).length") == before + 1
    page.keyboard.press("Tab")
    expect(page.locator("#settingsPane-data")).to_be_focused()
    tabs.first.click()

    # Healthy diagnostics collapse, with real restrictions accessible by keyboard.
    expect(page.locator("#sandboxDiagnostics")).not_to_have_attribute("open", "")
    expect(page.locator("#shellRecoveryDiagnostics")).not_to_have_attribute("open", "")
    page.screenshot(path=str(output / "settings-general.png"))
    summary = page.locator("#sandboxDiagnostics > summary")
    summary.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#sandboxStatusHost")).to_contain_text("网络隔离")
    expect(page.locator("#sandboxStatusHost")).to_contain_text("硬链接")
    expect(page.locator("#sandboxStatusHost")).to_contain_text("C:/work/<project>")
    expect(page.locator("#sandboxStatusHost")).to_contain_text("完全访问：未应用文件沙箱")

    mode = page.locator("#newSessionAccessMode")
    mode.select_option("read-only")
    assert page.evaluate("localStorage.getItem('focus-new-session-file-mode-v1')") == "read-only"
    expect(page.locator("#generalSettingsFeedback")).to_have_text("已保存，仅用于新会话")
    notifications = page.get_by_role("checkbox", name="启用桌面提醒", exact=True)
    notifications.uncheck()
    expect(page.locator("#notificationSettingsStatus")).to_have_text("已关闭")
    page.evaluate("notificationFailure = true")
    notifications.click()
    expect(notifications).not_to_be_checked()
    expect(page.locator("#generalSettingsFeedback")).to_have_text("Notification settings unavailable")
    page.evaluate("notificationFailure = false")

    # Reopening restores the saved state; unavailable sandbox and unknown effects expand.
    page.evaluate("""async () => {
      sandbox.available = false; sandbox.enforcement = 'unavailable'; sandbox.limited_label = '受限后端不可用';
      recoveryRuns = [{ run_id: 'blocked-run', title: '<script>unsafe</script>', status: 'interrupted',
        directory: 'C:/work/<project>', attempts: [{ tool: 'powershell', call_id: 'shell-call', state: 'unknown' }] }];
      await openSettings();
    }""")
    expect(mode).to_have_value("read-only")
    expect(page.locator("#sandboxDiagnostics")).to_have_attribute("open", "")
    expect(page.locator("#shellRecoveryDiagnostics")).to_have_attribute("open", "")
    recovery = page.locator("#shellRecoveryHost")
    expect(recovery).to_contain_text("执行结果未知")
    expect(recovery).to_contain_text("不会自动重放命令")
    expect(recovery).to_contain_text("重启 Windows")
    expect(recovery).to_contain_text("<script>unsafe</script>")
    assert recovery.locator("script").count() == 0
    page.screenshot(path=str(output / "settings-diagnostics.png"), full_page=True)
    page.evaluate("recoveryFailure = true")
    recovery.get_by_role("button", name="核验退出并恢复").click()
    expect(page.locator("#shellRecoveryFeedback")).to_have_text("Exit evidence unavailable")
    expect(page.locator("#shellRecoveryDiagnostics")).to_have_attribute("open", "")
    page.evaluate("() => { recoveryFailure = false; return loadShellRecovery(); }")
    recovery.get_by_role("button", name="核验退出并恢复").click()
    expect(page.locator("#shellRecoverySummary")).to_have_text("无待核验项")
    expect(page.locator("#shellRecoveryFeedback")).to_contain_text("请核对命令副作用")
    assert page.evaluate("calls.filter(c => c.options.method === 'POST').length") == 2

    page.evaluate("() => { sandboxFailure = true; recoveryFailure = true; return openSettings(); }")
    expect(page.locator("#sandboxStatusSummary")).to_have_text("读取失败，状态未知")
    expect(page.locator("#sandboxStatusHost")).to_contain_text("Sandbox status unavailable")
    expect(page.locator("#shellRecoverySummary")).to_have_text("读取失败，状态未知")
    expect(page.locator("#shellRecoveryDiagnostics")).to_have_attribute("open", "")
    page.evaluate("() => { sandboxFailure = false; recoveryFailure = false; sandbox.available = true; sandbox.enforcement = 'partial'; sandbox.limited_label = '已启用，部分约束'; return openSettings(); }")
    mode.select_option("workspace-write")

    # Native language controls use the same focus navigation and preserve selection.
    chinese = dialog.locator('[role="radio"][data-locale="zh-CN"]')
    chinese.focus()
    page.keyboard.press("ArrowRight")
    expect(page.get_by_role("radio", name="English", exact=True)).to_be_focused()
    expect(page.get_by_role("radio", name="English", exact=True)).to_have_attribute("aria-checked", "true")
    expect(page.locator("#settingsTitle")).to_have_text("Settings")
    expect(page.locator("#generalSettingsFeedback")).to_have_text("Saved for new sessions only")
    expect(page.locator("#shellRecoveryFeedback")).to_contain_text("Review command side effects")
    page.keyboard.press("ArrowLeft")
    expect(chinese).to_be_focused()

    # Both locales and each pane fit narrow and desktop windows.
    for width in (1100, 390):
        page.set_viewport_size({"width": width, "height": 844})
        for locale in ("zh-CN", "en-US"):
            page.evaluate("locale => FocusI18n.setLocale(locale)", locale)
            for pane in ("general", "models", "data"):
                dialog.locator(f'[data-settings-tab="{pane}"]').click()
                assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth + 1"), (width, locale, pane)
                assert dialog.evaluate("el => el.getBoundingClientRect().right <= innerWidth + 1"), (width, locale, pane)
                if pane == "general" or (width == 390 and pane == "data"):
                    page.screenshot(path=str(output / f"settings-{width}-{locale}-{pane}.png"), full_page=True)
    # The shared navigation also preserves the existing inspector keyboard path.
    dialog.locator('[data-action="close-settings"]').first.click()
    page.evaluate("() => { state.view = 'focus'; openInspector('context'); }")
    inspector_tabs = page.locator('#appInspector [role="tab"]')
    inspector_tabs.first.focus()
    for key, selected in (("ArrowRight", "materials"), ("End", "run"), ("ArrowRight", "context"), ("ArrowLeft", "run"), ("Home", "context")):
        page.keyboard.press(key)
        current = page.locator(f'#appInspector [data-inspector-tab="{selected}"]')
        expect(current).to_be_focused()
        expect(current).to_have_attribute("aria-selected", "true")
    assert not errors, errors
    browser.close()
print(f"Settings browser checks passed; screenshots: {output}")
