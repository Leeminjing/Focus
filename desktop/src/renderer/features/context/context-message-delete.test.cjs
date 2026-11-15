"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { createAppHarness, readAppSource } = require("../../../../tests/helpers/test-helper.cjs");
const { createElement } = require("../../../../tests/helpers/test-dom.cjs");

function setup({ expandedKey = null, reducedMotion = false } = {}) {
  const root = createElement("main");
  root.rootMarker = true;
  const animations = [];
  const submissions = [];
  const harness = createAppHarness({ selectors: { "#app": root }, globals: { matchMedia: () => ({ matches: reducedMotion }) } });
  const { vm, context, document } = harness;
  vm.runInContext(readAppSource(), context);
  const run = source => vm.runInContext(source, context);
  // Use the application's real rendering, click routing, draft reader and submit path.
  const originalQuery = document.querySelector.bind(document);
  document.querySelector = selector => root.querySelector(selector) || (selector.startsWith("#") ? originalQuery(selector) : null);
  document.querySelectorAll = selector => root.querySelectorAll(selector);
  context.captureSubmission = body => submissions.push(JSON.parse(body));
  run(`
    state.contextDraft = {
      title: "Regression", sources: [], messages: [
        { role: "human", content: "A" },
        { role: "ai", content: "B" },
        { role: "human", content: "C" }
      ], uiKeys: ["a", "b", "c"], expandedKey: ${JSON.stringify(expandedKey)}, editMode: "json"
    };
    contextSourcePanelMarkup = () => "";
    renderContextEditor();
    api = async (_url, options) => { captureSubmission(options.body); return { projection_status: "valid" }; };
    refreshTasks = async () => {};
    hydrateContextTrees = async () => {};
    openDerivedContext = () => {};
  `);
  const hydrated = new WeakSet();
  const hydrateRows = () => {
    // Match browser textarea initial values and Web Animations on newly rendered nodes.
    for (const input of root.querySelectorAll("textarea")) {
      if (hydrated.has(input)) continue;
      hydrated.add(input);
      const path = `state.contextDraft.messages[${Number(input.dataset.contextMessageIndex)}]`;
      input.value = input.hasAttribute("data-context-message-json") ? run(`JSON.stringify(${path})`)
        : input.dataset.contextPartIndex == null ? run(`${path}.content`) : run(`${path}.content[${Number(input.dataset.contextPartIndex)}].text`);
    }
    for (const row of root.querySelectorAll(".context-message-editor")) {
      if (row.animate) continue;
      row.animate = () => {
        let finish;
        const finished = new Promise(resolve => { finish = resolve; });
        animations.push(finish);
        return { finished };
      };
    }
  };
  hydrateRows();
  const click = async button => {
    context.clickTarget = button;
    const result = await run("handleDocumentClick({ target: clickTarget })");
    hydrateRows();
    return result;
  };
  const contents = () => JSON.parse(run("JSON.stringify(state.contextDraft.messages.map(message => message.content))"));
  const keys = () => [...root.querySelectorAll(".context-message-editor[data-context-ui-key]")].map(row => row.dataset.contextUiKey);
  const finish = async () => { animations.forEach(resolve => resolve()); await Promise.resolve(); };
  return { root, run, click, contents, keys, finish, submissions };
}

test("double delete during the exit animation preserves B and C in the draft, UI and submission", async () => {
  const h = setup();
  const button = h.root.querySelector('[data-context-ui-key="a"] [data-action="context-message-delete"]');
  const first = h.click(button);
  assert.equal(button.disabled, true);
  assert.equal(button.closest("article").hasAttribute("inert"), true);
  assert.deepEqual(h.keys(), ["b", "c"]);
  assert.deepEqual([...h.root.querySelectorAll(".context-message-editor[data-context-ui-key]")].map(row => row.dataset.contextMessageIndex), ["0", "1"]);
  assert.equal(h.root.querySelector(".context-compose-heading > div > span").textContent, "2 条消息");
  const second = h.click(button);
  await h.run("submitContext()");
  await h.finish();
  await Promise.all([first, second]);
  assert.deepEqual(h.contents(), ["B", "C"]);
  assert.deepEqual(h.keys(), ["b", "c"]);
  assert.deepEqual(h.submissions[0].messages.map(message => message.content), ["B", "C"]);
});

test("an expanded deleted row cannot write its JSON back while the animation runs", async () => {
  const h = setup({ expandedKey: "a" });
  const pending = h.run('deleteContextMessage("a")');
  await h.run('deleteContextMessage("a")');
  await h.run("submitContext()");
  assert.deepEqual(h.contents(), ["B", "C"]);
  assert.equal(h.run("state.contextDraft.expandedKey"), null);
  assert.deepEqual(h.submissions[0].messages.map(message => message.content), ["B", "C"]);
  await h.finish();
  await pending;
});

