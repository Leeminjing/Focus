"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");
const { createElement } = require("./test-dom.cjs");

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
      ], uiKeys: ["a", "b", "c"], expandedKey: ${JSON.stringify(expandedKey)}
    };
    contextSourcePanelMarkup = () => "";
    renderContextEditor();
    api = async (_url, options) => { captureSubmission(options.body); return { projection_status: "valid" }; };
    refreshTasks = async () => {};
    hydrateContextTrees = async () => {};
    openDerivedContext = () => {};
  `);
  // The minimal DOM does not decode textarea HTML entities as a browser does.
  for (const input of root.querySelectorAll("textarea")) {
    input.value = run(`JSON.stringify(state.contextDraft.messages[${Number(input.dataset.contextMessageIndex)}])`);
  }
  for (const row of root.querySelectorAll(".context-message-editor")) {
    row.animate = () => {
      let finish;
      const finished = new Promise(resolve => { finish = resolve; });
      animations.push(finish);
      return { finished };
    };
  }
  const click = button => {
    context.clickTarget = button;
    return run("handleDocumentClick({ target: clickTarget })");
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
