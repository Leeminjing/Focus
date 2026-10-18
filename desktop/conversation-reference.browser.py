"""Real Chromium regression for ordinary conversation selection and quotation.

Uses the existing deterministic desktop API fixture; no model or database calls.
Run: python desktop/conversation-reference.browser.py
FOCUS_BROWSER_EXECUTABLE and FOCUS_REFERENCE_QA_DIR optionally override outputs.
"""
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright, expect


DESKTOP = Path(__file__).resolve().parent
SOURCE = "先看这里。\n\n这里需要引入 **Redis 分布式锁**。这里需要引入 Redis 分布式锁。\n\n```js\nconst lock = '🔒';\nawait lock.acquire();\n```"
HARNESS = r"""
(() => {
  const base = window.fetch;
  window.referenceHarness = { requests: [], fail: false };
  Object.defineProperty(navigator, 'clipboard', {value:{writeText: async text => { referenceHarness.copied = text; }}});
  window.fetch = async (url, options = {}) => {
    const path = new URL(String(url), 'http://focus.test').pathname;
    if (path.endsWith('/main/runs')) {
      referenceHarness.requests.push({body: JSON.parse(options.body), key: options.headers['Idempotency-Key']});
      return new Response(JSON.stringify(referenceHarness.fail ? {detail:'模拟网络失败'} :
        {intent_kind:'direct_message',intent_id:'accepted'}), {status:referenceHarness.fail ? 500 : 200});
    }
    const response = await base(url, options);
    if (/\/tasks\/[^/]+$/.test(path)) {
      const body = await response.json();
      body.messages = [{id:'source-answer',role:'ai',content:SOURCE},
        {id:'other-answer',role:'ai',content:'另一个回答，不能跨消息引用。'}];
      return new Response(JSON.stringify(body), {status:200});
    }
    return response;
  };
})();
""".replace("SOURCE", json.dumps(SOURCE, ensure_ascii=False))


