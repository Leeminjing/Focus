/*
 * 用真实 app/Loop Store/Console 控制器与可控响应顺序验证导航竞态。
 * 旧 Context 的发现、激活、快照、关联与控制台响应不得覆盖新页面或重启 Live。
 * 示例：node --test desktop/src/renderer/app/app-loop-navigation.test.cjs。
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const { createAppHarness, readAppSource } = require("../../../tests/helpers/test-helper.cjs");

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function setup(stage, { patrol = false } = {}) {
  const entered = deferred();
  const response = deferred();
  const connections = [];
  let waiting = true;
  const snapshot = loopId => ({ loop_id: loopId, workspace_id: loopId, interaction_mode: patrol && loopId === "loop-a" ? "workspace_patrol" : "context" });
  const methods = {
    findByContext: taskId => stage === "activationEligibility" && taskId === "task-a" ? null : snapshot(taskId.replace("task", "loop")),
    activationEligibility: () => ({ eligible: true }),
    get: snapshot,
    related: value => ({ loop_id: value.loop_id }),
    console: loopId => ({ loop_id: loopId, initial_context_id: "shared-context", nodes: [{ context_id: "shared-context" }] }),
    conversation: loopId => ({ loop_id: loopId, context_id: "shared-context", messages: [] }),
    facts: loopId => ({ loop_id: loopId, items: [] }),
  };
  const api = Object.fromEntries(Object.entries(methods).map(([name, result]) => [name, async (...args) => {
    const value = result(...args);
    if (waiting && name === stage) {
      waiting = false;
      entered.resolve();
      await response.promise;
    }
    return value;
  }]));
  const harness = createAppHarness({ globals: {
    AbortController,
    FocusLoopApi: { create: () => api },
    FocusLoopLiveConnection: { create: () => ({ stop() {}, async start(id) { connections.push(id); } }) },
    // 连接只记录是否启动；投影状态使用真实 Loop/Console Store。
    FocusLoopLiveStore: { create: () => ({ subscribe() {}, reset() {} }) },
  } });
  for (const module of ["loop-store", "loop-console-store", "loop-console-controller"]) {
    harness.vm.runInContext(fs.readFileSync(require.resolve(`../features/loop/${module}.js`), "utf8"), harness.context);
  }
  harness.vm.runInContext(readAppSource(), harness.context);
  harness.vm.runInContext(`
    state.tasks = [
      { task_id: 'task-a', workspace_id: 'workspace-a', title: 'A' },
      { task_id: 'task-b', workspace_id: 'workspace-b', title: 'B' },
    ];
    state.activeTaskId = 'task-a';
    state.view = 'focus';
    render = () => {};
    renderLoop = () => {};
    persistFocusState = () => {};
    hydrateActive = async () => {};
    readLoopNavigation = () => JSON.stringify({
      task: state.activeTaskId, view: state.view, loop: state.loop,
      snapshot: loopStore.get(), console: loopConsoleStore.get(),
      workspace: state.patrolWorkspace, details: [...state.details],
      storedA: localStorage.getItem('focus-agent-loop:task-a'),
      storedB: localStorage.getItem('focus-agent-loop:task-b'),
    });
  `, harness.context);
  return { ...harness, entered, response, connections };
}

for (const stage of ["findByContext", "activationEligibility", "get", "related", "console", "conversation", "facts"]) {
  for (const failure of [false, true]) {
    test(`迟到 ${stage} ${failure ? "错误" : "响应"} 不覆盖 Context B`, { timeout: 3000 }, async () => {
      const harness = setup(stage);
      const first = harness.context.openLoopView();
      await harness.entered.promise;
      await harness.context.switchTask("task-b");
      await harness.context.openLoopView();
      const before = harness.context.readLoopNavigation();
      const connections = [...harness.connections];
      if (failure) harness.response.reject(new Error("stale A failure"));
      else harness.response.resolve();
      await first;
      assert.equal(harness.context.readLoopNavigation(), before);
      assert.deepEqual(harness.connections, connections);
    });
  }
}

test("旧 workspace_patrol 快照不能把离开的用户切回 Patrol", async () => {
  const harness = setup("get", { patrol: true });
  const first = harness.context.openLoopView();
  await harness.entered.promise;
  harness.vm.runInContext("cancelPendingViewRequests(); state.view = 'map';", harness.context);
  const before = harness.context.readLoopNavigation();
  harness.response.resolve();
  await first;
  assert.equal(harness.context.readLoopNavigation(), before);
});

test("同一 Context/Loop 重新打开时旧 finally 不得清除新请求的 loading", async () => {
  const harness = setup("get");
  const first = harness.context.openLoopView();
  await harness.entered.promise;
  const entered = deferred();
  const response = deferred();
  harness.context.FocusLoopApi.create().get = async loopId => {
    entered.resolve();
    await response.promise;
    return { loop_id: loopId };
  };
  harness.vm.runInContext("cancelPendingViewRequests(); state.view = 'map';", harness.context);
  const second = harness.context.openLoopView();
  await entered.promise;
  const before = harness.context.readLoopNavigation();
  harness.response.resolve();
  await first;
  assert.equal(harness.context.readLoopNavigation(), before);
  assert.equal(harness.vm.runInContext("state.loop.loading", harness.context), true);
  response.resolve();
  await second;
  assert.equal(harness.vm.runInContext("state.loop.loading", harness.context), false);
});

test("当前 Loop 正常加载快照、控制台并启动一次连接", async () => {
  const harness = setup("get");
  const loading = harness.context.openLoopView();
  await harness.entered.promise;
  harness.response.resolve();
  await loading;
  const current = JSON.parse(harness.context.readLoopNavigation());
  assert.equal(current.view, "loop");
  assert.equal(current.loop.loading, false);
  assert.equal(current.snapshot.snapshot.loop_id, "loop-a");
  assert.equal(current.console.conversation.loop_id, "loop-a");
  assert.equal(current.console.facts.loop_id, "loop-a");
  assert.equal(current.console.pending, null);
  assert.deepEqual(harness.connections, ["loop-a"]);
});

test("同一 Context 的 Loop 身份改变后旧快照不能写入", async () => {
  const harness = setup("get");
  const loading = harness.context.openLoopView();
  await harness.entered.promise;
  harness.vm.runInContext("state.loop.loopId = 'successor-loop';", harness.context);
  const before = harness.context.readLoopNavigation();
  harness.response.resolve();
  await loading;
  assert.equal(harness.context.readLoopNavigation(), before);
  assert.deepEqual(harness.connections, []);
});

test("发现请求尚未返回时退出 Loop 不得被旧结果重新激活", async () => {
  const harness = setup("findByContext");
  const loading = harness.context.openLoopView();
  await harness.entered.promise;
  harness.vm.runInContext("api = async () => ({ task_id: 'task-a' });", harness.context);
  await harness.context.exitAgentLoop();
  const before = harness.context.readLoopNavigation();
  harness.response.resolve();
  await loading;
  assert.equal(harness.context.readLoopNavigation(), before);
  assert.equal(JSON.parse(before).loop.loading, false);
  assert.deepEqual(harness.connections, []);
});

test("当前 workspace_patrol 快照仍能进入 Patrol 并结束 loading", async () => {
  const harness = setup("get", { patrol: true });
  const loading = harness.context.openLoopView();
  await harness.entered.promise;
  harness.response.resolve();
  await loading;
  const current = JSON.parse(harness.context.readLoopNavigation());
  assert.equal(current.view, "patrol");
  assert.equal(current.workspace.workspace_id, "loop-a");
  assert.equal(current.loop.loading, false);
  assert.deepEqual(harness.connections, []);
});

test("当前请求失败仍显示错误并结束 loading", async () => {
  const harness = setup("get");
  const loading = harness.context.openLoopView();
  await harness.entered.promise;
  harness.response.reject(new Error("current failure"));
  await loading;
  const current = JSON.parse(harness.context.readLoopNavigation());
  assert.equal(current.snapshot.error, "current failure");
  assert.equal(current.loop.loading, false);
  assert.deepEqual(harness.connections, []);
});

test("没有 Loop 时当前 Context 仍获取激活资格", async () => {
  const harness = setup("activationEligibility");
  const loading = harness.context.openLoopView();
  await harness.entered.promise;
  harness.response.resolve();
  await loading;
  const current = JSON.parse(harness.context.readLoopNavigation());
  assert.equal(current.loop.loopId, null);
  assert.equal(current.loop.loading, false);
  assert.deepEqual(current.details, [["task-a", { loop_activation: { eligible: true } }]]);
  assert.deepEqual(harness.connections, []);
});
