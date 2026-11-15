/* 运行真实渲染器与 app 事件，区分分类为空、搜索零匹配和读取失败，并保护底层内容。 */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const Memory = require("./memory.js");
const { createElement } = require("../../../../tests/helpers/test-dom.cjs");
const { createAppHarness, createNode, readAppSource } = require("../../../../tests/helpers/test-helper.cjs");

const note = { memory_id: "note", title: "决定", content: "保留原始材料", source_kind: "manual" };
const strategy = { memory_id: "strategy", category: "evolution_strategy", title: "验证方法", enabled: true,
  strategy: { applicability: "存在假设", evolution: "先验证再实施", inheritance: "保留证据" } };

function view(memories, options) {
  const root = createElement("div");
  root.innerHTML = Memory.render(memories, options);
  return root;
}

for (const [category, items, title, action] of [
  ["", [], "还没有任何记忆", "new-memory"],
  ["memory", [strategy], "还没有任何记忆", "new-memory"],
  ["evolution_strategy", [note], "还没有工作策略", "new-strategy"],
]) test(`${category || "all"}: an empty category offers creation instead of search advice`, () => {
  for (const query of ["", "不存在"]) {
    const empty = view(items, { category, query }).querySelector(".memory-empty");
    assert.equal(empty.querySelector("h2").textContent, title);
    assert.ok(empty.querySelector(`[data-action="${action}"]`));
    assert.doesNotMatch(empty.textContent, /没有匹配|换一个关键词/);
  }
});

for (const category of ["", "memory", "evolution_strategy"]) {
  test(`${category || "all"}: a search miss offers clearing instead of creation or import`, () => {
    const root = view([note, strategy], { category, query: "不存在" });
    const empty = root.querySelector(".memory-empty");
    assert.match(empty.textContent, /没有匹配/);
    assert.ok(empty.querySelector('[data-action="clear-memory-search"]'));
    assert.equal(empty.querySelector('[data-action="new-memory"], [data-action="new-strategy"], [data-action="import-strategy"]'), null);
    assert.equal(root.querySelector(".memory-page-heading p"), null);
  });
}

test("loading and errors cannot appear as an empty category or an unsuccessful search", () => {
  for (const category of ["", "memory", "evolution_strategy"]) {
    for (const memories of [[], [note], [strategy]]) {
      for (const query of ["", "不存在"]) {
        for (const status of [{ loading: true }, { error: "读取中断 <offline>" }]) {
          const root = view(memories, { category, query, ...status });
          assert.ok(root.querySelector(status.error ? '[role="alert"]' : '[role="status"]'));
          assert.equal(root.querySelector(".memory-empty"), null);
          if (status.error) assert.ok(root.querySelector('[data-action="refresh-memory"]'));
        }
      }
    }
  }
});

test("a pending or failed refresh keeps cached matching entries visible", () => {
  for (const status of [{ loading: true }, { error: "读取失败" }]) {
    const root = view([note, strategy], { category: "memory", query: "原始", ...status });
    assert.equal(root.querySelectorAll(".memory-card").length, 1);
    assert.equal(root.querySelector(".memory-empty"), null);
  }
});

test("typing and clearing search only change the query and restore the current category", async () => {
  const memories = Object.freeze([Object.freeze(structuredClone(note)), Object.freeze(structuredClone(strategy))]);
  const original = JSON.stringify(memories);
  const searchInput = createNode();
  const h = createAppHarness({ globals: { memories }, selectors: { "#app": createNode({ querySelector: () => searchInput }) } });
  h.vm.runInContext(readAppSource(), h.context);
  const run = code => h.vm.runInContext(code, h.context);
  run('state.view = "memory"; state.memory.memories = memories; state.memory.category = "evolution_strategy"; state.memory.selectedId = "strategy"; state.memory.layout = "list"; globalThis.redraws = 0; renderMemory = () => { redraws++; }; api = () => { throw new Error("search must not call API"); };');
  h.dispatch("input", { target: { dataset: { pageSearch: "memory" }, value: "不存在", selectionStart: 3 } });
  assert.equal(run("state.memory.query"), "不存在");
  assert.ok(view(memories, { category: run("state.memory.category"), query: run("state.memory.query") }).querySelector('[data-action="clear-memory-search"]'));
  h.context.button = { dataset: { action: "clear-memory-search" } };
  await run('handleDocumentClick({ target: { closest: selector => selector === "[data-action]" ? button : null } })');
  assert.equal(run("state.memory.query"), "");
  assert.equal(run("state.memory.category"), "evolution_strategy");
  assert.equal(run("state.memory.selectedId"), "strategy");
  assert.equal(run("state.memory.layout"), "list");
  assert.equal(run("state.memory.memories"), memories);
  assert.equal(JSON.stringify(memories), original);
  assert.equal(run("redraws"), 2);
  const root = view(memories, { category: run("state.memory.category"), query: run("state.memory.query") });
  assert.equal(root.querySelectorAll(".memory-card").length, 1);
  assert.equal(root.querySelector(".memory-card").dataset.memoryId, "strategy");
});
