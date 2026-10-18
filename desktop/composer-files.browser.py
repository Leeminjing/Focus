"""Real composer acceptance with deterministic APIs; no model calls.

Run: python desktop/composer-files.browser.py
Optional FOCUS_COMPOSER_QA_DIR saves screenshots for visual review.
"""
import os
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

DESKTOP = Path(__file__).resolve().parent

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_default_timeout(8000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script(path=str(DESKTOP / "context-ui-test-preload.cjs"))
        page.add_init_script("""(() => {
          const baseFetch = window.fetch;
          window.fileHarness = { requests: [], searchRequests: [], fail: false, hold: false, release: null };
          window.fetch = async (input, options = {}) => {
            const url = new URL(String(input), 'http://focus.test');
            if (url.pathname.endsWith('/files/search')) {
              const q = url.searchParams.get('q'); fileHarness.searchRequests.push(q);
              if (fileHarness.hold) await new Promise(resolve => fileHarness.release = resolve);
              return new Response(JSON.stringify({files: q === 'missing' ? [] : [
                {path:'C:/workspace/src/dispatch.py', relative_path:'src/dispatch.py', workspace_id:'workspace'},
                {path:'C:/workspace/test/dispatch.py', relative_path:'test/dispatch.py', workspace_id:'workspace'}]}));
            }
            if (url.pathname.includes('/files/content')) return new Response(Array.from({length:180}, (_,i) => 'submit(' + (i+1) + ');').join('\\n'));
            if (url.pathname.endsWith('/main/runs')) {
              fileHarness.requests.push(options.body);
              if (fileHarness.fail) throw new TypeError('Failed to fetch');
              return new Response(JSON.stringify({intent_kind:'direct_message', intent_id:'accepted'}));
            }
            if (url.pathname === '/desktop/api/plugins') return new Response(JSON.stringify([]));
            if (url.pathname === '/desktop/api/tasks/child' && localStorage.getItem('fileHarness-ui')) {
              const detail = await (await baseFetch(input, options)).json();
              return new Response(JSON.stringify({...detail, ui_state:JSON.parse(localStorage.getItem('fileHarness-ui'))}));
            }
            return baseFetch(input, options);
          };
        })();""")
        page.goto((DESKTOP / "index.html").as_uri())
        page.wait_for_load_state("networkidle")
        page.locator('[data-action="focus-home"]').click()
        page.locator("#mainInput").wait_for()
        page.evaluate("state.details.get(state.activeTaskId).ui_state.show_session_patrol = false; renderFocus()")

        def screenshot(name):
            destination = os.environ.get("FOCUS_COMPOSER_QA_DIR")
            if destination:
                Path(destination).mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(Path(destination) / name))

        screenshot("composer-files-initial.png")
        textarea = page.locator("#mainInput")
        textarea.fill("检查 @dispatch")
        expect(page.locator("#composerFileMenu [role=option]")).to_have_count(2)
        expect(textarea).to_have_attribute("aria-expanded", "true")
        textarea.press("ArrowDown")
        textarea.press("Enter")
        expect(textarea).to_have_value("检查 ")
        expect(page.locator("#composerFileReferences")).to_contain_text("dispatch.py")
        assert page.evaluate("fileHarness.requests.length") == 0
        expect(page.locator(".composer-file-card")).to_have_attribute("title", "C:/workspace/test/dispatch.py")

        # Mouse selection must not lose the caret or send the message.
        textarea.fill("检查 @dispatch")
        page.locator("#composerFileMenu [role=option]").first.click()
        expect(page.locator(".composer-file-card")).to_have_count(2)
        page.locator('[data-action="open-composer-file"]').last.click()
        expect(page.locator(".file-source-line")).to_have_count(180)
        page.locator('[data-select-line="120"]').click()
        page.locator('[data-select-line="148"]').click(modifiers=["Shift"])
        expect(page.locator(".is-referenced")).to_have_count(29)
        page.get_by_role("button", name="加入当前问题", exact=True).click()
        expect(page.locator("#composerFileReferences")).to_contain_text("第 120–148 行")
        screenshot("composer-files-quotation.png")

        # Click a range card and verify scroll + highlighting after remount.
        page.locator('[data-action="open-composer-file"]').last.click()
        expect(page.locator(".is-referenced")).to_have_count(29)
        assert page.locator(".file-text-body").evaluate("el => el.scrollTop > 0")
        assert page.locator('[data-line="120"]').evaluate("el => { const r=el.getBoundingClientRect(), p=el.closest('.file-text-body').getBoundingClientRect(); return r.top>=p.top && r.bottom<=p.bottom; }")

        # Native text selection produces exact inclusive line boundaries.
        page.locator(".file-text-body").evaluate("""el => {
          const range = document.createRange();
          range.setStart(el.querySelector('[data-line="5"] .syntax-number').firstChild, 0);
          range.setEnd(el.querySelector('[data-line="7"] .syntax-number').firstChild, 0);
          getSelection().removeAllRanges(); getSelection().addRange(range);
          el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));
        }""")
        page.get_by_role("button", name="加入当前问题", exact=True).click()
        expect(page.locator("#composerFileReferences")).to_contain_text("第 5–7 行")

        # Saved UI state includes exact excerpts; retry replays the frozen request.
        page.wait_for_function("() => threadUiHarness !== undefined")
        saved = page.evaluate("state.details.get(state.activeTaskId).ui_state")
        assert saved["file_references"][-1]["text"] == "submit(5);\nsubmit(6);\nsubmit(7);"
        page.evaluate("localStorage.setItem('fileHarness-ui', JSON.stringify(state.details.get(state.activeTaskId).ui_state))")
        page.reload()
        page.wait_for_load_state("networkidle")
        page.locator('[data-action="focus-home"]').click()
        expect(page.locator(".composer-file-card")).to_have_count(4)
        expect(page.locator("#composerFileReferences")).to_contain_text("第 5–7 行")
        page.evaluate("fileHarness.fail = true")
        textarea.fill("检查这里为什么重复提交")
        textarea.press("Enter")
        expect(page.locator("#composerFeedback")).to_contain_text("尚未确认")
        expect(page.locator(".composer-file-card")).to_have_count(4)
        first = page.evaluate("fileHarness.requests[0]")
        assert "#L120-L148" in first and "submit(120);" in first and "submit(148);" in first
        page.evaluate("fileHarness.fail = false")
        page.locator('[data-action="retry-main"]').click()
        expect(page.locator(".composer-file-card")).to_have_count(0)
        assert page.evaluate("fileHarness.requests[1]") == first

        # Render a real assistant Markdown link and use the app's delegated handler.
        page.evaluate("""() => {
          const node = document.createElement('div');
          node.innerHTML = renderAssistantContent('[dispatch.py · 第 120–148 行](C%3A/workspace/src/dispatch.py#L120-L148)');
          document.querySelector('#conversation').append(node);
        }""")
        page.get_by_role("link", name="dispatch.py · 第 120–148 行").click()
        expect(page.locator(".is-referenced")).to_have_count(29)
        assert page.locator(".file-text-body").evaluate("el => el.scrollTop > 0")
        screenshot("composer-files-answer-link.png")

        # Stale search results must stay dismissed; IME Enter must not select/send.
        textarea.fill("@dispatch")
        expect(page.locator("#composerFileMenu [role=option]")).to_have_count(2)
        textarea.evaluate("el => el.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', isComposing:true, bubbles:true}))")
        expect(page.locator("#composerFileMenu")).to_be_visible()
        assert page.evaluate("fileHarness.requests.length") == 2
        textarea.press("Escape")
        page.evaluate("fileHarness.hold = true")
        textarea.fill("@slow")
        page.wait_for_function("() => fileHarness.release !== null")
        textarea.press("Escape")
        page.evaluate("fileHarness.release()")
        expect(page.locator("#composerFileMenu")).to_be_hidden()
        textarea.fill("/")
        expect(page.locator("#mainSkillList")).to_be_visible()
        expect(textarea).to_have_attribute("aria-controls", "mainSkillList")
        expect(textarea).to_have_attribute("aria-expanded", "true")
        page.evaluate("fileHarness.hold = false")
        textarea.fill("@dispatch")
        expect(page.locator("#composerFileMenu [role=option]")).to_have_count(2)
        expect(page.locator("#mainSkillList")).to_be_hidden()
        textarea.press("Escape")
        # Existing material previews pass the quotation callback through the plugin host.
        page.add_script_tag(url=(DESKTOP.parent / "plugins/spatial-patrol/desktop/viewer.js").as_uri())
        page.add_script_tag(url=(DESKTOP.parent / "plugins/spatial-patrol/desktop/entry.js").as_uri())
        page.evaluate("""() => {
          state.filesPanel = { relative_path:'src/dispatch.py' };
          mountFilePanel();
        }""")
        expect(page.locator(".file-source-line")).to_have_count(180)
        page.locator('[data-select-line="5"]').click()
        page.get_by_role("button", name="加入当前问题", exact=True).click()
        expect(page.locator("#composerFileReferences")).to_contain_text("第 5–5 行")
        assert page.evaluate("fileReferences(state.activeTaskId).at(-1).path").endswith("/src/dispatch.py")
        assert not errors, errors
        print("composer-files.browser.py OK")
    finally:
        browser.close()
