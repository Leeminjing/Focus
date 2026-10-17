/* 验证生产 Patrol 控制器在历史失败/挂起时仍连接当前 Loop，并隔离历史重试和旧工作区响应。
 * 运行：node --test desktop/workspace-patrol-loading.test.cjs。
 */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createElement } = require("./test-dom.cjs");
const Inputs = require("./workspace-patrol-inputs.js");
const View = require("./workspace-patrol-view.js");
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

function fixture(api) {
  const host = createElement("main"), starts = [], histories = [];
  const inputs = Inputs.create();
  let listener;
  const state = { projection: null, connection: { status: "live" } };
  const context = vm.createContext({ AbortController, document: { body: createElement("body"), createElement }, addEventListener() {},
    FocusLoopWaitRequestView: { createDraftStore: () => new Map() },
    FocusArtifactDrawer: { create: () => ({ update() {}, dispose() {} }) },
    FocusPatrolInputReferences: { create: () => ({ bind() {}, dispose() {} }) },
    FocusWorkspacePatrolBudget: { create: () => ({ syncDraft() {}, update() {}, dispose() {} }) },
    FocusWorkspacePatrolMaterialPreview: { create: () => ({ render() {}, dispose() {} }) },
    FocusContextInspector: { create: () => ({ mount() {}, dispose() {} }) },
    FocusSurfaceTransition: { visible: async (node, show) => { node.hidden = !show; }, finish() {} },
    FocusObservationView: { rounds: () => "round" },
    FocusLoopFactsView: { markFileReferences: () => [], reconcileTimeline() {} },
    FocusLoopLiveSelectors: { selectFacts: () => [] },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "workspace-patrol-controller.js"), "utf8"), context);
  const controller = context.FocusWorkspacePatrolController.create({ api, inputs, view: View,
    intake: { reconcile: async () => {}, history(id, page, ...args) { histories.push([id, page]); inputs.history(page, ...args); } },
    liveStore: { get: () => state, subscribe: fn => { listener = fn; return () => {}; } },
    connection: { start: async id => { starts.push(id); }, stop() {} } });
  const click = selector => host.dispatchEvent({ type: "click", target: host.querySelector(selector), stopPropagation() {} });
  return { controller, host, starts, histories, inputs, click,
    mount(id) { const mounted = controller.mount(host, { workspace_id: id, display_name: id }); host.querySelector("[data-patrol-dialog]").close = () => {}; return mounted; },
    emit(patch = {}) { state.projection = { loop_id: "loop-a", loop: { state: { status: "running" } }, wait_requests: {}, interventions: {}, facts: {}, contexts: {}, ...patch }; listener(state); } };
}

test("history 503 leaves current Loop connected and retries only history", async () => {
  let loopReads = 0, historyReads = 0;
  const f = fixture({ workspacePatrol: async () => { loopReads++; return { loop_id: "loop-a" }; },
    workspaceInputs: async () => { if (++historyReads === 1) throw new Error("HTTP 503"); return { items: [], next_before: null }; } });
  await f.mount("a");
  assert.deepEqual(f.starts, ["loop-a"], "历史 503 不能阻止 Live 连接");
  assert.match(f.host.querySelector("[data-patrol-history-status]").textContent, /503/);
  assert.equal(f.host.querySelector("[data-patrol-error]").textContent, "");
  f.emit({ task_progress: { state: { document: { items: [{ description: "已提交工作", state: "completed", support: "supported" }] } } },
    wait_requests: { question: { entity_id: "question", revision: 1, state: { status: "open", prompt: "需要确认范围", scope: { information_only: true } } } } });
  assert.equal(f.host.querySelector('[data-patrol-control="pause"]').disabled, false);
  assert.match(f.host.querySelector("[data-patrol-state]").textContent, /推进中/);
  assert.match(f.host.querySelector("[data-patrol-progress]").textContent, /已提交工作/);
  assert.equal(f.host.querySelector("[data-patrol-pending]").hidden, false);
  assert.match(f.host.querySelector("[data-patrol-requests]").textContent, /需要确认范围/);
  f.click("[data-patrol-history-open]"); await tick();
  f.click("[data-patrol-history-retry]"); await tick();
  assert.equal(historyReads, 2);
  assert.equal(loopReads, 1);
  assert.deepEqual(f.starts, ["loop-a"]);
  assert.equal(f.host.querySelector("[data-patrol-history-status]").hidden, true);
  f.controller.dispose();
});

