"""Browser acceptance for the shared viewer. Run with python desktop/tests/e2e/file-preview.browser.py."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

DESKTOP = Path(__file__).resolve().parents[2] / "src/renderer"
PLUGIN = DESKTOP.parents[2] / "plugins" / "spatial-patrol" / "desktop"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1000, "height": 760})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def serve(route):
        if route.request.url == "http://localhost/":
            route.fulfill(content_type="text/html", body='<div id="host" style="width:430px;height:650px;display:flex;flex-direction:column"></div>')
            return
        if "/docx/sessions" in route.request.url:
            route.fulfill(status=503, content_type="application/json", body=json.dumps({"detail": "Document Server unavailable"}))
            return
        if "/files/text" in route.request.url:
            route.fulfill(content_type="application/json", body=json.dumps({"content": "DOCX fallback paragraph\nTable content"}))
            return
        if "/files/content" in route.request.url:
            route.fulfill(status=409, content_type="application/json", body=json.dumps({"detail": "磁盘文件已变化，此历史版本未保存正文"}))
            return
        route.fulfill(status=404, body="not found")

    page.route("http://localhost/**", serve)
    page.goto("http://localhost/")
    page.wait_for_load_state("networkidle")
    page.add_style_tag(path=str(DESKTOP / "shared/styles/components.css"))
    page.add_style_tag(path=str(DESKTOP / "features/files/file-preview.css"))
    page.add_style_tag(path=str(PLUGIN / "docx-editor.css"))
    for source in [DESKTOP / "vendor" / "markdown-it.min.js", DESKTOP / "features/files/file-preview.js", PLUGIN / "docx-editor.js", PLUGIN / "viewer.js"]:
        page.add_script_tag(path=str(source))
    page.evaluate("window.actions=[]; window.focusDesktop={runtime:()=>({session:'test'}),fileAction:async value=>{actions.push(value);return true}}")

    def mount(name, text):
        page.evaluate("async ({name,text})=>await FocusFilePreview.mount(document.querySelector('#host'),{name,text,target:{task_id:'task',content_ref:name}})", {"name": name, "text": text})

    mount("script.js", 'const html = "<img src=x onerror=alert(1)>";\nreturn 42;')
    expect(page.locator(".file-source-line")).to_have_count(2)
    expect(page.locator(".file-source-line").first).to_have_text('const html = "<img src=x onerror=alert(1)>";')
    expect(page.locator("#host img")).to_have_count(0)
    # Inline line-number buttons must fit their source rows under shared button styles.
    def assert_line_number_alignment():
        rows = page.locator(".file-source-line").evaluate_all("""lines => lines.map(line => {
          const row = line.getBoundingClientRect();
          const number = line.querySelector('.file-line-number').getBoundingClientRect();
          return {line: line.dataset.line, rowTop: row.top, rowBottom: row.bottom,
            numberTop: number.top, numberBottom: number.bottom};
        })""")
        assert rows
        for row in rows:
            assert abs(row["numberTop"] - row["rowTop"]) < 0.1, row
            assert abs(row["numberBottom"] - row["rowBottom"]) < 0.1, row

    assert_line_number_alignment()
    assert page.get_by_text("复制全文", exact=True).evaluate("el => el.getBoundingClientRect().height >= 34")
    page.get_by_role("searchbox").fill("return")
    expect(page.locator(".is-match")).to_have_count(1)
    page.get_by_text("下一个", exact=True).click()
    expect(page.locator(".is-current")).to_have_text("return 42;")
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.get_by_text("复制全文", exact=True).click()
    expect(page.get_by_text("已复制", exact=True)).to_be_visible()
    assert page.evaluate("navigator.clipboard.readText()").replace("\r\n", "\n") == 'const html = "<img src=x onerror=alert(1)>";\nreturn 42;'

    # A search must retain source/syntax nodes, selection and scroll position.
    large_text = "\n".join(f"const value{i} = {i}; // {'needle' if i in [2, 8999] else 'row'}" for i in range(10000))
    mount("large.js", large_text)
    assert page.locator(".file-source-line").count() <= 200
    page.locator('[data-select-line="3"]').click()
    page.evaluate("""() => {
      window.sourceBefore = document.querySelector('.file-source');
      window.lineBefore = document.querySelector('[data-line="3"]');
      window.syntaxBefore = lineBefore.querySelector('.syntax-keyword');
      window.sourceChanges = [];
      window.sourceObserver = new MutationObserver(records => sourceChanges.push(...records));
      sourceObserver.observe(sourceBefore, {childList:true, subtree:true});
      const input = document.querySelector('[data-file-search]');
      for (const value of ['n', 'ne', 'nee', 'needle']) {
        input.value = value; input.dispatchEvent(new Event('input', {bubbles:true}));
      }
    }""")
    expect(page.locator("[data-search-count]")).to_have_text("2 行匹配")
    assert page.evaluate("sourceChanges.length") == 0
    assert page.evaluate("document.querySelector('[data-line=\"3\"]') === lineBefore && lineBefore.querySelector('.syntax-keyword') === syntaxBefore")
    expect(page.locator('[data-line="3"]')).to_have_class("file-source-line is-referenced is-match")
    page.evaluate("sourceObserver.disconnect()")
    page.get_by_text("下一个", exact=True).click()
    expect(page.locator(".is-current")).to_have_attribute("data-line", "3")
    page.get_by_text("下一个", exact=True).click()
    expect(page.locator(".is-current")).to_have_attribute("data-line", "9000")
    assert page.locator(".is-current").evaluate("el => {const r=el.getBoundingClientRect(), p=el.closest('.file-text-body').getBoundingClientRect(); return r.top >= p.top && r.bottom <= p.bottom}")
    assert page.locator(".file-source-line").count() <= 200
    search_scroll = page.locator(".file-text-body").evaluate("el=>el.scrollTop")
    page.get_by_role("searchbox").fill("no matches")
    expect(page.locator("[data-search-count]")).to_have_text("0 行匹配")
    assert page.locator(".file-text-body").evaluate("el=>el.scrollTop") == search_scroll
    expect(page.locator(".is-current")).to_have_count(0)
    page.get_by_role("searchbox").fill("")
    expect(page.locator("[data-search-count]")).to_be_empty()
    page.locator(".file-text-body").evaluate("el=>el.scrollTop=el.scrollHeight")
    expect(page.locator('[data-line="10000"]')).to_be_attached()
    assert_line_number_alignment()
    page.locator(".file-text-body").evaluate("el=>el.scrollTop=0")
    expect(page.locator('[data-line="3"]')).to_have_class("file-source-line is-referenced")
    page.get_by_text("复制全文", exact=True).click()
    expect(page.get_by_text("已复制", exact=True)).to_be_visible()
    assert page.evaluate("navigator.clipboard.readText()").replace("\r\n", "\n") == large_text
    destination = os.environ.get("FOCUS_FILE_PREVIEW_QA_DIR")
    if destination:
        Path(destination).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(destination) / "large-file-preview.png"))

    # Deep references retain original line numbers and the existing search scope.
    page.evaluate("""() => FocusFilePreview.renderText(document.querySelector('[data-file-content]'),
      Array.from({length:12000}, (_,i) => `line ${i+1}`).join('\\n'), 'large.txt',
      {range:{start_line:11990,end_line:11995},onReference:ref=>window.lastReference=ref})""")
    expect(page.locator(".is-referenced")).to_have_count(6)
    expect(page.locator('[data-line="11990"]')).to_be_in_viewport()
    page.get_by_text("加入当前问题", exact=True).click()
    assert page.evaluate("lastReference.text") == "\n".join(f"line {i}" for i in range(11990, 11996))

    # Recycled windows keep long-line scrolling and Shift selection across windows.
    page.evaluate("""() => FocusFilePreview.renderText(document.querySelector('[data-file-content]'),
      Array.from({length:10000}, (_,i) => i === 0 ? 'x'.repeat(500) : `line ${i+1}`).join('\\n'),
      'wide.txt', {onReference:ref=>window.lastReference=ref})""")
    page.locator('[data-select-line="1"]').click()
    page.locator(".file-text-body").evaluate("el=>{el.scrollLeft=500;el.scrollTop=el.scrollHeight}")
    expect(page.locator('[data-line="10000"]')).to_be_attached()
    assert page.locator(".file-text-body").evaluate("el=>el.scrollLeft") == 500
    page.locator('[data-select-line="10000"]').click(modifiers=["Shift"])
    page.get_by_text("加入当前问题", exact=True).click()
    assert page.evaluate("lastReference.start_line === 1 && lastReference.end_line === 10000 && lastReference.text.split('\\n').length === 10000")

    mount("readme.md", "# Preview heading\n\n**Bold content**\n\n<script>window.executed=true</script>")
    expect(page.get_by_role("heading", name="Preview heading")).to_be_visible()
    page.get_by_text("查看源码", exact=True).click()
    expect(page.locator(".file-source-line").first).to_have_text("# Preview heading")
    assert not page.evaluate("window.executed === true")

    mount("table.csv", 'name,notes,' + ','.join("column" + str(i) for i in range(20)) + '\n"a,b","line one\nline two",' + ','.join(str(i) for i in range(20)))
    expect(page.locator(".file-csv td").first).to_have_text("a,b")
    expect(page.locator(".file-csv td").nth(1)).to_have_text("line one\nline two")
    assert page.locator(".file-text-body").evaluate("el=>el.scrollWidth>el.clientWidth")

    mount("page.html", '<h1>Hello</h1><script>window.executed=true</script>')
    expect(page.locator("#host h1")).to_have_count(0)
    page.get_by_text("独立预览 HTML", exact=True).click()
    expect(page.locator("[data-file-status]")).to_have_text("已完成")
    assert page.evaluate("actions.at(-1).operation") == "html"

    page.evaluate("async ()=>await FocusFilePreview.mount(document.querySelector('#host'),{name:'image.png',target:{loop_id:'loop',fact_id:'fact',revision:2}})")
    expect(page.get_by_text("磁盘文件已变化，此历史版本未保存正文。可使用上方文件操作。", exact=True)).to_be_visible()
    page.get_by_text("打开当前文件", exact=True).click()
    expect(page.locator("[data-file-status]")).to_have_text("已完成")
    assert page.evaluate("actions.at(-1).target.current") is True

    page.evaluate("FocusDocxEditor.mount(document.querySelector('#host'),{relative_path:'report.docx'},{activeTaskId:'task',tasks:[{task_id:'task'}]})")
    page.get_by_text("只读打开", exact=True).click()
    expect(page.locator("[data-docx-status]")).to_have_text("编辑服务不可用，已回退只读文本")
    expect(page.locator(".file-source")).to_contain_text("DOCX fallback paragraph")
    page.get_by_text("关闭", exact=True).click()
    page.evaluate("FocusSpatialViewer.mountPanel(document.querySelector('#host'),{relative_path:'data/result.json'},{activeTaskId:'task',tasks:[{task_id:'task'}]})")
    expect(page.get_by_text("用系统应用打开", exact=True)).to_be_visible()
    page.get_by_text("打开所在文件夹", exact=True).click()
    expect(page.locator("[data-file-status]")).to_have_text("已完成")
    assert page.evaluate("actions.at(-1).target.content_ref") == "data/result.json"
    assert not errors, errors
    browser.close()
    print("Browser acceptance passed: source/search/copy/Markdown/CSV/HTML/history/DOCX fallback")
