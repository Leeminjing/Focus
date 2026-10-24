const test = require("node:test");
const assert = require("node:assert/strict");
const { createElement, createDocument } = require("../../../../tests/helpers/test-dom.cjs");
const { create, reviewText } = require("./round-changes.js");
const diff = require("../loop/loop-facts-view.js");
const tick = () => new Promise(resolve => setImmediate(resolve));
const file = { path: 'src/<unsafe>.js', operation: "modified", added: 1, removed: 1, first_key: "first", last_key: "last" };
const snapshot = { before: { available: true, text: "old\n" }, after: { available: true, text: "new\n" } };
const overview = { run_id: "run", status: "success", files: [file] };
function setup(api, onReview = () => {}) {
  const host = createElement("div"); host.rootMarker = true;
  const controller = create({ api, diff, onReview, document: createDocument() });
  return { host, controller };
}

test("only metadata loads initially; expanding loads the pinned diff and retains its DOM on refresh", async () => {
  const calls = [];
  const { host, controller } = setup(async url => { calls.push(url); return url.endsWith("/changes") ? structuredClone(overview) : snapshot; });
  controller.mount(host, "task"); await tick();
  assert.equal(calls.length, 1);
  assert.match(host.innerHTML, /src\/&lt;unsafe&gt;.js/);
  const row = host.querySelector("[data-change-key]");
  row.open = true; row.dispatchEvent({ type: "toggle" }); await tick();
  assert.equal(calls[1], "/desktop/api/tasks/task/changes/run/first/last");
  assert.match(row.innerHTML, /is-added/);
  const body = row.querySelector("[data-change-body]");
  await controller.refresh();
  assert.equal(host.querySelector("[data-change-key]"), row);
  assert.equal(row.querySelector("[data-change-body]"), body);
  assert.equal(row.open, true);
});

test("late response from another task cannot overwrite the selected task", async () => {
  let release;
  const { host, controller } = setup(url => url.includes("/old/") ? new Promise(resolve => { release = resolve; }) : Promise.resolve({ ...overview, run_id: "new-run" }));
  controller.mount(host, "old");
  const next = createElement("div"); next.rootMarker = true;
  controller.mount(next, "new"); await tick();
  release({ ...overview, files: [{ ...file, path: "old-only" }] }); await tick();
  assert.doesNotMatch(next.innerHTML, /old-only/);
  assert.match(next.innerHTML, /src/);
});

test("errors allow retry, unavailable diffs retain honest counts, and review preserves pinned evidence", async () => {
  let fail = true, submitted;
  const { host, controller } = setup(async url => {
    if (url.endsWith("/changes")) { if (fail) throw Error("offline"); return overview; }
    return snapshot;
  }, text => { submitted = text; });
  controller.mount(host, "task"); await tick();
  assert.match(host.textContent, /offline/);
  fail = false; await controller.refresh();
  host.querySelector("[data-review-changes]").dispatchEvent({ type: "click" }); await tick();
  assert.match(submitted, /Run run/); assert.match(submitted, /first → last/);
  assert.match(submitted, /旧1\/新-/); assert.match(submitted, /new/);
  assert.match(host.textContent, /可补充意见后发送/);
  const text = reviewText({ ...overview, files: [{ ...file, added: null, removed: null, reason: "binary" }] }, [null], diff);
  assert.match(text, /行数未知/); assert.match(text, /二进制/);
});

test("a new active run clears old changes immediately and review failures never submit partial context", async () => {
  let failDetail = false, submitted = false;
  const { host, controller } = setup(async url => {
    if (url.endsWith("/changes")) return overview;
    if (failDetail) throw Error("missing snapshot");
    return snapshot;
  }, () => { submitted = true; });
  controller.mount(host, "task"); await tick();
  failDetail = true;
  host.querySelector("[data-review-changes]").dispatchEvent({ type: "click" }); await tick();
  assert.equal(submitted, false); assert.match(host.textContent, /missing snapshot/);
  const next = createElement("div"); next.rootMarker = true;
  controller.mount(next, "task", "next-run"); await tick();
  assert.doesNotMatch(next.innerHTML, /unsafe/);
});
