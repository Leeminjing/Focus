/* 通过真实 app.js 与可控 Promise 验证记忆库迟到响应不会覆盖后续导航和编辑。
 * 运行：node --test desktop/src/renderer/features/memory/memory-request-isolation.test.cjs。
 */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { createAppHarness, readAppSource } = require("../../../../tests/helpers/test-helper.cjs");

function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function harness() {
  const h = createAppHarness({ statusNode: "record" });
  h.vm.runInContext(require("node:fs").readFileSync(require.resolve("./memory.js"), "utf8"), h.context);
  h.vm.runInContext(readAppSource(), h.context);
  const run = code => h.vm.runInContext(code, h.context);
  // 测试跳过 bootstrap；补齐主线中已改为在 bootstrap 初始化的 Patrol intake。
  run('workspacePatrolIntake = FocusWorkspacePatrolIntake.create({ runtime, api: loopApi }); render = () => {}; renderMemory = () => {};');
  return { ...h, run };
}

function click(h, action) {
  h.context.button = { dataset: { action }, hasAttribute: () => false };
  return h.run('handleDocumentClick({ target: { closest: selector => selector === "[data-action]" ? button : null } })');
}

function compose(h, name = "A") {
  h.context.memoryName = name;
  h.run(`state.view = "memory";
    openMemoryCompose();
    syncMemoryField("title", memoryName);
    addSessionSource("context-" + memoryName, memoryName);`);
}

test("reading and checking messages update independent state without saving or compressing", async () => {
  const h = harness();
  compose(h);
  h.run(`state.memory.enabledMessages = [{id:"one",role:"human",content:"first"}, {id:"two",role:"ai",content:"second"}];
    state.memory.textSelection = "old"; state.memory.textMessageId = "old"; state.memory.textRange = {start:0,end:3};`);
  const draft = h.run("state.memory.draft"), sources = h.run("state.memory.collectedSources");
  h.context.api = () => { throw new Error("Reading must not call the API"); };
  h.context.button = { dataset: { action: "read-memory-message", messageId: "one" } };
  await h.run('handleDocumentClick({ target: { closest: selector => selector === "[data-action]" ? button : null } })');
  assert.equal(h.run("state.memory.activeMessageId"), "one");
  assert.equal(h.run("state.memory.selectedMessageIds.length"), 0);
  assert.equal(h.run("state.memory.textRange"), null);
  h.run('selectMemoryMessage("two", true); selectMemoryMessage("two", true);');
  assert.equal(h.run("state.memory.selectedMessageIds.join(',')"), "two");
  assert.equal(h.run("state.memory.activeMessageId"), "one");
  h.run('readMemoryMessage("two"); selectMemoryMessage("two", false);');
  assert.equal(h.run("state.memory.activeMessageId"), "two");
  assert.equal(h.run("state.memory.selectedMessageIds.length"), 0);
  assert.equal(h.run("state.memory.draft"), draft);
  assert.equal(h.run("state.memory.collectedSources"), sources);
});

test("merge checkboxes preserve source provenance through repeated merging", () => {
  const h = harness();
  compose(h);
  h.run(`state.memory.collectedSources = [
    {kind:"session",context_id:"session",rawText:"conversation"},
    {kind:"messages",context_id:"session",message_ids:["a","c"],rawText:"messages"},
    {kind:"text",context_id:"session",message_id:"c",ranges:[{start:12,end:20}],text:" selected",rawText:" selected"}
  ];`);
  const original = h.run("JSON.stringify(state.memory.collectedSources)");
  h.run('enterMergeSources(); selectMergeSource(1, true); selectMergeSource(2, true); selectMergeSource(2, true);');
  assert.equal(h.run("JSON.stringify(state.memory.collectedSources)"), original);
  h.run('confirmMergeSources(); enterMergeSources(); selectMergeSource(0, true); selectMergeSource(1, true); confirmMergeSources();');
  const merged = JSON.parse(h.run("JSON.stringify(state.memory.collectedSources[0])"));
  const originals = JSON.parse(original);
  assert.deepEqual(merged.subSources[0], originals[0]);
  assert.deepEqual(merged.subSources[1].subSources, originals.slice(1));
});

