/*
 * 本文件验证全图页面只展示活动 Context。输入为混合 active/archived/deleted 的任务与 Context tree，
 * 输出为活动工作区过滤及空态导航保留的断言；工作流走 VM DOM 替身，覆盖折叠视图、卡片视图及原任务空态。
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

const treeHtml = renderMapHtml("tree");
assert.match(treeHtml, /workspace-living/, "有活动 Context 的工作区必须保留");
assert.doesNotMatch(treeHtml, /workspace-archived/, "只剩归档 Context 的工作区不能出现在全图");
assert.doesNotMatch(treeHtml, /workspace-deleted/, "只剩已删除 Context 的工作区不能出现在全图");
assert.doesNotMatch(treeHtml, /0 个 Context/, "全图不应再有 0 个 Context 的空工作区行");
assert.doesNotMatch(treeHtml, /archived-tail/, "归档 Context 不进入全图");

const cardsHtml = renderMapHtml("cards");
assert.match(cardsHtml, /workspace-living/, "卡片视图保留有活动 Context 的工作区");
assert.doesNotMatch(cardsHtml, /workspace-archived/, "卡片视图同样过滤只剩归档 Context 的工作区");
assert.doesNotMatch(cardsHtml, /workspace-deleted/, "卡片视图同样过滤只剩已删除 Context 的工作区");
assert.match(cardsHtml, /1 个 Context/, "卡片视图计数只统计活动 Context");
assert.doesNotMatch(cardsHtml, /0 个 Context/, "卡片视图不应出现 0 个 Context");

appNode.innerHTML = "";
harness.vm.runInContext(`(() => {
  state.tasks = ${JSON.stringify(tasks.filter(item => item.lifecycle !== "active"))};
  state.activeTaskId = null;
  renderMap();
  return true;
})()`, harness.context);
const emptyHtml = appNode.innerHTML;
assert.match(emptyHtml, /data-map-view="graph"/, "空态仍能切回已提交关系");
assert.doesNotMatch(emptyHtml, /map-collapsible-view/, "空态不渲染折叠视图");
assert.doesNotMatch(emptyHtml, /map-workspace-group/, "空态不渲染工作区分组");
assert.match(emptyHtml, /暂无匹配的活动任务/, "空态说明当前任务范围");

const templateNode = { innerHTML: "<p>GLOBAL-EMPTY-TEMPLATE</p>", content: { cloneNode: () => templateNode } };
const renderAppNode = { dataset: {}, innerHTML: "", replaceChildren: node => { renderAppNode.innerHTML = node.innerHTML; } };
const renderHarness = createAppHarness({
  globals: { setTimeout },
  selectors: { "#app": renderAppNode, "#emptyTemplate": templateNode },
});
renderHarness.vm.runInContext(readAppSource(), renderHarness.context);

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
assert.match(blankMapHtml, /data-action="show-patrol"/, "空的全图仍能返回 Patrol");
assert.doesNotMatch(blankMapHtml, /map-collapsible-view/, "空的全图不渲染折叠视图");
assert.doesNotMatch(blankMapHtml, /map-workspace-group/, "空的全图不渲染工作区分组");

renderAppNode.innerHTML = "";
renderHarness.vm.runInContext(`(() => {
  state.tasks = [];
  state.view = "focus";
  render();
  return true;
})()`, renderHarness.context);
assert.match(renderAppNode.innerHTML, /暂无活动 Context/, "任务视图显示实际活动范围空态");
assert.match(renderAppNode.innerHTML, /data-action="new-task"/, "任务空态仍可新增任务");
assert.match(renderAppNode.innerHTML, /data-action="open-settings"/, "任务空态仍可查看归档");

console.log("app-map-view: 活动 Context 过滤、模式导航与空态恢复入口通过");
