/* 通过真实 app.js 和可控 Promise 验证历史 Revision 的请求与页面生命周期隔离。
 * 运行：node --test desktop/src/renderer/features/loop/loop-revision-request.test.cjs。
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
  const requests = [], failures = [];
  const h = createAppHarness({ globals: {
    FocusLoopApi: { create: () => ({ revision(id) {
      const gate = deferred();
      requests.push({ id, ...gate });
      return gate.promise;
    }, get: async () => ({ loop_id: "loop-a" }), related: async () => ({}) }) },
    FocusLoopStore: { create: () => ({ load() {}, reconcile() {}, reconcileRelated() {}, fail: error => failures.push(error) }) },
  } });
  h.vm.runInContext(readAppSource(), h.context);
  const run = code => h.vm.runInContext(code, h.context);
  run(`state.view = "loop"; state.activeTaskId = "context-a"; state.loop.loopId = "loop-a";
    state.tasks = [{ task_id: "context-a" }];
    localStorage.setItem("focus-agent-loop:context-a", "loop-a");
    api = async () => ({});
    workspacePatrolIntake = FocusWorkspacePatrolIntake.create({ runtime, api: loopApi });
    globalThis.redraws = 0;
    renderLoop = () => { redraws++; app.innerHTML = "Loop HTML"; };
    render = () => { app.innerHTML = state.view; };`);
  const click = action => {
    h.context.button = { dataset: { action }, hasAttribute: () => false };
    return run('handleDocumentClick({ target: { closest: selector => selector === "[data-action]" ? button : null } })');
  };
  return { ...h, run, click, requests, failures };
}

for (const newerFirst of [false, true]) {
  test(`latest Revision wins when ${newerFirst ? "newer" : "older"} response arrives first`, async () => {
    const h = harness();
    const first = h.run('openLoopRevision("revision-a")');
    const second = h.run('openLoopRevision("revision-b")');
    assert.deepEqual(h.requests.map(request => request.id), ["revision-a", "revision-b"]);
    if (newerFirst) {
      h.requests[1].resolve({ revision_id: "revision-b" });
      await second;
    }
    const redraws = h.run("redraws");
    h.requests[0].resolve({ revision_id: "revision-a" });
    await first;
    assert.equal(h.run("redraws"), redraws, "过期响应不能调用 renderLoop");
    if (!newerFirst) {
      assert.equal(h.run("state.loop.revisionSnapshot"), null);
      h.requests[1].resolve({ revision_id: "revision-b" });
      await second;
    }
    assert.equal(h.run("state.loop.revisionSnapshot.revision_id"), "revision-b");
    assert.equal(h.run("redraws"), 1);
  });
}

for (const [name, change] of [
  ["navigation away", h => h.click("show-patrol")],
  ["leaving and reopening the same Loop", async h => { await h.click("show-patrol"); await h.run("openLoopView()"); }],
  ["reopening the Loop view", h => h.run("openLoopView()")],
  ["Loop switch", h => h.run('state.loop.loopId = "loop-b"')],
  ["Context switch", h => h.run('state.activeTaskId = "context-b"')],
  ["Loop exit", h => h.run("exitAgentLoop()")],
  ["snapshot close", h => h.click("close-loop-revision")],
]) {
  for (const fail of [false, true]) {
    test(`late Revision ${fail ? "failure" : "success"} is ignored after ${name}`, async () => {
      const h = harness();
      const loading = h.run('openLoopRevision("revision-a")');
      await change(h);
      if (name.includes("reopening")) assert.equal(h.run("state.loop.loopId"), "loop-a");
      const redraws = h.run("redraws"), html = h.run("app.innerHTML");
      if (fail) h.requests[0].reject(new Error("stale revision failure"));
      else h.requests[0].resolve({ revision_id: "revision-a" });
      await loading;
      assert.equal(h.run("redraws"), redraws);
      assert.equal(h.run("app.innerHTML"), html);
      assert.equal(h.run("state.loop.revisionSnapshot"), null);
      assert.equal(h.failures.length, 0);
    });
  }
}

test("a superseded Revision failure cannot affect the current request", async () => {
  const h = harness();
  const first = h.run('openLoopRevision("revision-a")');
  const second = h.run('openLoopRevision("revision-b")');
  h.requests[0].reject(new Error("stale failure"));
  await first;
  assert.equal(h.failures.length, 0);
  assert.equal(h.run("redraws"), 0);
  const error = new Error("current failure");
  h.requests[1].reject(error);
  await second;
  assert.deepEqual(h.failures, [error]);
  assert.equal(h.run("redraws"), 1);
});

test("repeated requests for the same Revision still accept only the latest response", async () => {
  const h = harness();
  const first = h.run('openLoopRevision("revision-a")');
  const second = h.run('openLoopRevision("revision-a")');
  h.requests[0].resolve({ revision_id: "revision-a", content: "stale" });
  await first;
  assert.equal(h.run("state.loop.revisionSnapshot"), null);
  assert.equal(h.run("redraws"), 0);
  h.requests[1].resolve({ revision_id: "revision-a", content: "current" });
  await second;
  assert.equal(h.run("state.loop.revisionSnapshot.content"), "current");
  assert.equal(h.run("redraws"), 1);
});

test("Revision actions outside an active Loop do not send requests", async () => {
  const h = harness();
  h.run('state.view = "patrol"');
  await h.run('openLoopRevision("revision-a")');
  h.run('state.view = "loop"; state.loop.loopId = null');
  await h.run('openLoopRevision("revision-a")');
  assert.equal(h.requests.length, 0);
  assert.equal(h.run("redraws"), 0);
});