test("adding selected text keeps whitespace aligned with its character range", () => {
  const h = harness();
  compose(h);
  h.run('state.memory.activeSessionId = "context-A"; state.memory.textMessageId = "message"; state.memory.textRange = {start:7,end:17}; addTextSource(" selected ");');
  const source = JSON.parse(h.run("JSON.stringify(state.memory.collectedSources.at(-1))"));
  assert.equal(source.message_id, "message");
  assert.deepEqual(source.ranges, [{ start: 7, end: 17 }]);
  assert.equal(source.text, " selected ");
});

test("entering Memory then Patrol keeps Patrol after the list response arrives", async () => {
  const h = harness(), gate = deferred();
  h.context.api = () => gate.promise;
  const loading = h.run("openMemoryView()");
  await click(h, "show-patrol");
  assert.equal(h.run("state.view"), "patrol");
  gate.resolve({ memories: [] });
  await loading;
  assert.equal(h.run("state.view"), "patrol", "迟到的列表响应不能把页面切回记忆库");
});

test("compression of A cannot replace a later B draft", async () => {
  const h = harness(), gate = deferred();
  compose(h);
  h.context.api = () => gate.promise;
  const compressing = h.run("recompressMemory()");
  compose(h, "B");
  h.run('syncMemoryField("content", "B 的新草稿");');
  const draft = h.run("state.memory.draft");
  gate.resolve({ content: "A 的压缩结果", segments: [] });
  await compressing;
  assert.equal(h.run("state.memory.draft"), draft, "A 的旧请求不能替换 B 的草稿对象");
  assert.equal(h.run("state.memory.draft.content"), "B 的新草稿");
});

test("late list failures do not add errors or redraw after navigation", async () => {
  const h = harness(), gate = deferred();
  h.context.api = () => gate.promise;
  const loading = h.run("openMemoryView()");
  await click(h, "show-patrol");
  h.run('globalThis.redraws = 0; render = renderMemory = () => { redraws++; };');
  gate.reject(new Error("old list failure"));
  await loading;
  assert.equal(h.run("state.memory.error"), null);
  assert.equal(h.run("state.memory.loading"), false);
  assert.equal(h.run("redraws"), 0);
});

test("a list refresh preserves a selection made while the request was pending", async () => {
  const h = harness(), gate = deferred();
  h.run('state.view = "memory"; state.memory.selectedId = "A";');
  h.context.api = () => gate.promise;
  const loading = h.run("hydrateMemory()");
  h.context.button = { dataset: { action: "select-memory", memoryId: "B" } };
  await h.run('handleDocumentClick({ target: { closest: selector => selector === "[data-action]" ? button : null } })');
  gate.resolve({ memories: [{ memory_id: "A" }] });
  await loading;
  assert.equal(h.run("state.memory.selectedId"), "B");
});

test("reentering Memory only accepts the newest list and preserves its editor", async () => {
  const h = harness(), old = deferred(), current = deferred();
  const pending = [old, current];
  h.context.api = () => pending.shift().promise;
  const first = h.run("openMemoryView()");
  await click(h, "show-patrol");
  const second = h.run("openMemoryView()");
  compose(h, "B");
  const draft = h.run("state.memory.draft");
  current.resolve({ memories: [{ memory_id: "new" }] });
  await second;
  old.resolve({ memories: [{ memory_id: "old" }] });
  await first;
  assert.equal(h.run("state.memory.memories[0].memory_id"), "new");
  assert.equal(h.run("state.memory.draft"), draft);
});