def select(page, text, occurrence=0):
    """Select an exact rendered occurrence across Markdown text nodes."""
    return page.evaluate("""({text, occurrence}) => {
      const content = document.querySelector('#conversation [data-message-id="source-answer"] .message-content');
      const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
      const nodes = []; let node;
      while (node = walker.nextNode()) if (!node.parentElement.closest('.code-block-toolbar')) nodes.push(node);
      const source = nodes.map(node => node.textContent).join('');
      let start = -1;
      for (let i = 0; i <= occurrence; i++) start = source.indexOf(text, start + 1);
      if (start < 0) throw new Error('selection text missing');
      const end = start + text.length; let offset = 0; const range = document.createRange();
      let found = false;
      for (const node of nodes) {
        const next = offset + node.length;
        if (!found && start >= offset && start < next) { range.setStart(node, start - offset); found = true; }
        if (end > offset && end <= next) { range.setEnd(node, end - offset); break; }
        offset = next;
      }
      const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
      return {start,end,text};
    }""", {"text": text, "occurrence": occurrence})


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, **(
        {"executable_path": os.environ["FOCUS_BROWSER_EXECUTABLE"]} if os.environ.get("FOCUS_BROWSER_EXECUTABLE") else {}))
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script((DESKTOP / "context-ui-test-preload.cjs").read_text(encoding="utf-8") + HARNESS)
        page.goto((DESKTOP / "index.html").as_uri())
        page.wait_for_load_state("networkidle")
        page.locator('[data-action="focus-home"]').click()
        expect(page.locator("#mainInput")).to_be_visible()
        page.locator("#mainInput").fill("为什么单机部署也需要这个？")
        selected = select(page, "Redis 分布式锁", 1)
        expect(page.locator(".conversation-selection-toolbar")).to_be_visible()
        page.locator('[data-reference-command="quote"]').click()
        expect(page.locator("#composerReference blockquote")).to_have_text(selected["text"])
        expect(page.locator("#mainInput")).to_have_value("为什么单机部署也需要这个？")
        page.locator('#composerReference [data-action="reference-source"]').click()
        expect(page.locator(".is-reference-source")).to_be_visible()
        assert page.evaluate("getSelection().toString()") == selected["text"]
        page.locator('#composerReference [data-action="reference-remove"]').click()
        expect(page.locator("#composerReference blockquote")).to_have_count(0)
        select(page, "Redis 分布式锁")
        page.locator('[data-reference-command="copy"]').click()
        page.wait_for_function('referenceHarness.copied === "Redis 分布式锁"')
        expect(page.locator("#composerReference blockquote")).to_have_count(0)

        # Cross-message selections and copy controls are not valid quote targets.
        page.evaluate("""() => {
          const contents = document.querySelectorAll('#conversation .message-content');
          const range = document.createRange(); range.setStart(contents[0], 0); range.setEnd(contents[1], 1);
          getSelection().removeAllRanges(); getSelection().addRange(range);
        }""")
        expect(page.locator(".conversation-selection-toolbar")).to_have_count(0)
        selected = select(page, "lock.acquire()")
        expect(page.locator(".conversation-selection-toolbar")).to_be_visible()
        page.locator('[data-reference-command="quote"]').click()
        saved = page.evaluate('mainReference(state.activeTaskId)')
        assert saved["start"] == selected["start"] and saved["end"] == selected["end"]
        assert saved["text"] == "lock.acquire()"
        assert saved["offset_basis"] == "rendered_text_utf16"
        # Source navigation widens a bounded history window when needed.
        page.evaluate("""() => {
          const detail = state.details.get(state.activeTaskId);
          detail.messages.push(...Array.from({length:100}, (_, i) => ({id:`later-${i}`,role:i%2?'ai':'human',content:`later ${i}`})));
          renderFocus();
        }""")
        expect(page.locator('#conversation [data-message-id="source-answer"]')).to_have_count(0)
        page.locator('#composerReference [data-action="reference-source"]').click()
        expect(page.locator('#conversation [data-message-id="source-answer"]')).to_be_visible()
        task_id = page.evaluate("state.activeTaskId")
        page.evaluate('switchTask("sibling")')
        expect(page.locator("#composerReference blockquote")).to_have_count(0)
        page.evaluate("id => switchTask(id)", task_id)
        expect(page.locator("#composerReference blockquote")).to_have_text(saved["text"])
        page.reload()
        page.wait_for_load_state("networkidle")
        page.locator('[data-action="focus-home"]').click()
        page.evaluate("id => switchTask(id)", task_id)
        expect(page.locator("#composerReference blockquote")).to_have_text(saved["text"])
        page.set_viewport_size({"width": 900, "height": 760})
        page.locator('[data-action="close-inspector"]').click()
        expect(page.locator("#appInspector")).to_be_hidden()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        output = os.environ.get("FOCUS_REFERENCE_QA_DIR")
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(output) / "conversation-reference.png"))

        page.locator("#mainInput").fill("这行会在哪里等待？")
        page.evaluate("referenceHarness.fail = true")
        page.locator('[data-action="send-main"]').click()
        expect(page.locator("#composerFeedback")).to_contain_text("尚未确认")
        expect(page.locator("#composerReference blockquote")).to_have_text(saved["text"])
        page.evaluate("referenceHarness.fail = false")
        page.locator('[data-action="retry-main"]').click()
        expect(page.locator("#composerReference blockquote")).to_have_count(0)
        page.wait_for_function("referenceHarness.requests.length === 2")
        requests = page.evaluate("referenceHarness.requests")
        assert requests[0] == requests[1]
        assert requests[0]["body"]["message_reference"] == saved
        assert requests[0]["body"]["message"] == "这行会在哪里等待？"
        assert errors == [], errors
        print("CONVERSATION_REFERENCE_BROWSER_PASS")
    finally:
        browser.close()
