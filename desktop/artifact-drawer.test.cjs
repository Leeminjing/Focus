"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { createDocument, createElement } = require("./test-dom.cjs");
const { groupFiles } = require("./artifact-drawer.js");
const fact = (id, path = "reports/报告.pdf", slot = "slot", time = 1) => ({ fact_id: id, revision: 1, context_id: "context", occurred_at: new Date(time * 1000).toISOString(),
  file: { path, slot_id: slot, result_key: id, operation: "modified", sha256: id } });

test("files group by location, latest write wins over verification time, deletions hide old files", () => {
  const old = { ...fact("old"), updated_at: "2099-01-01", revision: 5 };
  const newest = fact("new", undefined, undefined, 2);
  const other = fact("other", "other/报告.pdf"), isolated = fact("isolated", undefined, "isolated");
  const deleted = { ...fact("deleted", "gone.txt", "slot", 3), file: { ...fact("deleted", "gone.txt").file, operation: "deleted" } };
  const groups = groupFiles([newest, old, other, isolated, fact("gone", "gone.txt"), deleted, { kind: "artifact", title: "legacy" }], "报告");
  assert.equal(groups.length, 3);
  assert.deepEqual(groups[0].map(row => row.fact_id), ["new", "old"]);
  assert.equal(groupFiles([old, newest], "reports").length, 0, "search matches filenames only");
});

test("drawer searches all pages, previews history and cancels stale workspace reads", async () => {
  const host = createElement("main"); host.rootMarker = true;
  let id = "a", release, delayed = false;
  const calls = [], exits = [], sources = [];
  const api = { facts: async (loop, options) => {
    calls.push([loop, options.before]);
    if (delayed) return new Promise(resolve => { release = resolve; });
    return options.before == null ? { facts: [fact("new", undefined, undefined, 2)], has_more: true, next_before: 1 }
      : { facts: [fact("old"), fact("older-report", "archive/summary.txt")], has_more: false };
  }, factDetail: async (_loop, identity) => ({ file: { ...fact(identity).file, result_key: identity } }) };
  const context = vm.createContext({ document: createDocument(), AbortController,
    FocusLoopFactsView: { renderFile: file => `<pre>${file.result_key}</pre>` } });
  vm.runInContext(fs.readFileSync(require.resolve("./artifact-drawer.js"), "utf8"), context);
  const drawer = context.FocusArtifactDrawer.create({ host, api, getLoopId: () => id, bridge: { saveAs: async ref => { exits.push(ref); return { ok: true }; } }, onSource: source => sources.push(source) });
  await drawer.open();
  assert.deepEqual(calls, [["a", undefined], ["a", 1]]);
  assert.equal(host.querySelectorAll(".artifact-row").length, 2);
  const search = host.querySelector("[data-files-search]"); search.value = "summary";
  host.querySelector(".artifact-drawer").dispatchEvent({ type: "input", target: search });
  assert.equal(host.querySelectorAll(".artifact-row").length, 1);
  assert.match(host.querySelector("[data-files-list]").textContent, /summary.txt/);
  const click = async selector => {
    const target = host.querySelector(selector);
    host.querySelector(".artifact-drawer").dispatchEvent({ type: "click", target, stopPropagation() {} });
    await new Promise(resolve => setImmediate(resolve));
  };
  await click('[data-files-native="saveAs"]');
  assert.equal(exits[0].factId, "older-report");
  await click("[data-files-source]"); assert.deepEqual(sources, ["context"]);
  search.value = "报告"; host.querySelector(".artifact-drawer").dispatchEvent({ type: "input", target: search });
  await click('.artifact-history [data-files-view]');
  assert.match(host.querySelector("[data-files-preview]").textContent, /old/);
  assert.equal(host.querySelectorAll(".artifact-history [data-files-native]").length, 0);
  delayed = true;
  const pending = drawer.refresh();
  id = "b"; drawer.reset();
  release({ facts: [fact("stale")], has_more: false }); await pending;
  assert.equal(drawer.isOpen(), false);
  assert.doesNotMatch(host.textContent, /stale/);
  drawer.dispose();
});
