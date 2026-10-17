"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const vm = require("node:vm");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

function harness() {
  const h = createAppHarness();
  new vm.Script(readAppSource()).runInContext(h.context);
  return code => new vm.Script(code).runInContext(h.context);
}

test("sidebar shows background running task before any detail has loaded", () => {
  const run = harness();
  const html = run(`
    state.tasks = [{ task_id: "background", title: "后台任务", active_run: { status: "running" } }];
    renderTaskRail({ task_id: "foreground" });
  `);
  assert.match(html, /运行中/);
  assert.doesNotMatch(html, /就绪/);
});

test("overview distinguishes unstarted, completed, failed and waiting after restart", () => {
  const run = harness();
  for (const [summary, label] of [
    [{}, "未开始"],
    [{ latest_run: { status: "success" } }, "已完成"],
    [{ latest_run: { status: "error" } }, "运行失败"],
    [{ latest_run: { status: "interrupted" }, attention: [{ kind: "access_approval", id: "approval" }] }, "等待授权"],
  ]) {
    const html = run(`taskCardMarkup(${JSON.stringify({ task_id: "task", title: "任务", ...summary })})`);
    assert.match(html, new RegExp(label));
  }
});

test("list summary wins over stale detail cache and attention wins over running", () => {
  const run = harness();
  assert.match(run(`
    state.tasks = [{ task_id: "task", title: "任务", latest_run: {status: "success"} }];
    state.details.set("task", { active_run: {status: "running"} });
    renderTaskRail({ task_id: "task" });
  `), /已完成/);
  assert.match(run(`taskCardMarkup({task_id: "task", active_run: {status: "running"}, attention: [{id: "ask", kind: "access_approval"}]})`), /等待授权/);
});

test("notification click navigates to its task without relying on cached details", async () => {
  const run = harness();
  await run(`
    globalThis.openedTasks = [];
    refreshTasks = async () => { state.tasks = [{task_id: "target", lifecycle: "active"}]; };
    switchTask = async id => { openedTasks.push(id); };
    openTaskNotification({taskId: "target"});
  `);
  assert.equal(run("openedTasks[0]"), "target");
  await run(`openTaskNotification({taskId: "deleted"});`);
  assert.equal(run("openedTasks.length"), 1);
});

test("notification click discovers its loop from the server after restart", async () => {
  const run = harness();
  await run(`
    globalThis.openedLoops = [];
    refreshTasks = async () => { state.tasks = [{task_id: "target", loop: {loop_id: "loop"}}]; };
    switchTask = async id => { state.activeTaskId = id; };
    openLoopView = async () => { openedLoops.push(localStorage.getItem("focus-agent-loop:target")); };
    openTaskNotification({taskId: "target", loopId: "loop", attentionId: "wait"});
  `);
  assert.equal(run("openedLoops[0]"), "loop");
});
