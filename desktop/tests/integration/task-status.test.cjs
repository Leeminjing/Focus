"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const vm = require("node:vm");
const { createAppHarness, readAppSource } = require("../helpers/test-helper.cjs");

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

test("retired task Loop notification falls back to task recovery", async () => {
  const run = harness();
  await run(`
    globalThis.openedTasks = [];
    refreshTasks = async () => { state.tasks = [{task_id: "target", loop: {loop_id: "loop"}}]; };
    switchTask = async id => { openedTasks.push(id); state.activeTaskId = id; state.view = "focus"; };
    render = () => {};
    openTaskNotification({taskId: "target", loopId: "loop", attentionId: "wait"});
  `);
  assert.equal(run("openedTasks[0]"), "target");
  assert.equal(run("state.view"), "focus");
  assert.equal(run("state.inspector.tab"), "run");
  assert.equal(run('localStorage.getItem("focus-agent-loop:target")'), null);
});

test("Patrol notification opens its workspace and wait instead of the Context loop", async () => {
  const run = harness();
  await run(`
    globalThis.openedPatrols = [];
    refreshTasks = async () => { state.tasks = [{task_id: "root", workspace_id: "workspace", loop: {
      loop_id: "patrol", interaction_mode: "workspace_patrol", workspace_id: "workspace", initial_context_id: "root"
    }}]; };
    switchTask = async () => { throw new Error("Patrol notification should open the workspace"); };
    openPatrolWorkbench = async target => { openedPatrols.push(target); };
    openTaskNotification({taskId: "root", loopId: "patrol", workspaceId: "workspace", attentionId: "budget"});
  `);
  assert.deepEqual(JSON.parse(run("JSON.stringify(openedPatrols[0])")), {
    workspaceId: "workspace", loopId: "patrol", attentionId: "budget",
  });
});

test("task Patrol details save task state and use the task workspace", async () => {
  const run = harness();
  await run(`
    state.view = "focus"; state.activeTaskId = "target";
    state.tasks = [{task_id: "target", workspace_id: "task-workspace"}];
    state.patrolWorkspace = {workspace_id: "other-workspace"};
    globalThis.navigation = [];
    persistFocusState = () => navigation.push("saved");
    openPatrolWorkbench = async target => navigation.push(target);
    handleDocumentClick({target: {closest: selector => selector === "[data-action]" ? {dataset: {action: "show-task-patrol"}} : null}});
  `);
  assert.deepEqual(JSON.parse(run("JSON.stringify(navigation)")), ["saved", {workspaceId: "task-workspace"}]);
});
