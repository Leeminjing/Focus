"""Browser regression: real app.js, native composition, focus, selection and undo.

Run: python desktop/composer-continuity.browser.py
Requires Python Playwright and Chrome (or a Playwright Chromium installation).
All resources and API responses are local fixtures; no backend or model is used.
"""
import mimetypes
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
SETUP = """mode => {
  state.view = 'focus';
  state.tasks = ['a', 'b'].map(id => ({ task_id: id, thread_id: 'thread-' + id,
    workspace_id: 'ws', workspace_path: 'C:/ws', title: id, harness_mode: mode }));
  state.activeTaskId = 'b';
  state.contextTrees.set('ws', []);
  for (const task of state.tasks) state.details.set(task.task_id, { messages: [], ui_state: {} });
  render();
  window.editor = document.querySelector('#mainInput');
  window.editEvents = [];
  for (const type of ['blur', 'compositionstart', 'compositionend'])
    editor.addEventListener(type, () => editEvents.push(type));
  window.editorRemovals = [];
  window.editorObserver = new MutationObserver(records => {
    for (const record of records) for (const node of record.removedNodes)
      if (node === editor || node.contains?.(editor)) editorRemovals.push(node.nodeName);
  });
  editorObserver.observe(document.querySelector('#app'), { childList: true, subtree: true });
  api = async path => {
    if (path === '/desktop/api/tasks') return state.tasks;
    if (path.includes('/contexts/tree')) return [];
    if (/\\/(agents|materials|material-groups|material-history)$/.test(path)) return [];
    if (path.endsWith('/skills')) return { skills: [] };
    return { messages: [{ id: 'done', role: 'ai', content: '完成' }], ui_state: {} };
  };
}"""


def serve(route):
    relative = urlparse(route.request.url).path.removeprefix("/desktop/")
    target = (ROOT / relative).resolve()
    if not target.is_relative_to(ROOT) or not target.is_file():
        route.fulfill(status=404, body="Not found")
        return
    body = target.read_bytes()
    if target.name == "app.js":
        body = re.sub(
            rb"runUiAction\(async \(\) => \{ await bootstrap\(\); if \(desktopPatrol\) await desktopPatrol.ready\(\); \}\);\s*$",
            b"", body,
        )
    route.fulfill(body=body, content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream")


def assert_editor(page, composing=False):
    result = page.evaluate("""() => ({
      same: document.querySelector('#mainInput') === editor,
      focused: document.activeElement === editor,
      removed: editorRemovals,
      events: editEvents,
      value: editor.value
    })""")
    assert result["same"] and result["focused"], result
    assert result["removed"] == [], result
    assert "blur" not in result["events"], result
    if composing:
        assert result["events"].count("compositionstart") == 1, result
        assert "compositionend" not in result["events"], result
    return result


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, **(
            {"executable_path": str(CHROME)} if CHROME.exists() else {}))
        try:
            page = browser.new_page()
            page.route("http://localhost:7229/**", serve)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            for mode in ["workspace", "assembly"]:
                page.goto("http://localhost:7229/desktop/index.html", wait_until="networkidle")
                page.evaluate(SETUP, mode)
                editor = page.locator("#mainInput")
                editor.focus()
                cdp = page.context.new_cdp_session(page)
                cdp.send("Input.imeSetComposition", {"text": "中文补充", "selectionStart": 2, "selectionEnd": 4})
                selection = editor.evaluate("e => [e.selectionStart, e.selectionEnd]")
                page.evaluate("""async () => {
                  const run = { run_id: 'run-a', task_id: 'a', kind: 'main', status: 'running' };
                  const source = { close() {} };
                  state.streams.set(run.run_id, source);
                  await settleRunStream(run, source, 'a', { status: 'success' });
                  renderFocus();
                }""")
                assert_editor(page, composing=True)
                assert editor.evaluate("e => [e.selectionStart, e.selectionEnd]") == selection
                cdp.send("Input.insertText", {"text": "中文补充"})
                assert editor.input_value() == "中文补充"
                print(f"PASS {mode}: background completion, same-task render, native IME and selection")

                # A receipt arrives during a new composition. At compositionstart
                # the value can still equal the submitted text; it must not clear.
                for direct in [False, True]:
                    editor.fill("已发送")
                    page.evaluate("""() => {
                      api = async path => path.endsWith('/main/runs')
                        ? new Promise(resolve => window.receipt = resolve) : {};
                      window.sending = sendMain();
                    }""")
                    assert not editor.evaluate("e => e.readOnly")
                    cdp.send("Input.imeSetComposition", {"text": "下一条", "selectionStart": 3, "selectionEnd": 3})
                    page.evaluate("""async direct => {
                      receipt(direct ? { intent_kind: 'direct_message', intent_id: 'intent-b' }
                        : { run_id: 'run-b', task_id: 'b', message_id: 'sent', kind: 'main', status: 'running' });
                      await sending;
                    }""", direct)
                    # Each new composition starts after the preceding one ended.
                    current = page.evaluate("editEvents.slice(editEvents.lastIndexOf('compositionstart'))")
                    assert "compositionend" not in current, current
                    assert_editor(page)
                    assert "下一条" in editor.input_value()
                    cdp.send("Input.insertText", {"text": "下一条"})
                    print(f"PASS {mode}: {'direct-message' if direct else 'run'} receipt during native composition")

                # Preserve the browser's undo history across a background refresh.
                editor.fill("")
                editor.press_sequentially("undo survives")
                page.evaluate("renderFocus()")
                editor.press("Control+z")
                assert editor.input_value() != "undo survives"
                assert_editor(page)
                print(f"PASS {mode}: browser undo history survives render")
            assert not errors, errors
        finally:
            browser.close()


if __name__ == "__main__":
    main()
