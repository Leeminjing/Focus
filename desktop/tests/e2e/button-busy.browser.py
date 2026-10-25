"""Busy buttons retain their geometry. Run with python desktop/tests/e2e/button-busy.browser.py."""
from pathlib import Path

from playwright.sync_api import sync_playwright, expect


STYLES = Path(__file__).resolve().parents[2] / "src/renderer/shared/styles"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 360, "height": 240})
    page.set_content("""<div class="ui-toolbar" style="width: max-content">
      <button class="primary">保存</button>
      <button class="quiet-button">暂停</button>
      <button class="icon-button" aria-label="刷新">
        <svg width="16" height="16" aria-hidden="true"><circle cx="8" cy="8" r="5"/></svg>
      </button>
      <button class="text-button">详情</button>
      <button>取消</button>
    </div>""")
    for stylesheet in ["tokens.css", "base.css", "components.css"]:
        page.add_style_tag(path=str(STYLES / stylesheet))

    def geometry():
        return page.locator("button").evaluate_all("""buttons => buttons.map(button => {
          const {x, y, width, height} = button.getBoundingClientRect();
          return {x, y, width, height};
        })""")

    for language in ["zh-CN", "en-US"]:
        page.locator("html").evaluate("(el, lang) => el.lang = lang", language)
        for name in ["保存", "暂停", "刷新", "详情", "取消"]:
            button = page.get_by_role("button", name=name, exact=True)
            before = geometry()
            button.evaluate("el => el.setAttribute('aria-busy', 'true')")
            assert geometry() == before, f"{language} / {name}: busy state shifted buttons"
            expect(button).to_have_accessible_name(name)
            expect(button).to_have_css("cursor", "progress")
            expect(button).to_have_css("outline-style", "solid")
            button.evaluate("el => el.setAttribute('aria-busy', 'false')")
            assert geometry() == before
            button.evaluate("el => el.removeAttribute('aria-busy')")
            assert geometry() == before

    browser.close()
    print("Browser acceptance passed: busy button geometry, labels and feedback in Chinese/English")
