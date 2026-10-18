"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const files = require("./composer-files.js");
const links = require("./file-links.js");
const preview = require("./file-preview.js");
const { createElement } = require("./test-dom.cjs");
const { createAppHarness, createNode, readAppSource } = require("./test-helper.cjs");

test("mentions respect caret, email boundaries and the existing compression command", () => {
  assert.deepEqual(files.mention("检查 @dispatch"), { query: "dispatch", start: 3, end: 12 });
  assert.equal(files.mention("someone@example.com"), null);
  assert.equal(files.mention("@关键字快捷压缩 提交"), null);
  assert.deepEqual(files.mention("@src 后面的文字", 4), { query: "src", start: 0, end: 4 });
});

test("quotation links retain full Unicode paths, literal hashes and line ranges", () => {
  const ref = { path: "C:/work tree/中文/#dispatch.py", start_line: 120, end_line: 148, text: "return `value`;\n```" };
  const message = files.compose("为什么重复提交", [ref]);
  assert.match(message, /dispatch.py · 第 120–148 行/);
  assert.match(message, /\n````\nreturn `value`;\n```\n````/);
  const href = message.match(/\]\(([^)]+)\)/)[1];
  assert.equal(links.referenceFromHref(href).path, ref.path);
  assert.equal(links.referenceFromHref(href).start_line, 120);
  assert.equal(links.referenceFromHref(href).end_line, 148);
  assert.equal(links.referenceFromHref("src/code.py#L10").end_line, 10);
  assert.equal(links.referenceFromHref("src/code.py#L20-L10").start_line, undefined);
});

test("preview highlights the requested lines and adds exact text using Shift selection", async () => {
  const host = createElement("div"), refs = [];
  global.FocusFileLinks = links;
  await preview.mount(host, { name: "code.py", target: { link: { path: "src/code.py", start_line: 2, end_line: 3 } },
    text: "one\ntwo\nthree\nfour", onReference: ref => refs.push(ref) });
  assert.equal(host.querySelectorAll(".is-referenced").length, 2);
  host.querySelector("[data-add-reference]").dispatchEvent({ type: "click" });
  assert.deepEqual(refs[0], { path: "src/code.py", start_line: 2, end_line: 3, text: "two\nthree" });
  const content = host.querySelector(".file-text-body");
  content.dispatchEvent({ type: "click", target: host.querySelector('[data-select-line="4"]') });
  content.dispatchEvent({ type: "click", target: host.querySelector('[data-select-line="2"]'), shiftKey: true });
  host.querySelector("[data-add-reference]").dispatchEvent({ type: "click" });
  assert.equal(refs[1].text, "two\nthree\nfour");
  assert.equal(refs[1].start_line, 2);
  assert.equal(refs[1].end_line, 4);
});

test("a reference beyond line 10000 opens a bounded window with original line numbers", async () => {
  const host = createElement("div");
  await preview.mount(host, { name: "large.txt", target: { link: { path: "large.txt", start_line: 11990, end_line: 11995 } },
    text: Array.from({ length: 12000 }, (_, index) => `line ${index + 1}`).join("\n") });
  assert.equal(host.querySelectorAll(".file-source-line").length, 10000);
  assert.equal(host.querySelectorAll(".is-referenced").length, 6);
  assert.equal(host.querySelector('[data-line="11990"]').textContent, "line 11990");
});

function setup() {
  const input = createNode({ value: "检查重复提交" });
  const h = createAppHarness({ selectors: { "#mainInput": input }, statusNode: "record" });
  const evaluate = code => h.vm.runInContext(code, h.context);
  evaluate(readAppSource());
  evaluate(`state.activeTaskId = 'A'; state.tasks = [{ task_id:'A', workspace_id:'w' }, { task_id:'B', workspace_id:'w' }];
    state.details.set('A', { messages:[], ui_state:{} }); state.details.set('B', { messages:[], ui_state:{} });
    renderFocus = () => {}; listenToRun = () => {}; persistFocusState = () => {}; persistUiState = async () => {};
    globalThis.requests = []; globalThis.fail = true;
    api = async (path, options) => {
      if (path.endsWith('/main/runs')) { requests.push(options.body); if (fail) throw new Error('Failed to fetch'); return { intent_kind:'direct_message', intent_id:'accepted' }; }
    };
    addFileReference({path:'C:/workspace/dispatch.py', start_line:120, end_line:121, text:'submit();\\nsubmit();'}, 'A');`);
  return { ...h, input, evaluate };
}

test("failed send keeps quotations; retry replays frozen text and preserves new references", async () => {
  const h = setup();
  await h.evaluate("sendMain()");
  assert.equal(h.evaluate("fileReferences('A').length"), 1);
  assert.match(JSON.parse(h.context.requests[0]).message, /submit\(\);\nsubmit\(\);/);
  h.evaluate("addFileReference({path:'C:/workspace/new.py'}, 'A'); fail = false;");
  await h.evaluate("sendMain(true)");
  assert.equal(h.context.requests[0], h.context.requests[1]);
  assert.equal(h.evaluate("fileReferences('A').length"), 1);
  assert.equal(h.evaluate("fileReferences('A')[0].path"), "C:/workspace/new.py");
});

test("references hydrate from saved UI state and remain owned by the originating task", () => {
  const h = setup();
  h.evaluate("state.composerFileReferences.delete('A'); state.activeTaskId = 'B';");
  assert.equal(h.evaluate("fileReferences('A')[0].start_line"), 120);
  assert.equal(h.evaluate("fileReferences('B').length"), 0);
  h.evaluate("addFileReference({path:'b.py'}, 'B');");
  assert.equal(h.evaluate("fileReferences('A').length"), 1);
  assert.equal(h.evaluate("state.details.get('B').ui_state.file_references[0].path"), "b.py");
});

test("the loading render does not mask persisted references arriving with task details", () => {
  const h = setup();
  h.evaluate("state.details.delete('A'); state.composerFileReferences.delete('A'); fileReferences('A');");
  h.evaluate("state.details.set('A', {ui_state:{file_references:[{path:'saved.py',start_line:10,end_line:20}]}});");
  assert.equal(h.evaluate("fileReferences('A')[0].start_line"), 10);
});

test("a reply link opens the referenced line range with its task and workspace", () => {
  const h = setup();
  h.evaluate("render = () => {};");
  const anchor = { dataset: {}, getAttribute: () => "src/dispatch.py#L120-L148", closest: () => null };
  h.dispatch("click", { target: { closest: selector => selector === "a[href]" ? anchor : null }, preventDefault() {}, stopPropagation() {} });
  assert.equal(h.evaluate("state.filesPanel.start_line"), 120);
  assert.equal(h.evaluate("state.filesPanel.end_line"), 148);
  assert.equal(h.evaluate("state.filesPanel.task_id"), "A");
});
