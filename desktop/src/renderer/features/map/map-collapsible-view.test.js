/*
 * 本文件验证全图折叠视图的纯行为。输入为包含多父、非 active 祖先和多层后代的模拟数据，
 * 输出为 Node test 断言；工作流覆盖唯一挂载、墓碑排除、ARIA 标记和键盘导航边界。
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const view = require("./map-collapsible-view.js");

function task(id, extra = {}) {
  return {
    task_id: id,
    thread_id: `thread-${id}`,
    workspace_id: "workspace-a",
    workspace_name: "focus",
    workspace_path: "C:\\focus",
    title: id,
    lifecycle: "active",
    active_run: null,
    ...extra,
  };
}

function node(id, parents = [], extra = {}) {
  return { context_id: id, title: id, lifecycle: "active", parents: parents.map(context_id => ({ context_id })), projection_status: "valid", ...extra };
}

test("非 active 祖先不成为可操作节点，active 后代提升且只出现一次", () => {
  const tasks = [
    task("root", { lifecycle: "archived" }),
    task("child"),
    task("leaf"),
  ];
  const trees = new Map([["workspace-a", [
    node("root", [], { lifecycle: "archived" }),
    node("child", ["root"]),
    node("leaf", ["child"]),
  ]]]);
  const model = view.buildModel(tasks, trees);
  const group = model.workspaces[0].roots[0];
  assert.equal(group.nodes.has("root"), false);
  assert.equal(group.nodes.get("child").parentId, null);
  assert.equal(group.nodes.get("leaf").parentId, "child");
  assert.deepEqual(group.topIds, ["child"]);

  const html = view.render(model, {
    tasks,
    expandedWorkspaceIds: new Set(["workspace-a"]),
    expandedContextIds: new Set(["child"]),
  });
  assert.equal((html.match(/data-task-id="child"/g) || []).length, 1);
  assert.equal((html.match(/data-task-id="leaf"/g) || []).length, 1);
  assert.equal(html.includes('data-task-id="root"'), false);
  assert.match(html, /根 Context 已归档/);
});

test("多父 Context 只按首个有效父节点挂载并暴露其他来源", () => {
  const tasks = [task("root"), task("other", { title: "第二来源" }), task("child", { title: "<child>" })];
  const trees = new Map([["workspace-a", [node("root"), node("other"), node("child", ["root", "other"])]]]);
  const model = view.buildModel(tasks, trees);
  assert.equal(model.contexts.get("child").parentId, "root");
  assert.equal(model.contexts.get("child").additionalParents.length, 1);

  const html = view.render(model, {
    tasks,
    expandedWorkspaceIds: new Set(["workspace-a"]),
    expandedContextIds: new Set(["root"]),
  });
  assert.equal((html.match(/data-task-id="child"/g) || []).length, 1);
  assert.match(html, /\+1 来源/);
  assert.match(html, /第二来源/);
  assert.match(html, /&lt;child&gt;/);
});

test("渲染当前项、选择态和树语义", () => {
  const tasks = [task("root", { active_run: { status: "running" } }), task("child")];
  const trees = new Map([["workspace-a", [node("root"), node("child", ["root"])]]]);
  const model = view.buildModel(tasks, trees);
  const html = view.render(model, {
    tasks,
    activeTaskId: "child",
    selectionMode: true,
    selectedContextIds: new Set(["child"]),
    expandedWorkspaceIds: new Set(["workspace-a"]),
    expandedContextIds: new Set(["root"]),
    presentStatus: status => ({ label: status === "running" ? "运行中" : "就绪", tone: status === "running" ? "active" : "neutral" }),
  });
  assert.match(html, /role="tree"/);
  assert.match(html, /role="treeitem"/);
  assert.match(html, /aria-current="page"/);
  assert.match(html, /aria-pressed="true"/);
  assert.match(html, /data-action="toggle-select-session"/);
  assert.match(html, />当前</);
});

test("展开路径只包含当前项的可见祖先", () => {
  const tasks = [task("root"), task("child"), task("leaf")];
  const trees = new Map([["workspace-a", [node("root"), node("child", ["root"]), node("leaf", ["child"])]]]);
  assert.deepEqual(view.expansionForTask(view.buildModel(tasks, trees), "leaf"), {
    workspaceId: "workspace-a",
    contextIds: ["root", "child"],
  });
});

test("键盘动作遵循可见树项顺序和父子关系", () => {
  const items = [
    { key: "workspace:w", parentKey: "", hasChildren: true, expanded: true },
    { key: "context:r", parentKey: "workspace:w", hasChildren: true, expanded: false },
    { key: "context:s", parentKey: "workspace:w", hasChildren: false, expanded: false },
  ];
  assert.deepEqual(view.treeKeyAction("ArrowDown", 0, items), { type: "focus", index: 1 });
  assert.deepEqual(view.treeKeyAction("ArrowUp", 0, items), { type: "focus", index: 0 });
  assert.deepEqual(view.treeKeyAction("ArrowRight", 1, items), { type: "expand", key: "context:r" });
  assert.deepEqual(view.treeKeyAction("ArrowLeft", 1, items), { type: "focus", index: 0 });
  assert.deepEqual(view.treeKeyAction("End", 0, items), { type: "focus", index: 2 });
  assert.equal(view.treeKeyAction("ArrowRight", 2, items), null);
});
