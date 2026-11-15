/* Task identity and cross-workspace navigation use the production renderer and
 * the DOM harness. Responsive visibility is checked separately in a browser. */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { createAppHarness, readAppSource } = require("../helpers/test-helper.cjs");
const { createElement } = require("../helpers/test-dom.cjs");

function fixture() {
  const app = createElement("main");
  app.rootMarker = true;
  const inspector = createElement("aside");
  const shell = createElement("div");
  const h = createAppHarness({ selectors: {
    "#app": app, "#appInspector": inspector, ".app-shell": shell,
    "#conversation": () => app.querySelector("#conversation"),
  } });
  const query = h.document.querySelector;
  h.document.querySelector = selector => app.querySelector(selector)
    || (["#mainInput", ".context-rail-list"].includes(selector) ? null : query(selector));
  const evaluate = source => h.vm.runInContext(source, h.context);
  evaluate(readAppSource());
  evaluate(`
    state.view = "focus";
    state.tasks = [
      { task_id: "a", workspace_id: "ws-a", workspace_name: "同名工作区", title: "当前任务标题" },
      { task_id: "b", workspace_id: "ws-b", workspace_name: "同名工作区", title: "其他工作区任务" },
      { task_id: "c", workspace_id: "ws-a", workspace_name: "同名工作区", title: "同组任务" },
      { task_id: "archived", workspace_id: "ws-a", title: "归档任务", lifecycle: "archived" },
      { task_id: "deleted", workspace_id: "ws-a", title: "删除任务", lifecycle: "deleted" },
      { task_id: "assembly", title: "独立任务", harness_mode: "assembly" }
    ];
    state.activeTaskId = "a";
    for (const task of state.tasks) state.details.set(task.task_id, { messages: [], ui_state: {} });
    renderFocus();
  `);
  return { ...h, app, evaluate };
}

test("ordinary session has one main heading and no repeated composer identity", () => {
  const { app } = fixture();
  assert.equal(app.querySelectorAll("h1").length, 1);
  assert.equal(app.querySelector("h1").textContent, "当前任务标题");
  assert.equal(app.querySelector(".task-page-heading"), null);
  assert.equal(app.querySelector(".composer-context"), null);
  assert.equal(app.querySelector(".task-identity-meta code").textContent, "a");
  assert.ok(app.querySelector('.task-workspace-tools [data-inspector-tab="run"]'));
  assert.equal(app.querySelectorAll('[data-action="new-task"]').length, 1);
});

test("workspace identity groups active tasks without losing other workspaces", () => {
  const { app } = fixture();
  const groups = app.querySelectorAll(".task-workspace-group");
  assert.equal(groups.length, 2, "equal workspace names do not merge different IDs");
  assert.deepEqual(groups.map(group => group.getAttribute("data-workspace-id")), ["ws-a", "ws-b"]);
  assert.deepEqual(groups[0].querySelectorAll("button").map(node => node.dataset.taskId), ["a", "c"]);
  assert.deepEqual(groups[1].querySelectorAll("button").map(node => node.dataset.taskId), ["b"]);
  for (const group of groups) {
    assert.equal(group.querySelector("h2").getAttribute("id"), group.getAttribute("aria-labelledby"));
    for (const button of group.querySelectorAll("button")) {
      assert.equal(button.dataset.action, "context-rail-card");
      assert.equal(button.getAttribute("title"), button.dataset.taskId);
      assert.doesNotMatch(button.querySelector("small").textContent, /同名工作区/);
    }
  }
  const current = app.querySelectorAll('[aria-current="true"]');
  assert.equal(current.length, 1);
  assert.equal(current[0].dataset.taskId, "a");
});

test("title and status refresh preserve the draft, and switching updates current identity", () => {
  const { app, evaluate } = fixture();
  const input = app.querySelector("#mainInput");
  input.value = "尚未发送的内容";
  evaluate(`state.tasks[0].title = "更新后的标题";
    state.tasks[0].active_run = { status: "running" }; renderFocus();`);
  assert.equal(app.querySelector("#mainInput"), input);
  assert.equal(input.value, "尚未发送的内容");
  assert.equal(app.querySelector("h1").textContent, "更新后的标题");
  assert.match(app.querySelector(".task-identity-meta").textContent, /运行中/);
  evaluate(`state.activeTaskId = "b"; renderFocus();`);
  assert.equal(app.querySelector("h1").textContent, "其他工作区任务");
  assert.equal(app.querySelector(".task-identity-meta code").textContent, "b");
  assert.equal(app.querySelector('[aria-current="true"]').dataset.taskId, "b");
  evaluate(`state.activeTaskId = "assembly"; renderFocus();`);
  assert.ok(app.querySelector(".task-page-heading"));
  assert.ok(app.querySelector(".composer-context"));
  assert.equal(app.querySelector(".task-workspace-list"), null);
});
