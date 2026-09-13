/*
 * 本文件验证全图页面只展示活动 Context。输入为混合 active/archived/deleted 的任务与 Context tree，
 * 输出为工作区可见性与空态留白的断言；工作流走 VM DOM 替身，覆盖折叠视图与卡片视图两条渲染路径。
 * 示例：`node app-map-view.test.cjs`
 */
"use strict";
const assert = require("node:assert/strict");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

const appNode = { dataset: {}, innerHTML: "", replaceChildren() {} };
const harness = createAppHarness({ globals: { setTimeout }, selectors: { "#app": appNode } });
harness.vm.runInContext(readAppSource(), harness.context);

function task(id, workspaceId, lifecycle = "active") {
  return {
    task_id: id,
    workspace_id: workspaceId,
    workspace_name: id,
    workspace_path: `C:/${workspaceId}`,
    thread_id: `thread-${id}`,
    title: id,
    lifecycle,
    active_run: null,
  };
}

// 活动 Context 与它的工作区 / 只剩归档 Context 的工作区 / 只剩已删除 Context 的工作区
const tasks = [
  task("living", "workspace-living"),
  task("archived-root", "workspace-archived", "archived"),
  task("deleted-root", "workspace-deleted", "deleted"),
  task("archived-tail", "workspace-living", "archived"),
];
const trees = [
  { context_id: "living", title: "living", lifecycle: "active", parents: [], projection_status: "valid" },
  { context_id: "archived-root", title: "archived-root", lifecycle: "archived", parents: [], projection_status: "valid" },
  { context_id: "deleted-root", title: "deleted-root", lifecycle: "deleted", parents: [], projection_status: "valid" },
];

function renderMapHtml(viewMode) {
  appNode.innerHTML = "";
  harness.vm.runInContext(`(() => {
    const tasks = ${JSON.stringify(tasks)};
    state.tasks = tasks;
    state.contextTrees = new Map([
      ["workspace-living", ${JSON.stringify([trees[0]])}],
      ["workspace-archived", ${JSON.stringify([trees[1]])}],
      ["workspace-deleted", ${JSON.stringify([trees[2]])}],
    ]);
    state.activeTaskId = "living";
    state.soldierArmed = false;
    state.selectionMode = false;
    state.mapViewMode = ${JSON.stringify(viewMode)};
    renderMap();
    return true;
  })()`, harness.context);
  return appNode.innerHTML;
}

// 折叠视图：只有含活动 Context 的工作区出现，归档/删除的工作区整行消失
const treeHtml = renderMapHtml("tree");
assert.match(treeHtml, /workspace-living/, "有活动 Context 的工作区必须保留");
assert.doesNotMatch(treeHtml, /workspace-archived/, "只剩归档 Context 的工作区不能出现在全图");
assert.doesNotMatch(treeHtml, /workspace-deleted/, "只剩已删除 Context 的工作区不能出现在全图");
assert.doesNotMatch(treeHtml, /0 个 Context/, "全图不应再有 0 个 Context 的空工作区行");
assert.doesNotMatch(treeHtml, /archived-tail/, "归档 Context 不进入全图");

// 卡片视图：与折叠视图同一套过滤规则（两条渲染路径都从 renderMap 拿到已过滤的任务）
const cardsHtml = renderMapHtml("cards");
assert.match(cardsHtml, /workspace-living/, "卡片视图保留有活动 Context 的工作区");
assert.doesNotMatch(cardsHtml, /workspace-archived/, "卡片视图同样过滤只剩归档 Context 的工作区");
assert.doesNotMatch(cardsHtml, /workspace-deleted/, "卡片视图同样过滤只剩已删除 Context 的工作区");
assert.match(cardsHtml, /1 个 Context/, "卡片视图计数只统计活动 Context");
assert.doesNotMatch(cardsHtml, /0 个 Context/, "卡片视图不应出现 0 个 Context");

// 没有一个活动 Context 时整页留白：工具栏、折叠视图、工作区分组、占位文案都不渲染
appNode.innerHTML = "";
harness.vm.runInContext(`(() => {
  state.tasks = ${JSON.stringify(tasks.filter(item => item.lifecycle !== "active"))};
  state.activeTaskId = null;
  renderMap();
  return true;
})()`, harness.context);
const emptyHtml = appNode.innerHTML;
assert.doesNotMatch(emptyHtml, /map-toolbar/, "空态不渲染只有空操作的全图工具栏");
assert.doesNotMatch(emptyHtml, /map-collapsible-view/, "空态不渲染折叠视图");
assert.doesNotMatch(emptyHtml, /map-workspace-group/, "空态不渲染工作区分组");
assert.doesNotMatch(emptyHtml, /暂无/, "空态不显示任何占位文案");

const templateNode = { innerHTML: "<p>GLOBAL-EMPTY-TEMPLATE</p>", content: { cloneNode: () => templateNode } };
const renderAppNode = { dataset: {}, innerHTML: "", replaceChildren: node => { renderAppNode.innerHTML = node.innerHTML; } };
const renderHarness = createAppHarness({
  globals: { setTimeout },
  selectors: { "#app": renderAppNode, "#emptyTemplate": templateNode },
});
renderHarness.vm.runInContext(readAppSource(), renderHarness.context);

// render() 对全图不得套用全局空态模板：tasks 为空时 state.view === "map" 仍要渲染留白的全图
renderAppNode.innerHTML = "";
renderHarness.vm.runInContext(`(() => {
  state.tasks = [];
  state.activeTaskId = null;
  state.view = "map";
  state.mapViewMode = "tree";
  render();
  return true;
})()`, renderHarness.context);
const blankMapHtml = renderAppNode.innerHTML;
assert.doesNotMatch(blankMapHtml, /GLOBAL-EMPTY-TEMPLATE/, "全图不能被全局空态模板替换");
assert.doesNotMatch(blankMapHtml, /map-toolbar/, "空的全图不渲染工具栏");
assert.doesNotMatch(blankMapHtml, /map-collapsible-view/, "空的全图不渲染折叠视图");
assert.doesNotMatch(blankMapHtml, /map-workspace-group/, "空的全图不渲染工作区分组");

// 任务视图保留全局空态兜底
renderAppNode.innerHTML = "";
renderHarness.vm.runInContext(`(() => {
  state.tasks = [];
  state.view = "focus";
  render();
  return true;
})()`, renderHarness.context);
assert.match(renderAppNode.innerHTML, /GLOBAL-EMPTY-TEMPLATE/, "任务视图在没有任何任务时仍显示全局空态");

console.log("app-map-view: 全图只展示活动 Context、两个视图一致、空态整页留白通过");