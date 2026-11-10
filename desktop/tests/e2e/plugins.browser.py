"""Plugin queries, loading, activation policies and responsive layout.

Uses production scripts/handlers with a fixture API and no bootstrap/backend.
Run: python desktop/tests/e2e/plugins.browser.py [screenshot directory]
"""
from pathlib import Path
import re
import sys
import tempfile

from playwright.sync_api import sync_playwright, expect

renderer = Path(__file__).resolve().parents[2] / "src/renderer"
output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="focus-plugins-ui-"))
output.mkdir(parents=True, exist_ok=True)
source = (renderer / "index.html").read_text(encoding="utf-8")
html = re.sub(r'<script\b[^>]*>[\s\S]*?</script>', '', source)
html = re.sub(r'<meta http-equiv="Content-Security-Policy"[^>]*>', '', html)
html = html.replace('<head>', f'<head><base href="{(renderer / "index.html").as_uri()}">')

with tempfile.TemporaryDirectory(prefix="focus-plugins-fixture-") as temp, sync_playwright() as p:
    fixture = Path(temp) / "plugins.html"
    fixture.write_text(html, encoding="utf-8")
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(fixture.as_uri())
    page.evaluate("localStorage.clear()")
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
      window.queryFails = false; window.reloadFails = false;
      window.registry = {
        plugins: [
          { name: 'demo', version: '1', status: 'active', injected: ['tool', 'hook.after_tool'], requires: [] },
          { name: 'vision', version: '1', status: 'unavailable', injected: [], requires: ['service.vision'], missing: ['service.vision'] },
          { name: 'conflict<script>', version: '1', status: 'rejected', injected: [], requires: [],
            conflict: { interface: 'service.vision', current: 'vision', new: 'conflict<script>' } }
        ],
        interfaces: { tool: { plugins: ['demo'] }, 'hook.after_tool': { plugins: ['demo'], read_only: true } },
        activation: { registry: 'immediate', tools_and_hooks: 'next_run', running_runs: 'unchanged',
          http_routes: 'process_restart', desktop_assets: 'process_restart' }
      };
      state.details.set('running-task', { active_run: { run_id: 'running', tool_snapshot: ['old-tool'] } });
      api = async (path, options = {}) => {
        calls.push({ path, method: options.method || 'GET' });
        if (options.method === 'POST') {
          if (reloadFails) throw new Error('load response lost');
          registry.plugins[0].version = '2';
          return structuredClone(registry);
        }
        if (path.endsWith('/plugins') && window.queryGate) await queryGate;
        if (queryFails) throw new Error('query unavailable');
        if (path.endsWith('/traces')) return { traces: [
          { interface: 'tool', plugin: 'demo', status: 'failed', duration_ms: 4, error: '<script>tool failed</script>' }
        ] };
        return structuredClone(registry);
      };
    }""")
    page.locator('[data-action="show-plugins"]').click()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#pluginQueryStatus")).to_have_text("状态已刷新")
    refresh = page.get_by_role("button", name="刷新状态", exact=True)
    reload = page.get_by_role("button", name="加载插件变更", exact=True)
    timing = page.get_by_role("table", name="变更生效时机", exact=True)
    expect(timing.locator('[data-plugin-activation="tools_and_hooks"]')).to_contain_text("下次 Run 装配时生效")
    expect(timing).to_contain_text("正在运行的 Run 保留原快照")
    for key in ("desktop_assets", "http_routes"):
        expect(timing.locator(f'[data-plugin-activation="{key}"]')).to_contain_text("重启后端进程后生效")
    expect(timing).to_contain_text("新增、变更、禁用或卸载")
    expect(timing).to_contain_text("退出并重新启动 Focus")

    # Keyboard query remains a GET, suppresses duplicates and preserves focus.
    before = page.evaluate("calls.length")
    page.evaluate("() => { window.queryGate = new Promise(resolve => window.releaseQuery = resolve); }")
    refresh.focus()
    page.keyboard.press("Enter")
    expect(refresh).to_have_attribute("aria-busy", "true")
    expect(reload).to_have_attribute("aria-disabled", "true")
    page.keyboard.press("Enter")
    assert page.evaluate("calls.length") == before + 2
    page.evaluate("releaseQuery(); queryGate = null")
    expect(refresh).to_have_attribute("aria-busy", "false")
    expect(refresh).to_be_focused()
    assert page.evaluate("calls.every(c => c.method === 'GET')")

    # Confirmed registry changes survive a failed follow-up read.
    page.evaluate("queryFails = true")
    reload.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#pluginReloadStatus")).to_have_text("已加载到注册表")
    expect(page.locator("#app [role=alert]")).to_contain_text("刷新状态失败：query unavailable")
    expect(reload).to_be_focused()
    expect(page.locator('[data-plugin-name="demo"]')).to_contain_text("v2")
    assert page.evaluate("state.details.get('running-task').active_run.tool_snapshot.join()") == "old-tool"
    assert page.evaluate("calls.filter(c => c.method === 'POST').length") == 1
    page.evaluate("queryFails = false")
    refresh.click()
    expect(page.locator("#app [role=alert]")).to_have_count(0)
    assert page.evaluate("calls.filter(c => c.method === 'POST').length") == 1

    # Detail content retains conflicts and traces without repeating registration headings.
    page.locator('[data-plugin-name="demo"]').click()
    detail = page.locator(".plugin-detail")
    expect(detail.locator("h2, .plugin-detail-heading")).to_have_count(0)
    assert "已注册" not in detail.inner_text()
    expect(detail.locator(".plugin-interface-timing").first).to_have_text("下次 Run 装配时生效")
    expect(detail).to_contain_text("<script>tool failed</script>")
    assert detail.locator("script").count() == 0
    page.locator('[data-plugin-name="conflict<script>"]').click()
    expect(detail).to_contain_text("接口注入冲突")
    expect(detail).to_contain_text("service.vision")
    page.locator('[data-plugin-name="vision"]').click()
    expect(detail).to_contain_text("缺少依赖")
    expect(detail).to_contain_text("service.vision")

    page.evaluate("reloadFails = true")
    reload.click()
    expect(page.locator("#app [role=alert]")).to_contain_text("加载结果未确认：load response lost")
    assert page.evaluate("state.plugins.reloadSucceeded") is False
    assert page.evaluate("state.plugins.plugins[0].version") == "2"
    refresh.click()
    expect(page.locator("#pluginQueryStatus")).to_have_text("状态已刷新")
    assert page.evaluate("calls.filter(c => c.method === 'POST').length") == 2
    page.evaluate("reloadFails = false")
    reload.click()
    expect(page.locator("#app [role=alert]")).to_have_count(0)

    # Unknown policy stays unknown even for an active registration.
    page.evaluate("window.savedActivation = registry.activation; registry.activation = {}")
    refresh.click()
    expect(timing).to_contain_text("生效时机未知")
    assert "下次 Run 装配时生效" not in timing.inner_text()
    page.evaluate("registry.activation = savedActivation")
    refresh.click()
    expect(timing).to_contain_text("下次 Run 装配时生效")
    page.locator('[data-plugin-name="demo"]').click()

    for width in (1440, 900, 390):
        page.set_viewport_size({"width": width, "height": 1000})
        for locale in ("zh-CN", "en-US"):
            page.evaluate("locale => FocusI18n.setLocale(locale)", locale)
            for layout in ("cards", "list"):
                page.locator(f'[data-library="plugins"][data-layout="{layout}"]').click()
                view = page.locator(".plugins-view")
                assert view.evaluate("el => el.scrollWidth <= el.clientWidth + 1"), (width, locale, layout)
                assert page.locator("#app").evaluate("el => el.scrollWidth <= el.clientWidth + 1"), (width, locale, layout)
                assert page.locator(".plugin-filter").evaluate("""group => {
                  const bounds = group.getBoundingClientRect();
                  return [...group.querySelectorAll('button')].every(button => {
                    const rect = button.getBoundingClientRect();
                    return rect.left >= bounds.left && rect.right <= bounds.right + 1
                      && rect.top >= bounds.top && rect.bottom <= bounds.bottom + 1;
                  });
                }"""), ("Clipped filter", width, locale, layout)
                if locale == "en-US":
                    assert not re.search(r'[\u4e00-\u9fff]', view.inner_text()), view.inner_text()
                if layout == "cards":
                    page.screenshot(path=str(output / f"plugins-{width}-{locale}.png"), full_page=True)
    assert not errors, errors
    browser.close()
print(f"Plugin browser checks passed; screenshots: {output}")
