"""Context message editing regression in real Chromium, using deterministic APIs.

Run: python desktop/tests/e2e/context-message-edit.browser.py
Optional FOCUS_CONTEXT_QA_DIR saves screenshots; no model or database calls.
"""
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

DESKTOP = Path(__file__).resolve().parents[2] / "src/renderer"
MESSAGES = [
    {"role": "tool", "content": "工具查询结果", "tool_call_id": "call-source", "name": "search",
     "future": {"nested": [None, False, {"exact": 3}]}},
    {"role": "ai", "content": [
        {"type": "text", "text": "图片说明", "annotations": [{"unknown": True}]},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,preserve", "detail": "high"}},
        {"type": "output_text", "text": "处理结果", "future": [0, None]},
        {"type": "input_audio", "input_audio": {"data": "exact-audio", "format": "wav"}},
        {"type": "future_type", "text": "preserve unknown block", "data": {"exact": True}},
    ], "tool_calls": [{"id": "call-1", "function": {"name": "search", "arguments": '{"q":"exact"}'}}],
     "additional_kwargs": {"unknown": [None, 0, False]}},
    {"role": "ai", "content": None, "tool_calls": [{"id": "call-2", "type": "function",
     "function": {"name": "search", "arguments": "{}"}}], "future": {"preserve": True}},
]
HARNESS = r"""
(() => {
  const base = window.fetch;
  window.contextEditHarness = { submissions: [] };
  window.fetch = async (input, options = {}) => {
    const path = new URL(String(input), 'http://focus.test').pathname;
    if (/\/contexts\/[^/]+\/snapshot$/.test(path)) return new Response(JSON.stringify({checkpoint_id:'checkpoint', messages:MESSAGES}));
    if (path === '/desktop/api/contexts/derive') {
      const body = JSON.parse(options.body);
      contextEditHarness.submissions.push(body);
      return new Response(JSON.stringify({context_id:'qa', projection_status:'rejected', editable:true, issues:[]}));
    }
    return base(input, options);
  };
})();
""".replace("MESSAGES", json.dumps(MESSAGES, ensure_ascii=False))


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script((DESKTOP / "../../tests/fixtures/context-ui-test-preload.cjs").read_text(encoding="utf-8") + HARNESS)
        page.goto((DESKTOP / "index.html").as_uri())
        page.wait_for_load_state("networkidle")
        page.wait_for_function("() => state.tasks.length > 0")
        page.evaluate("openContextEditor('root')")
        expect(page.locator(".context-source-message")).to_have_count(3)
        for index in range(3):
            page.locator(f'[data-context-source-index="{index}"] [data-action="context-source-copy"]').click()
        rows = page.locator(".context-message-editor")
        expect(rows).to_have_count(3)
        expect(rows.locator("[data-context-message-json]")).to_have_count(0)
        expect(rows.first.locator("header")).not_to_contain_text("call-source")
        expect(rows.first.locator(".context-source-json")).not_to_have_attribute("open", "")

        first_key = rows.first.get_attribute("data-context-ui-key")
        first = page.locator(f'[data-context-ui-key="{first_key}"]')
        first.get_by_role("button", name="编辑正文").click()
        expect(first.locator("textarea")).to_be_focused()
        first.locator("textarea").fill("未失焦修改\n<script>正文仍是文本</script>")
        first.get_by_role("button", name="创建副本").click()
        expect(rows).to_have_count(4)
        expected_copy = dict(MESSAGES[0], content="未失焦修改\n<script>正文仍是文本</script>")
        assert page.evaluate("state.contextDraft.messages.slice(0, 2)") == [expected_copy, expected_copy]
        assert page.evaluate("state.contextDraft.sourceSnapshots[0].messages") == MESSAGES
        page.evaluate("state.contextDraft.messages[1].future.nested[2].exact = 4")
        assert page.evaluate("state.contextDraft.messages[0].future.nested[2].exact") == 3

        mixed_key = rows.nth(2).get_attribute("data-context-ui-key")
        mixed = page.locator(f'[data-context-ui-key="{mixed_key}"]')
        mixed.get_by_role("button", name="编辑正文").click()
        expect(mixed.locator("[data-context-message-content]")).to_have_count(2)
        mixed.locator("textarea").nth(0).fill("新的图片说明")
        mixed.locator("textarea").nth(1).fill("新的处理结果")
        mixed.locator(".context-source-json > summary").click()
        mixed.get_by_role("button", name="编辑完整 JSON").click()
        expect(mixed.locator("[data-context-message-json]")).to_be_focused()
        expected_mixed = json.loads(json.dumps(MESSAGES[1]))
        expected_mixed["content"][0]["text"] = "新的图片说明"
        expected_mixed["content"][2]["text"] = "新的处理结果"
        assert json.loads(mixed.locator("textarea").input_value()) == expected_mixed
        mixed.locator("textarea").fill("invalid JSON")
        mixed.get_by_role("button", name="编辑正文").click()
        expect(mixed.locator("[data-context-message-json]")).to_have_value("invalid JSON")
        mixed.get_by_role("button", name="创建副本").click()
        expect(rows).to_have_count(4)
        expect(page.locator("#globalStatus")).to_contain_text("不是合法 JSON")
        expected_mixed["additional_kwargs"]["new_extension"] = {"keep": True}
        mixed.locator("textarea").fill(json.dumps(expected_mixed, ensure_ascii=False))
        mixed.get_by_role("button", name="编辑正文").click()
        expect(mixed.locator("[data-context-message-json]")).to_have_count(0)
        expect(mixed.locator("textarea").first).to_have_value("新的图片说明")
        expect(mixed.locator("textarea").first).to_be_focused()
        mixed.locator("[data-context-pointer-handle]").press("Alt+ArrowUp")
        expect(rows.nth(1)).to_have_attribute("data-context-ui-key", mixed_key)
        expect(mixed.locator("textarea").first).to_have_attribute("aria-label", "消息 2 正文 1")
        mixed.get_by_role("button", name="删除", exact=True).click()
        expect(page.locator(f'[data-context-ui-key="{mixed_key}"]')).to_have_count(0)
        page.get_by_role("button", name="撤销", exact=True).click()
        expect(rows.nth(1)).to_have_attribute("data-context-ui-key", mixed_key)
        assert page.evaluate("state.contextDraft.messages[1]") == expected_mixed

        # Tool-only messages remain intact and keep the full JSON escape hatch.
        tool_only = rows.last
        expect(tool_only.locator('[data-action="context-message-toggle"]')).to_have_count(0)
        tool_only.locator(".context-source-json > summary").click()
        tool_only.get_by_role("button", name="编辑完整 JSON").click()
        assert json.loads(tool_only.locator("textarea").input_value()) == MESSAGES[2]
        page.get_by_role("button", name="新增消息", exact=True).click()
        expect(rows.locator("textarea")).to_have_count(1)
        expect(rows.last.locator("textarea")).to_be_focused()
        expect(rows.last.locator("textarea")).to_have_attribute("aria-label", "消息 5 正文")
        rows.last.locator("textarea").fill("新增正文")
        expected = page.evaluate("syncContextDraft().messages")
        page.get_by_role("button", name="创建 Context", exact=True).click()
        page.wait_for_function("() => contextEditHarness.submissions.length === 1")
        expect(page.locator(".context-decision")).to_be_visible()
        assert page.evaluate("contextEditHarness.submissions[0].messages") == expected
        assert expected[1] == expected_mixed
        assert expected[3] == MESSAGES[2]

        # Review both locales and narrow desktop layouts, including focus restoration.
        page.evaluate("state.contextDraft.context = null; setStatus(''); closeInspector(); renderContextEditor()")
        for locale in ["zh-CN", "en-US"]:
            page.set_viewport_size({"width": 1280, "height": 900})
            page.evaluate("locale => { syncContextDraft(); state.contextDraft.expandedKey = null; FocusI18n.setLocale(locale); renderContextEditor(); }", locale)
            mixed = page.locator(f'[data-context-ui-key="{mixed_key}"]')
            mixed.locator('[data-action="context-message-toggle"]').click()
            mixed.locator('[data-action="context-message-toggle"]').click()
            expect(mixed.locator('[data-action="context-message-toggle"]')).to_be_focused()
            mixed.locator(".context-source-json > summary").click()
            mixed.locator('[data-action="context-message-edit-json"]').click()
            mixed.locator('[data-action="context-message-edit-json"]').click()
            expect(mixed.locator('[data-action="context-message-edit-json"]')).to_be_focused()
            mixed.locator('[data-action="context-message-toggle"]').click()
            for width in [1280, 720]:
                page.set_viewport_size({"width": width, "height": 900})
                page.evaluate("document.querySelector('.context-definition-panel').scrollTop = 0; document.querySelector('.context-source-message-list').scrollTop = 0; document.querySelector('#app').scrollTop = 0")
                if width == 720:
                    mixed.scroll_into_view_if_needed()
                box = mixed.bounding_box()
                assert box["x"] >= 0 and box["x"] + box["width"] <= width
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                if destination := os.environ.get("FOCUS_CONTEXT_QA_DIR"):
                    Path(destination).mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(Path(destination) / f"context-edit-{locale}-{width}.png"))
        assert not errors, errors
        print("Context message editing browser checks passed (body, JSON, duplicate, protocol round trip, undo, submit, locales, responsive).")
    finally:
        browser.close()
