/* Regression coverage for the live task composer. Uses app.js and the real DOM
 * test tree so replacing and then reattaching a textarea cannot pass unnoticed.
 * Run: node --test desktop/composer-continuity.test.cjs
 */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");
const { createElement } = require("./test-dom.cjs");

function fixture(mode = "workspace") {
  const app = createElement("main");
  app.rootMarker = true;
  const inspector = createElement("aside");
  const shell = createElement("div");
  const frames = [];
  const h = createAppHarness({ fetch: true, selectors: {
    "#app": app, "#appInspector": inspector, ".app-shell": shell,
    "#conversation": () => app.querySelector("#conversation"),
  }, globals: { requestAnimationFrame: callback => frames.push(callback) } });
  const originalQuery = h.document.querySelector;
  h.document.querySelector = selector => {
    const node = app.querySelector(selector);
    if (node) return node;
    if (["#mainInput", ".context-rail-list"].includes(selector)) return null;
    return originalQuery(selector);
  };
  h.app = app;
  h.evaluate = source => h.vm.runInContext(source, h.context);
  h.flushFrames = () => { while (frames.length) frames.shift()(); };
  h.evaluate(readAppSource());
  h.context.__mode = mode;
  h.evaluate(`
    state.view = "focus";
    state.tasks = ["a", "b"].map(id => ({ task_id: id, thread_id: "thread-" + id,
      workspace_id: "ws", workspace_path: "C:/ws", title: id, harness_mode: __mode }));
    state.activeTaskId = "b";
    for (const task of state.tasks) state.details.set(task.task_id, { messages: [], ui_state: {} });
    state.contextTrees.set("ws", []);
    renderFocus();
  `);
  h.input = app.querySelector("#mainInput");
  h.input.id = "mainInput";
  h.type = (text, isComposing = false) => {
    if (isComposing) h.dispatch("compositionstart", { target: h.input });
    h.input.value = text;
    h.dispatch("input", { target: h.input, isComposing });
  };
  return h;
}

test("file panel, feedback and skill changes preserve the editor", async () => {
  const h = fixture();
  h.type("保留草稿");
  await withConnectedInput(h, () => h.evaluate(`
    state.filesPanel = { material_id: "file", relative_path: "file.txt" };
    state.composerErrors.set("b", "暂时失败");
    state.details.get("b").ui_state.skills = ["example"];
    renderFocus();
  `));
  assert.ok(h.app.querySelector("#filePanel"));
  assert.match(h.app.querySelector("#composerFeedback").textContent, /暂时失败/);
  assert.match(h.app.querySelector(".skill-tags").textContent, /example/);
  await withConnectedInput(h, () => h.evaluate("renderFocus()"));
  assert.equal(h.app.querySelector("#panelResizer").listeners.get("pointerdown").length, 1);
  await withConnectedInput(h, () => h.evaluate('state.filesPanel = null; renderFocus()'));
  assert.equal(h.app.querySelector("#filePanel"), null);
  assert.equal(h.input.value, "保留草稿");
  await withConnectedInput(h, () => h.evaluate('selectSkill("main", "another")'));
  assert.equal(h.input.value, "", "explicit skill selection consumes its query");
  assert.equal(h.evaluate('composerDraft.value("b")'), "");
  assert.equal(h.app.querySelector("#mainSkillList").hidden, true);
});

test("returning to a task creates the right editor and restores only its draft", () => {
  const h = fixture();
  h.type("B 的草稿");
  h.evaluate('state.activeTaskId = "a"; renderFocus()');
  const other = h.app.querySelector("#mainInput");
  assert.ok(other !== h.input);
  assert.equal(other.value, "");
  h.evaluate('state.activeTaskId = "b"; renderFocus()');
  assert.match(h.app.querySelector("#mainInput").textContent, /B 的草稿/);
});

// Observe writes to any ancestor, not just final node identity. Removing a live
// ancestor ends a browser's editing session even if the same node is reattached.
async function withConnectedInput(h, action) {
  const input = h.input;
  const prototype = Object.getPrototypeOf(h.app);
  const html = Object.getOwnPropertyDescriptor(prototype, "innerHTML");
  const originalRemove = prototype.remove;
  const originalReplace = prototype.replaceWith;
  const originalInsert = prototype.insertBefore;
  const writes = [];
  Object.defineProperty(prototype, "innerHTML", { ...html, set(value) {
    if (this.isConnected && this.contains(input)) writes.push("innerHTML on input ancestor");
    html.set.call(this, value);
  } });
  prototype.remove = function () {
    if (this.isConnected && this.contains(input)) writes.push("remove input ancestor");
    return originalRemove.call(this);
  };
  prototype.replaceWith = function (...nodes) {
    if (this.isConnected && this.contains(input)) writes.push("replace input ancestor");
    return originalReplace.apply(this, nodes);
  };
  prototype.insertBefore = function (node, reference) {
    if (node.isConnected && node.contains(input)) writes.push("move input ancestor");
    return originalInsert.call(this, node, reference);
  };
  try { await action(); }
  finally {
    Object.defineProperty(prototype, "innerHTML", html);
    prototype.remove = originalRemove;
    prototype.replaceWith = originalReplace;
    prototype.insertBefore = originalInsert;
  }
  assert.ok(h.app.querySelector("#mainInput") === input, "keep the original textarea");
  assert.deepEqual(writes, [], "never detach the editor or any of its ancestors");
  assert.equal(input.isConnected, true);
}