test("Loop read failure leaves successful history available and does not masquerade as a history failure", async () => {
  const f = fixture({ workspacePatrol: async () => { throw new Error("Loop unavailable"); }, workspaceInputs: async () => ({ items: [{ submission_id: "one", content: "saved input" }] }) });
  await f.mount("a");
  assert.match(f.host.querySelector("[data-patrol-error]").textContent, /Loop unavailable/);
  assert.equal(f.host.querySelector("[data-patrol-history-status]").hidden, true);
  assert.match(f.host.querySelector("[data-patrol-history]").textContent, /saved input/);
  f.controller.dispose();
});

test("late Loop and history responses cannot update another workspace or a disposed controller", async () => {
  for (const reject of [false, true]) {
    const loop = deferred(), history = deferred();
    const f = fixture({ workspacePatrol: id => id === "a" ? loop.promise : Promise.resolve({ loop_id: "loop-b" }),
      workspaceInputs: id => id === "a" ? history.promise : Promise.resolve({ items: [] }) });
    const old = f.mount("a");
    await f.mount("b");
    if (reject) { loop.reject(new Error("old Loop error")); history.reject(new Error("old history error")); }
    else { loop.resolve({ loop_id: "loop-a" }); history.resolve({ items: [{ submission_id: "old", content: "old input" }] }); }
    await old;
    assert.deepEqual(f.starts, ["loop-b"]);
    assert.equal(f.host.querySelector("[data-patrol-error]").textContent, "");
    assert.equal(f.host.querySelector("[data-patrol-history-status]").hidden, true);
    assert.equal(f.histories.some(([id]) => id === "a"), false);
    const pending = f.mount("a"); f.controller.dispose(); await pending;
    assert.deepEqual(f.starts, ["loop-b"]);
  }
});

test("Live-triggered history failure stays in history and retries the latest page even with an exhausted cursor", async () => {
  let reads = 0;
  const f = fixture({ workspacePatrol: async () => ({ loop_id: "loop-a" }), workspaceInputs: async (_id, before) => {
    assert.equal(Boolean(before), false);
    if (++reads === 2) throw new Error("history refresh 503");
    return { items: [], next_before: null };
  } });
  await f.mount("a"); f.emit(); f.emit({ interventions: { updated: {} } }); await tick();
  assert.equal(f.host.querySelector("[data-patrol-error]").textContent, "");
  assert.match(f.host.querySelector("[data-patrol-history-status]").textContent, /503/);
  f.click("[data-patrol-history-open]"); await tick();
  f.click("[data-patrol-history-retry]"); await tick();
  assert.equal(reads, 3);
  assert.equal(f.host.querySelector("[data-patrol-history-status]").hidden, true);
  assert.deepEqual(f.starts, ["loop-a"]);
  f.controller.dispose();
});

test("pending history does not delay Live connection", async () => {
  const pending = deferred();
  const f = fixture({ workspacePatrol: async () => ({ loop_id: "loop-a" }), workspaceInputs: () => pending.promise });
  const mounted = f.mount("a"); await tick();
  assert.deepEqual(f.starts, ["loop-a"]);
  pending.resolve({ items: [] }); await mounted;
  f.controller.dispose();
});

test("history updates arriving during initial loading refresh after the initial page without racing it", async () => {
  const pending = deferred(); let reads = 0;
  const f = fixture({ workspacePatrol: async () => ({ loop_id: "loop-a" }), workspaceInputs: () => {
    if (++reads === 1) return pending.promise;
    return Promise.resolve({ items: [{ submission_id: "one", content: "latest input" }], next_before: null });
  } });
  const mounted = f.mount("a"); await tick(); f.emit(); f.emit({ interventions: { updated: {} } });
  assert.equal(reads, 1, "初始历史未返回时合并刷新请求");
  pending.resolve({ items: [{ submission_id: "one", content: "old input" }], next_before: null });
  await mounted; await tick();
  assert.equal(reads, 2);
  assert.match(f.host.querySelector("[data-patrol-history]").textContent, /latest input/);
  f.controller.dispose();
});
