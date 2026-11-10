"""Real renderer keyboard/range/focus regressions, using local API fixtures.

Run: python desktop/tests/e2e/accessible-controls.browser.py [screenshot-directory]
This checks exposed semantics and DOM mutations; spoken screen-reader output needs a manual check.
"""
import mimetypes
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2] / "src/renderer"
CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
OUTPUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="focus-accessibility-"))
OUTPUT.mkdir(parents=True, exist_ok=True)


def serve(route):
    target = (ROOT / urlparse(route.request.url).path.removeprefix("/desktop/")).resolve()
    if not target.is_relative_to(ROOT) or not target.is_file():
        route.fulfill(status=404, body="Not found")
        return
    body = target.read_bytes()
    if target.name == "app.js":
        body = re.sub(rb"runUiAction\(async \(\) => \{ await bootstrap\(\); if \(desktopPatrol\) await desktopPatrol.ready\(\); \}\);\s*$", b"", body)
    route.fulfill(body=body, content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream")


def value(handle):
    return float(handle.get_attribute("aria-valuenow"))


def check_range(handle):
    assert handle.get_attribute("aria-label")
    for control in handle.get_attribute("aria-controls").split():
        assert handle.page.locator(f"#{control}").count() == 1
    assert float(handle.get_attribute("aria-valuemin")) <= value(handle) <= float(handle.get_attribute("aria-valuemax"))


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, **({"executable_path": str(CHROME)} if CHROME.exists() else {}))
    try:
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
        page.route("http://localhost:7229/**", serve)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://localhost:7229/desktop/index.html", wait_until="networkidle")
        page.evaluate("state.view = 'focus'; render();")
        assert page.locator("#app").get_attribute("aria-live") is None
        assert page.locator("#overlayRoot").get_attribute("aria-live") is None
        assert page.locator(".shell-task-context").get_attribute("aria-live") is None
        nav = page.locator(".shell-resizer-nav")
        check_range(nav)
        assert abs(value(nav) - page.locator("#appNavigation").bounding_box()["width"]) < 1
        start = value(nav)
        nav.focus()
        nav.press("ArrowLeft")
        assert value(nav) == round(start - 24)
        nav.press("End")
        assert value(nav) == float(nav.get_attribute("aria-valuemax"))
        nav.press("Home")
        assert value(nav) == float(nav.get_attribute("aria-valuemin"))
        nav.press("Shift+ArrowRight")
        assert value(nav) == float(nav.get_attribute("aria-valuemin")) + 64
        saved = page.evaluate("state.shellLayout.navWidth")
        page.reload(wait_until="networkidle")
        page.evaluate("state.view = 'focus'; render();")
        assert value(nav) == saved
        nav.press("End")
        page.evaluate("openInspector('context', document.querySelector('.app-mark'))")
        inspector = page.locator(".shell-resizer-inspector")
        expect(inspector).to_be_visible()
        # Opening the inspector changes the rendered width without resizing the window.
        check_range(inspector)
        assert abs(value(inspector) - page.locator("#appInspector").bounding_box()["width"]) < 1
        inspector.focus()
        start = page.locator("#appInspector").bounding_box()["width"]
        inspector.press("ArrowLeft")
        assert abs(value(inspector) - start - 24) < 1
        inspector.press("Home")
        assert value(inspector) == 240
        inspector.press("End")
        assert value(inspector) == 480
        tab = page.locator("#inspectorTab-context")
        tab.focus()
        tab.press("End")
        expect(page.locator("#inspectorTab-run")).to_be_focused()
        expect(page.locator("#inspectorTab-run")).to_have_attribute("aria-selected", "true")
        expect(page.locator("#inspectorContent")).to_have_attribute("aria-labelledby", "inspectorTab-run")
        page.locator("#inspectorTab-run").press("ArrowRight")
        expect(tab).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.locator(".app-mark")).to_be_focused()
        expect(inspector).to_have_attribute("tabindex", "-1")
        page.evaluate("openInspector('context', document.querySelector('.app-mark'))")
        page.set_viewport_size({"width": 1000, "height": 900})
        expect(inspector).to_have_attribute("tabindex", "-1")
        page.set_viewport_size({"width": 1440, "height": 1000})
        expect(inspector).to_have_attribute("tabindex", "0")
        page.evaluate("loadStorageSettings = async () => {}; loadModelSettings = async () => {}; selectSettingsTab('general'); settingsDialog.showModal();")
        page.locator("#settingsTab-general").focus()
        page.keyboard.press("ArrowRight")
        expect(page.locator("#settingsTab-models")).to_be_focused()
        expect(page.locator("#settingsPane-models")).to_be_visible()
        page.keyboard.press("End")
        expect(page.locator("#settingsTab-data")).to_be_focused()
        expect(page.locator("#settingsPane-data")).to_be_visible()
        page.keyboard.press("ArrowRight")
        expect(page.locator("#settingsTab-general")).to_be_focused()
        page.evaluate("settingsDialog.close(); closeInspector();")
        # Real file-panel markup and renderer binding, with no backend/model work.
        page.evaluate("""() => {
          const task = { task_id:'a', thread_id:'ta', workspace_id:'ws', title:'Keyboard check', harness_mode:'workspace' };
          state.tasks = [task]; state.activeTaskId = 'a'; state.contextTrees.set('ws', []);
          state.details.set('a', { messages:[], ui_state:{} }); state.filesPanel = true;
          renderFilesPanel = () => {}; renderFocus(task);
        }""")
        panel = page.locator("#panelResizer")
        check_range(panel)
        panel.focus()
        start = value(panel)
        panel.press("ArrowLeft")
        assert value(panel) == start + 24
        assert page.evaluate("Number(localStorage.getItem('focus-panel-width'))") == value(panel)
        # Both memory splits retain state through a real memory view rebuild.
        page.evaluate("state.view = 'memory'; state.filesPanel = false; state.memory.composing = true; render();")
        for selector, key, ratio in [("[data-memory-resizer-vertical]", "ArrowDown", "editorRatio"), ("[data-memory-resizer-horizontal]", "ArrowRight", "sourceRatio")]:
            handle = page.locator(selector)
            check_range(handle)
            handle.focus()
            handle.press(key)
            assert value(handle) == 52
            handle.press("End")
            saved = value(handle)
            page.evaluate("render()")
            assert value(page.locator(selector)) == saved
            assert round(page.evaluate(f"state.memory.{ratio} * 100")) == saved
        # No redundant live-region mutation for an identical status receipt.
        page.evaluate("""() => {
          window.statusWrites = 0;
          new MutationObserver(rows => statusWrites += rows.length).observe(document.querySelector('#globalStatus'), { childList:true });
          setStatus('已更新'); setStatus('已更新');
        }""")
        assert page.evaluate("statusWrites") == 1
        page.emulate_media(forced_colors="active")
        handle = page.locator("[data-memory-resizer-vertical]")
        handle.focus()
        assert handle.evaluate("el => getComputedStyle(el).outlineStyle") == "solid"
        assert handle.evaluate("el => getComputedStyle(el).outlineWidth") == "2px"
        page.screenshot(path=str(OUTPUT / "memory-keyboard-contrast.png"), full_page=True)
        page.emulate_media(forced_colors="none")
        page.evaluate("state.view = 'focus'; state.memory.composing = false; render(); openInspector('context', document.querySelector('.app-mark'));")
        inspector.focus()
        page.screenshot(path=str(OUTPUT / "shell-keyboard.png"), full_page=True)
        page.evaluate("""async () => {
          state.view = 'patrol'; renderShellChrome();
          const inputs = FocusWorkspacePatrolInputs.create();
          const live = { projection:null, connection:{status:'live'} };
          window.patrolLive = live;
          window.testPatrol = FocusWorkspacePatrolController.create({
            api:{ workspacePatrol:async () => ({loop_id:'a'}), workspaceInputs:async () => ({items:[]}) },
            inputs, view:FocusWorkspacePatrolView,
            intake:{ reconcile:async () => {}, history:(id, page, ...args) => inputs.history(page, ...args) },
            liveStore:{ get:() => live, subscribe:fn => { window.emitPatrol = () => fn(live); return () => {}; } },
            connection:{ start:async () => {}, stop() {} }
          });
          await testPatrol.mount(app, {workspace_id:'ws', display_name:'Keyboard check'});
          testPatrol.showDetails();
        }""")
        patrol = page.locator("[data-patrol-panel-resizer]")
        expect(patrol).to_be_visible()
        page.wait_for_function("Math.abs(Number(document.querySelector('[data-patrol-panel-resizer]').getAttribute('aria-valuenow')) - document.querySelector('#patrolProgressPanel').getBoundingClientRect().height) < 1")
        check_range(patrol)
        patrol.focus()
        start = value(patrol)
        patrol.press("ArrowUp")
        assert value(patrol) < start
        patrol.press("Home")
        assert value(patrol) == float(patrol.get_attribute("aria-valuemin"))
        patrol.press("End")
        assert value(patrol) == float(patrol.get_attribute("aria-valuemax"))
        saved = page.evaluate("localStorage.getItem('focus-patrol-panel-split-v1')")
        assert 0 < float(saved) < 1
        page.locator("[data-patrol-panel-resizer]").dblclick()
        assert page.evaluate("localStorage.getItem('focus-patrol-panel-split-v1')") == "0.6"
        # Cancelled pointer drags roll back the preference and rendered value.
        box = patrol.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 30)
        patrol.dispatch_event("pointercancel", {"pointerId": 1})
        page.mouse.up()
        assert page.evaluate("localStorage.getItem('focus-patrol-panel-split-v1')") == "0.6"
        page.locator(".patrol-page-heading [data-patrol-details]").click()
        page.evaluate("""() => {
          const status = document.querySelector('[data-patrol-pending-status]');
          window.pendingWrites = 0;
          new MutationObserver(rows => pendingWrites += rows.length).observe(status, {childList:true});
          const projection = {loop_id:'a',loop:{state:{status:'running'}},wait_requests:{
            q:{entity_id:'q',revision:1,state:{status:'open',prompt:'Confirm scope',scope:{information_only:true}}}
          },interventions:{},facts:{},contexts:{}};
          patrolLive.projection = projection; emitPatrol(); emitPatrol();
        }""")
        expect(page.locator("[data-patrol-pending-status]")).to_have_text("1 项待处理")
        assert page.evaluate("pendingWrites") == 1
        page.evaluate("patrolLive.projection = {...patrolLive.projection,wait_requests:{}}; emitPatrol();")
        expect(page.locator("[data-patrol-pending-status]")).to_have_text("0 项待处理")
        page.evaluate("""() => {
          testPatrol.dispose();
          const wb = {sourceRatio:42, doc:{value:{instructions:'',entries:[]}},
            rawEditor:{element:document.createElement('textarea')}, queue:{flush:async () => {}}};
          window.assembly = new FocusPatrolAssembly.PatrolAssemblyView(wb, app);
        }""")
        divider = page.locator(".patrol-divider")
        check_range(divider)
        divider.focus()
        divider.press("End")
        assert value(divider) == float(divider.get_attribute("aria-valuemax"))
        divider.press("Home")
        assert value(divider) == float(divider.get_attribute("aria-valuemin"))
        page.set_viewport_size({"width": 650, "height": 900})
        expect(divider).not_to_be_visible()
        check_range(divider)
        page.set_viewport_size({"width": 1440, "height": 1000})
        expect(divider).to_be_visible()
        check_range(divider)
        page.evaluate("assembly.destroy()")
        assert not errors, errors
        print(f"PASS: renderer keyboard ranges, preferences, tabs, focus restoration, live-region mutations, reduced motion and forced colors. Screenshots: {OUTPUT}")
    finally:
        browser.close()