for (const mode of ["workspace", "assembly"]) {
  test(`${mode}: same-task refresh keeps the composing textarea continuously attached`, async () => {
    const h = fixture(mode);
    h.type("正在写下一条补充", true);
    h.input.selectionStart = 2;
    h.input.selectionEnd = 5;
    await withConnectedInput(h, () => h.evaluate(`
      state.details.get("b").messages.push({ id: "reply", role: "ai", content: "已完成" });
      state.details.get("b").active_run = { run_id: "run-b", status: "running", kind: "main" };
      renderFocus();
    `));
    assert.equal(h.input.value, "正在写下一条补充");
    assert.equal(h.input.selectionStart, 2);
    assert.equal(h.input.selectionEnd, 5);
    assert.match(h.app.querySelector("#conversation").textContent, /已完成/);
    assert.ok(h.app.querySelector('[data-action="interrupt-main-run"]'), "run controls update");
  });
}

test("A finishes while B composes: the terminal event never rebuilds B", async () => {
  const h = fixture();
  h.type("B 的中文输入", true);
  h.evaluate(`
    globalThis.__run = { run_id: "run-a", task_id: "a", kind: "main", status: "running" };
    globalThis.__source = { close() {} };
    state.streams.set(__run.run_id, __source);
    api = async path => {
      if (path === "/desktop/api/tasks") return state.tasks;
      if (path.includes("context")) return [];
      return { task_id: "a", messages: [{ id: "a-done", role: "ai", content: "A 完成" }], ui_state: {} };
    };
  `);
  await withConnectedInput(h, async () => {
    await h.evaluate('settleRunStream(__run, __source, "a", { status: "success" })');
    h.flushFrames();
  });
  assert.equal(h.input.value, "B 的中文输入");
  assert.equal(h.evaluate('state.details.get("a").messages[0].content'), "A 完成");
});

for (const direct of [false, true]) {
  test(`${direct ? "direct message" : "run"} receipt preserves the next draft and editor`, async () => {
    const h = fixture();
    h.type("先发这一条");
    h.evaluate(`
      globalThis.__receipt = null;
      api = async (path, options) => path.endsWith("/main/runs")
        ? new Promise(resolve => { __receipt = resolve; }) : {};
      globalThis.__sending = sendMain();
    `);
    assert.equal(h.input.readOnly, false, "allow writing the next message during submission");
    h.type("接着输入下一条中文", true);
    await withConnectedInput(h, async () => {
      h.evaluate(direct
        ? '__receipt({ intent_kind: "direct_message", intent_id: "intent-b" })'
        : '__receipt({ run_id: "run-b", task_id: "b", message_id: "sent", kind: "main", status: "running" })');
      await h.evaluate("__sending");
    });
    assert.equal(h.input.value, "接着输入下一条中文");
    assert.equal(h.evaluate('composerDraft.value("b")'), "接着输入下一条中文");
  });

  test(`${direct ? "direct message" : "run"} receipt cannot clear a composition before its first input event`, async () => {
    const h = fixture();
    h.type("原消息");
    h.evaluate(`
      api = async path => path.endsWith("/main/runs")
        ? new Promise(resolve => { globalThis.__receipt = resolve; }) : {};
      globalThis.__sending = sendMain();
    `);
    h.dispatch("compositionstart", { target: h.input });
    await withConnectedInput(h, async () => {
      h.evaluate(direct
        ? '__receipt({ intent_kind: "direct_message", intent_id: "intent-b" })'
        : '__receipt({ run_id: "run-b", task_id: "b", message_id: "sent", kind: "main", status: "running" })');
      await h.evaluate("__sending");
    });
    assert.equal(h.input.value, "原消息", "do not assign value while IME owns the input");
    h.dispatch("compositionend", { target: h.input });
    h.type("原消息及中文补充");
    assert.equal(h.evaluate('composerDraft.value("b")'), "原消息及中文补充");
  });
}
