"""本文件提供执行详情因果链的真实浏览器回归。

输入为真实桌面页面及确定性只读 API；输出为详情、工作树证据、刷新失败与重试断言。
具体工作流为复用现有桌面夹具，打开运行详情并检查宽窄布局。示例：python desktop/tests/e2e/directive-causality.browser.py。
"""
import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

DESKTOP = Path(__file__).resolve().parents[2] / "src/renderer"
CHAIN = {
    "directive": {"directive_id": "directive-1", "content": "检查 result.txt", "origin": "patrol", "state": "settled", "correlation_id": "correlation-1"},
    "decision": {"rationale": "测试失败，需要检查结果"},
    "transitions": [{"state": "authorized", "occurred_at": "2026-10-18T01:00:00Z"}, {"state": "settled", "run_id": "run-1", "reason": "运行完成"}],
    "runs": [{"run_id": "run-1", "status": "success"}],
    "workspace": [{"run_id": "run-1", "slot_id": "slot-1", "root_path": "C:/workspace/isolated-worktree", "kind": "isolated", "observed_workspace_revision": 1, "resulting_workspace_revision": 2, "adoption_state": "pending", "effect_evidence": [{"path": "result.txt"}]}],
    "events": [{"sequence": 5, "kind": "context.workspace.changed", "payload": {"path": "result.txt"}}],
}
HARNESS = """
(() => {
  const baseFetch = window.fetch;
  window.causalityTest = { fail: false, count: 0 };
  window.fetch = async (url, options) => {
    if (String(url).endsWith('/runs/run-1/causality')) {
      causalityTest.count++;
      return new Response(JSON.stringify(causalityTest.fail ? {detail:'测试读取失败'} : {run_id:'run-1',causality:CHAIN}), {status:causalityTest.fail ? 503 : 200});
    }
    return baseFetch(url, options);
  };
})();
""".replace("CHAIN", json.dumps(CHAIN, ensure_ascii=False))

with sync_playwright() as playwright:
    executable = os.environ.get("FOCUS_BROWSER_EXECUTABLE")
    browser = playwright.chromium.launch(headless=True, **({"executable_path": executable} if executable else {}))
    try:
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script((DESKTOP / "../../tests/fixtures/context-ui-test-preload.cjs").read_text(encoding="utf-8") + HARNESS)
        page.goto((DESKTOP / "index.html").as_uri())
        page.wait_for_load_state("networkidle")
        page.locator('[data-action="focus-home"]').click()
        page.evaluate("activeTask().latest_run = {run_id:'summary-run',status:'success'}; state.details.get(state.activeTaskId).latest_main_run = {run_id:'run-1',status:'success'}")
        page.locator('.composer-context [data-inspector-tab="run"]').click()
        panel = page.locator(".run-causality")
        expect(panel.get_by_text("测试失败，需要检查结果", exact=True)).to_be_visible()
        expect(panel.get_by_text("C:/workspace/isolated-worktree", exact=True)).to_be_visible()
        panel.get_by_text("变更证据", exact=True).click()
        expect(panel.locator("pre").first).to_contain_text("result.txt")
        panel.get_by_text("关联事件（1）", exact=True).click()
        expect(panel).to_contain_text("context.workspace.changed")
        screenshot = os.environ.get("FOCUS_CAUSALITY_SCREENSHOT")
        if screenshot:
            page.screenshot(path=screenshot, full_page=True)
        page.set_viewport_size({"width": 800, "height": 900})
        assert panel.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        page.evaluate("causalityTest.fail = true")
        panel.get_by_role("button", name="刷新", exact=True).click()
        expect(panel.get_by_role("alert")).to_contain_text("测试读取失败")
        page.evaluate("causalityTest.fail = false")
        panel.get_by_role("button", name="重试", exact=True).click()
        expect(panel.get_by_text("测试失败，需要检查结果", exact=True)).to_be_visible()
        assert page.evaluate("causalityTest.count") == 3
        assert not errors, errors
        print("directive causality browser passed")
    finally:
        browser.close()