test("surviving expanded editors use their new index before submission", async () => {
  const h = setup({ expandedKey: "b" });
  const pending = h.run('deleteContextMessage("a")');
  const input = h.root.querySelector('[data-context-ui-key="b"] textarea');
  assert.equal(input.dataset.contextMessageIndex, "0");
  input.value = JSON.stringify({ role: "ai", content: "edited B" });
  await h.run("submitContext()");
  assert.deepEqual(h.submissions[0].messages.map(message => message.content), ["edited B", "C"]);
  await h.finish();
  await pending;
});

test("consecutive deletes keep stable identities and allow undo before either animation finishes", async () => {
  const h = setup();
  const a = h.root.querySelector('[data-context-ui-key="a"] [data-action="context-message-delete"]');
  const b = h.root.querySelector('[data-context-ui-key="b"] [data-action="context-message-delete"]');
  const first = h.click(a);
  const second = h.click(b);
  assert.deepEqual(h.contents(), ["C"]);
  assert.deepEqual(h.keys(), ["c"]);
  h.run("undoContextMessageDelete()");
  assert.deepEqual(h.contents(), ["B", "C"]);
  assert.deepEqual(h.keys(), ["b", "c"]);
  await h.finish();
  await Promise.all([first, second]);
  assert.deepEqual(h.keys(), ["b", "c"]);
  assert.equal(h.run("state.contextDraft.undo"), null);
  assert.equal(h.root.querySelector(".context-undo-toast").hidden, true);
});

test("deleting the last active row shows the empty state and can be undone during animation", async () => {
  const h = setup();
  const pending = ["a", "b", "c"].map(key => h.run(`deleteContextMessage(${JSON.stringify(key)})`));
  assert.deepEqual(h.contents(), []);
  assert.deepEqual(h.keys(), []);
  assert.ok(h.root.querySelector(".context-empty-drop"));
  h.run("undoContextMessageDelete()");
  assert.deepEqual(h.contents(), ["C"]);
  assert.deepEqual(h.keys(), ["c"]);
  assert.equal(h.root.querySelector(".context-empty-drop"), null);
  await h.finish();
  await Promise.all(pending);
  assert.deepEqual(h.keys(), ["c"]);
});

test("animation completion cannot reopen undo in a newly rendered draft", async () => {
  const h = setup();
  const pending = h.run('deleteContextMessage("a")');
  h.run(`
    state.contextDraft = {
      title: "Other", sources: [], messages: [{ role: "human", content: "D" }], uiKeys: ["d"]
    };
    renderContextEditor();
  `);
  await h.finish();
  await pending;
  assert.deepEqual(h.contents(), ["D"]);
  assert.deepEqual(h.keys(), ["d"]);
  assert.equal(h.root.querySelector(".context-undo-toast").hidden, true);
});

test("reduced motion removes immediately and repeated deletion remains harmless", async () => {
  const h = setup({ reducedMotion: true, expandedKey: "a" });
  await h.run('deleteContextMessage("a")');
  await h.run('deleteContextMessage("a")');
  assert.equal(h.root.querySelectorAll(".context-message-editor").length, 2);
  await h.run("submitContext()");
  assert.deepEqual(h.contents(), ["B", "C"]);
  assert.deepEqual(h.submissions[0].messages.map(message => message.content), ["B", "C"]);
});

test("creating a duplicate reads pending body edits and preserves every message field independently", async () => {
  const h = setup();
  const original = { role: "tool", content: "result", tool_call_id: "call-1", name: "search", future: { nested: [null, false, { version: 3 }] } };
  h.run(`state.contextDraft.messages[0] = ${JSON.stringify(original)}; renderContextEditor()`);
  await h.click(h.root.querySelector('[data-context-ui-key="a"] [data-action="context-message-toggle"]'));
  h.root.querySelector("[data-context-message-content]").value = "edited before blur\nsecond line";
  await h.click(h.root.querySelector('[data-context-ui-key="a"] [data-action="context-message-duplicate"]'));
  assert.deepEqual(h.contents(), ["edited before blur\nsecond line", "edited before blur\nsecond line", "B", "C"]);
  assert.equal(new Set(h.keys()).size, 4);
  h.run('state.contextDraft.messages[1].future.nested[2].version = 4');
  assert.equal(h.run("state.contextDraft.messages[0].future.nested[2].version"), 3);
  await h.run("submitContext()");
  assert.deepEqual(h.submissions[0].messages[0], { ...original, content: "edited before blur\nsecond line" });
});