for (const [name, edit] of [
  ["title", 'syncMemoryField("title", "new title")'],
  ["content", 'syncMemoryField("content", "new content")'],
  ["mode", 'syncMemoryField("contentMode", "segmented")'],
  ["segment", 'editMemorySegment(0, "body", "new segment")'],
  ["source text", 'editSourceText(0, "new source")'],
  ["selection", 'state.memory.selectedId = "B"'],
  ["category", 'state.memory.category = "evolution_strategy"'],
  ["cancel", 'await handleDocumentClick({ target: { closest: selector => selector === "[data-action]" ? { dataset: { action: "cancel-compose" } } : null } })'],
  ["navigation", 'await handleDocumentClick({ target: { closest: selector => selector === "[data-action]" ? { dataset: { action: "show-patrol" }, hasAttribute: () => false } : null } })'],
]) {
  test(`compression success and failure cannot affect a later ${name} change`, async () => {
    for (const fail of [false, true]) {
      const h = harness(), gate = deferred();
      compose(h);
      h.run('state.memory.draft.segments = [{ title: "part", body: "original" }];');
      h.context.api = () => gate.promise;
      const compressing = h.run("recompressMemory()");
      await h.run(`(async () => { ${edit}; })()`);
      const draft = h.run("state.memory.draft"), view = h.run("state.view");
      h.run('globalThis.redraws = 0; render = renderMemory = () => { redraws++; };');
      if (fail) gate.reject(new Error("stale compression failure"));
      else gate.resolve({ content: "stale summary", segments: [] });
      await compressing;
      assert.equal(h.run("state.memory.draft"), draft);
      assert.equal(h.run("state.view"), view);
      assert.equal(h.run("redraws"), 0);
      assert.equal(h.statusState.text, "");
    }
  });
}

test("the latest compression owns the response even before it finishes", async () => {
  const h = harness(), old = deferred(), current = deferred();
  compose(h);
  const pending = [old, current];
  h.context.api = () => pending.shift().promise;
  const draft = h.run("state.memory.draft");
  const first = h.run("recompressMemory()"), second = h.run("recompressMemory()");
  old.resolve({ content: "superseded summary" });
  await first;
  assert.equal(h.run("state.memory.draft"), draft);
  current.reject(new Error("current compression failure"));
  await second;
  assert.equal(h.statusState.text, "current compression failure");
  assert.equal(h.run("state.memory.draft"), draft);
});

test("an older compression cannot overwrite the newer completed summary", async () => {
  const h = harness(), old = deferred(), current = deferred();
  compose(h);
  const pending = [old, current];
  h.context.api = () => pending.shift().promise;
  const first = h.run("recompressMemory()"), second = h.run("recompressMemory()");
  current.resolve({ content: "new summary" });
  await second;
  old.resolve({ content: "old summary" });
  await first;
  assert.equal(h.run("state.memory.draft.content"), "new summary");
});

test("an unchanged editor accepts complete and segmented compression responses", async () => {
  for (const mode of ["complete", "segmented"]) {
    const h = harness(), gate = deferred(), calls = [];
    compose(h);
    h.run(`syncMemoryField("contentMode", "${mode}");`);
    h.context.api = (path, options) => { calls.push({ path, body: JSON.parse(options.body) }); return gate.promise; };
    const compressing = h.run("recompressMemory()");
    const segments = [{ title: "part", body: "summary" }];
    gate.resolve({ content: "summary", segments });
    await compressing;
    assert.equal(h.run("state.memory.draft.content"), "summary");
    assert.equal(h.run("state.memory.draft.contentMode"), mode);
    assert.equal(h.run("state.memory.draft.contentDirty"), false);
    assert.equal(h.run("state.memory.draft.segments"), segments);
    assert.deepEqual(calls, [{ path: "/desktop/api/memory/summarize", body: {
      selection: { sources: [{ type: "session", context_id: "context-A" }] }, mode,
    } }]);
  }
});
