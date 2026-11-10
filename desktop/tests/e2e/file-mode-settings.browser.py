"""Render the production settings markup and mode reader with controlled workspace responses.

Run: python desktop/tests/e2e/file-mode-settings.browser.py [screenshot-directory]
"""
from pathlib import Path
import sys
import tempfile

from playwright.sync_api import sync_playwright

renderer = Path(__file__).resolve().parents[2] / "src" / "renderer"
output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="focus-file-modes-"))
output.mkdir(parents=True, exist_ok=True)
html = (renderer / "index.html").read_text(encoding="utf-8")
dialog = html[html.index('<dialog id="settingsDialog"'):]
dialog = dialog[:dialog.index("</dialog>") + len("</dialog>")]
app = (renderer / "app/app.js").read_text(encoding="utf-8")
reader = app[app.index("let fileModeSettingsRead = 0;"):app.index("async function openSettings()")]
change = app[app.index('  if (event.target.id === "newSessionAccessMode") {'):]
change = change[:change.index('  if (event.target.id === "taskNotificationsEnabled") {')]

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1100, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("http://focus.test/", lambda route: route.fulfill(content_type="text/html", body=f'<!doctype html><html lang="zh-CN"><body>{dialog}</body></html>'))
    page.goto("http://focus.test/")
    for sheet in ("shared/styles/styles.css", "shared/styles/tokens.css", "shared/styles/base.css", "shared/styles/icons.css", "shared/styles/components.css"):
        page.add_style_tag(path=str(renderer / sheet))
    for script in ("shared/i18n/i18n.js", "features/conversation/access-mode.js", "features/patrol/workspace/workspace-patrol-inputs.js"):
        page.add_script_tag(path=str(renderer / script))
    page.add_script_tag(content="""
      const settingsDialog = document.querySelector('#settingsDialog');
      const accessMode = FocusAccessMode;
      const state = {patrolWorkspace:{workspace_id:'a',name:'设计工作区',path:'C:/workspace'}};
      let response = {loop_id:'loop',access_mode:'read-only'}, readError = false;
      const calls = [];
      const loopApi = {workspacePatrol: async id => { calls.push(id); if (readError) throw Error('offline'); return response; }};
      const uiText = (key, fallback) => { const text = FocusI18n.t(key); return text === key ? fallback : text; };
      const setStatus = message => { throw Error(message); };
    """ + reader + '\ndocument.addEventListener("change", event => {\n' + change + '\n});')
    page.evaluate("async () => { settingsDialog.showModal(); await loadFileModeSettings(); }")
    page.wait_for_load_state("networkidle")
    modes = page.locator(".settings-file-modes")
    ordinary = page.get_by_role("combobox", name="普通会话新建默认文件模式")
    assert ordinary.input_value() == "workspace-write"
    assert page.locator("#currentPatrolAccessMode").inner_text() == "只读执行"
    assert page.locator("#newPatrolAccessMode").inner_text() == "未应用文件沙箱"
    assert page.get_by_role("status", name="当前 Patrol 设计工作区").count() == 1
    ordinary.select_option("read-only")
    assert page.locator("#newSessionModeFeedback").inner_text() == "已保存普通会话新建默认值"
    assert page.evaluate("calls.length") == 1
    page.screenshot(path=str(output / "file-modes-desktop.png"), full_page=True)
    page.evaluate("async () => { response = null; await loadFileModeSettings(); }")
    assert page.locator("#currentPatrolAccessMode").inner_text() == "尚未创建"
    page.evaluate("async () => { response = {loop_id:'loop'}; await loadFileModeSettings(); }")
    assert page.locator("#currentPatrolAccessMode").inner_text() == "实际模式不可用"
    page.evaluate("async () => { readError = true; await loadFileModeSettings(); }")
    assert page.locator("#currentPatrolAccessMode").inner_text() == "读取失败，请刷新重试"
    page.screenshot(path=str(output / "file-modes-error.png"), full_page=True)
    page.evaluate("""async () => {
      FocusI18n.setLocale('en-US'); readError = false;
      response = {loop_id:'loop',access_mode:'workspace-write'};
      await loadFileModeSettings();
    }""")
    assert page.get_by_role("combobox", name="Default file mode for new ordinary sessions").input_value() == "read-only"
    assert page.locator("#currentPatrolAccessMode").inner_text() == "Workspace write"
    page.set_viewport_size({"width": 390, "height": 1000})
    modes.scroll_into_view_if_needed()
    assert page.locator("#settingsDialog").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    page.screenshot(path=str(output / "file-modes-narrow-en.png"), full_page=True)
    assert not errors, errors
    browser.close()
print(f"File mode browser checks passed; screenshots: {output}")