test("body and full JSON modes preserve multimodal blocks, tool calls and unknown fields through submission", async () => {
  const h = setup();
  const message = { role: "ai", content: [
    { type: "text", text: "before", annotations: [{ future: true }] },
    { type: "image_url", image_url: { url: "data:image/png;base64,exact" } },
    { type: "text", text: "after" },
  ], tool_calls: [{ id: "call-2", function: { name: "search", arguments: '{"q":"exact"}' } }], future: { keep: [1, null] } };
  h.run(`state.contextDraft.messages[1] = ${JSON.stringify(message)}; renderContextEditor()`);
  await h.click(h.root.querySelector('[data-context-ui-key="b"] [data-action="context-message-toggle"]'));
  const parts = h.root.querySelectorAll("[data-context-message-content]");
  assert.equal(parts.length, 2);
  parts[0].value = "edited before";
  parts[1].value = "edited after";
  await h.click(h.root.querySelector('[data-context-ui-key="b"] [data-action="context-message-edit-json"]'));
  assert.equal(h.root.querySelectorAll("[data-context-message-content]").length, 0);
  message.content[0].text = "edited before";
  message.content[2].text = "edited after";
  assert.deepEqual(JSON.parse(h.run("JSON.stringify(state.contextDraft.messages[1])")), message);
  message.future.addedInJson = { exact: true };
  h.root.querySelector("[data-context-message-json]").value = JSON.stringify(message);
  await h.click(h.root.querySelector('[data-context-ui-key="b"] [data-action="context-message-toggle"]'));
  assert.equal(h.root.querySelectorAll("[data-context-message-json]").length, 0);
  assert.equal(h.root.querySelector("[data-context-message-content]").value, "edited before");
  h.run("moveContextMessage(1, 0)");
  assert.equal(h.root.querySelector("[data-context-message-content]").dataset.contextMessageIndex, "0");
  assert.equal(h.root.querySelector("[data-context-message-content]").getAttribute("aria-label"), "消息 1 正文 1");
  await h.run("submitContext()");
  assert.deepEqual(h.submissions[0].messages[0], message);
});

test("deleting a body editor retires its inputs during animation and undo keeps the edit", async () => {
  const h = setup();
  await h.click(h.root.querySelector('[data-context-ui-key="a"] [data-action="context-message-toggle"]'));
  h.root.querySelector("[data-context-message-content]").value = "edited A";
  const pending = h.run('deleteContextMessage("a")');
  assert.equal(h.root.querySelectorAll("[data-context-message-content]").length, 0);
  await h.run("submitContext()");
  assert.deepEqual(h.submissions[0].messages.map(message => message.content), ["B", "C"]);
  h.run("undoContextMessageDelete()");
  assert.deepEqual(h.contents(), ["edited A", "B", "C"]);
  await h.finish();
  await pending;
});

test("invalid JSON prevents switching or duplication and keeps the input available for repair", async () => {
  const h = setup({ expandedKey: "a" });
  const input = h.root.querySelector("[data-context-message-json]");
  input.value = "invalid JSON";
  await h.click(h.root.querySelector('[data-context-ui-key="a"] [data-action="context-message-toggle"]'));
  await h.click(h.root.querySelector('[data-context-ui-key="a"] [data-action="context-message-duplicate"]'));
  assert.equal(h.root.querySelector("[data-context-message-json]"), input);
  assert.deepEqual(h.keys(), ["a", "b", "c"]);
  assert.deepEqual(h.contents(), ["A", "B", "C"]);
});

test("creating a duplicate reads pending full JSON edits including new extension fields", async () => {
  const h = setup({ expandedKey: "a" });
  const edited = { role: "ai", content: null, tool_calls: [{ id: "call-1", future: { raw: [null, 3] } }], extension: { exact: true } };
  h.root.querySelector("[data-context-message-json]").value = JSON.stringify(edited);
  await h.click(h.root.querySelector('[data-context-ui-key="a"] [data-action="context-message-duplicate"]'));
  await h.run("submitContext()");
  assert.deepEqual(h.submissions[0].messages.slice(0, 2), [edited, edited]);
});

test("adding a message closes the previous editor and renders the correct ordinal", async () => {
  const h = setup();
  await h.click(h.root.querySelector('[data-context-ui-key="b"] [data-action="context-message-toggle"]'));
  h.root.querySelector("[data-context-message-content]").value = "edited B";
  await h.click(h.root.querySelector('[data-action="context-message-add"]'));
  assert.equal(h.root.querySelectorAll("textarea").length, 1);
  assert.equal(h.root.querySelector("[data-context-message-content]").getAttribute("aria-label"), "消息 4 正文");
  assert.equal([...h.root.querySelectorAll("header strong")].at(-1).textContent, "04 · human");
  await h.run("submitContext()");
  assert.deepEqual(h.submissions[0].messages.map(message => message.content), ["A", "edited B", "C", ""]);
});
