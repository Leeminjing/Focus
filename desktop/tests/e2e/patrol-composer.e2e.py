"""Check the production composer, controller and native tools popover in Chromium.

Run: python desktop/tests/e2e/patrol-composer.e2e.py --output <directory>
API, Live and unrelated workbench services are offline fixtures; no Agent runs.
"""
import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2] / "src/renderer"
STYLES = ["shared/styles/tokens.css", "shared/styles/base.css", "shared/styles/icons.css", "shared/styles/components.css",
          "shared/styles/workspace-patrol.css", "features/files/artifact-drawer.css"]
SCRIPTS = ["features/patrol/workspace/workspace-patrol-feedback.js",
           "features/patrol/workspace/patrol-input-references.js",
           "features/patrol/workspace/workspace-patrol-view.js",
           "features/patrol/workspace/workspace-patrol-inputs.js",
           "features/loop/loop-facts-view.js", "features/files/artifact-drawer.js",
           "features/patrol/workspace/workspace-patrol-controller.js"]
FIXTURE = """async (remote = false) => {
  window.FocusLoopWaitRequestView = {createDraftStore: () => new Map()};
  window.FocusWorkspacePatrolMaterialPreview = {create: () => ({render() {}, dispose() {}})};
  window.FocusContextInspector = {create: () => ({mount() {}, close() {}, dispose() {}})};
  window.FocusSurfaceTransition = {
    visible: async (node, show) => {node.hidden = !show; node.inert = !show;},
    layout: (_node, change) => change(), finish() {}
  };
  const fact = {fact_id:'file', revision:1, file:{path:'report.md', slot_id:'slot', result_key:'snapshot', operation:'created'}};
  const api = {workspacePatrol: async () => ({loop_id:'loop'}),
    workspaceInputs: async () => ({items:[],next_before:null}), facts: async () => ({facts:[fact]})};
  const inputs = FocusWorkspacePatrolInputs.create(() => 'submission');
  window.sent = [];
  const intake = {history: (_id, page, ...args) => inputs.history(page, ...args), reconcile: async () => {},
    send: async (_id, row) => {sent.push(row); return {loop_id:'loop'};}};
  window.controller = FocusWorkspacePatrolController.create({api, inputs, intake,
    onCompose: remote ? async () => {window.remoteComposed = true;} : undefined,
    liveStore:{get: () => ({connection:{status:'live'}}), subscribe: () => () => {}},
    connection:{start: async () => {}, stop() {}}});
  await controller.mount(document.querySelector('#app'), {workspace_id:'workspace',display_name:'Focus'});
  window.savedInput = document.querySelector('[data-patrol-content]');
  window.savedForm = document.querySelector('[data-patrol-composer]');
}"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
        browser = p.chromium.launch(headless=True, **({"executable_path": str(chrome)} if chrome.exists() else {}))
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        def serve(route):
            relative = urlparse(route.request.url).path.lstrip("/")
            if not relative:
                styles = "".join(f'<link rel="stylesheet" href="/{file}">' for file in STYLES)
                route.fulfill(content_type="text/html", body=f'<!doctype html><html lang="zh-CN"><meta charset="UTF-8">{styles}<main id="app"></main></html>')
            else:
                file = (ROOT / relative).resolve()
                if file.is_relative_to(ROOT) and file.is_file():
                    route.fulfill(path=file)
                else:
                    route.fulfill(status=404)
        page.route("http://composer.test/**", serve)
        page.goto("http://composer.test/")
        page.wait_for_load_state("networkidle")
        page.add_style_tag(content="#app{height:100dvh;overflow:auto}body{background:var(--surface-canvas)}")
        for file in SCRIPTS:
            page.add_script_tag(path=str(ROOT / file))
        page.evaluate(FIXTURE)
        tools = page.locator("[data-patrol-composer-tools]")
        toggle = page.locator("[data-patrol-tools-toggle]")
        content = page.locator("[data-patrol-content]")
        content.fill("Keep this draft")

        for width, height in [(1440, 900), (1000, 720), (390, 844), (1280, 480)]:
            page.set_viewport_size({"width": width, "height": height})
            for detailed in [False, True]:
                page.evaluate("detailed => { if (detailed) controller.showDetails(); else if (!document.querySelector('.is-quiet')) document.querySelector('[data-patrol-composer] [data-patrol-details]').click(); }", detailed)
                expect(tools).to_be_hidden()
                expect(page.locator("[data-patrol-file-picker-open]")).to_be_visible()
                toggle.click()
                expect(tools).to_be_visible()
                for selector in ["[data-patrol-type]", "[data-patrol-files-open]", "[data-patrol-history-open]", "[data-patrol-composer] [data-patrol-details]"]:
                    expect(page.locator(selector)).to_be_visible()
                    assert page.locator(selector).evaluate("node => parseFloat(getComputedStyle(node).fontSize)") >= 13
                bounds = tools.bounding_box()
                assert bounds and bounds["x"] >= 0 and bounds["y"] >= 0, bounds
                assert bounds["x"] + bounds["width"] <= width and bounds["y"] + bounds["height"] <= height, bounds
                page.screenshot(path=str(args.output / f"composer-{width}-{'details' if detailed else 'quiet'}-tools.png"))
                page.keyboard.press("Escape")
                expect(tools).to_be_hidden()
                expect(toggle).to_be_focused()
                assert page.evaluate("savedInput === document.querySelector('[data-patrol-content]') && savedForm === document.querySelector('[data-patrol-composer]')")
                expect(content).to_have_value("Keep this draft")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(args.output / f"composer-{width}-{'details' if detailed else 'quiet'}.png"))
                if width == 1440 and detailed:
                    page.locator("[data-patrol-composer]").screenshot(path=str(args.output / "composer-primary.png"))

        toggle.click()
        content.click()
        expect(tools).to_be_hidden()
        toggle.focus()
        page.keyboard.press("Enter")
        expect(tools).to_be_visible()
        page.locator("[data-patrol-type]").select_option("outcome")
        page.locator("[data-patrol-history-open]").click()
        expect(tools).to_be_hidden()
        history = page.locator("[data-patrol-input-history]")
        expect(history).to_be_visible()
        history.focus()
        page.keyboard.press("Escape")
        expect(history).to_be_hidden()
        expect(toggle).to_be_focused()
        toggle.click()
        page.locator("[data-patrol-files-open]").click()
        expect(tools).to_be_hidden()
        expect(page.locator("[data-files-search]")).to_be_focused()
        page.locator("[data-files-close]").click()
        expect(toggle).to_be_focused()
        page.locator("[data-patrol-file-picker-open]").click()
        page.locator("[data-patrol-file-add]").click()
        expect(page.locator("[data-patrol-file-refs]")).to_contain_text("report.md")
        content.press("Control+Enter")
        page.wait_for_function("sent.length === 1")
        sent = page.evaluate("sent[0]")
        assert sent["content"] == "Keep this draft" and sent["input_type"] == "outcome", sent
        assert sent["file_refs"][0]["result_key"] == "snapshot", sent
        assert not errors, errors
        page.evaluate("controller.dispose()")
        page.evaluate(FIXTURE, True)
        expect(content).to_be_hidden()
        expect(page.locator("[data-patrol-file-picker-open]")).to_be_hidden()
        toggle.click()
        expect(page.locator("[data-patrol-type-field]")).to_be_hidden()
        page.keyboard.press("Escape")
        page.locator('[data-patrol-composer] button[type="submit"]').click()
        page.wait_for_function("window.remoteComposed === true")
        page.evaluate("controller.dispose();document.querySelector('#app').innerHTML = '<section class=\"workspace-patrol is-quiet\">' + FocusWorkspacePatrolView.composer(null) + '</section>'")
        expect(page.locator("[data-patrol-switch]")).to_be_visible()
        expect(page.locator("[data-patrol-state]")).to_be_visible()
        expect(page.locator('[data-patrol-composer] button[type="submit"]')).to_be_disabled()
        assert not errors, errors
        browser.close()
    print(json.dumps({"passed": True, "viewport_states": 8, "screenshots": str(args.output)}))


if __name__ == "__main__":
    main()
