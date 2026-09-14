/*
 * 本文件验证逐轮材料备注的渲染位置、草稿语义与历史材料卡片。输入为文本/图片/未选材料、折叠
 * 偏好、binding 草稿、线程历史与材料区事件入口；输出为备注框只出现在所属材料条目内、主输入区
 * 无材料托盘、取消勾选只隐藏输入框不丢弃草稿、重新勾选恢复原文、图片必看独立、分组折叠与已选
 * 摘要正确、备注编辑不启动材料拖放、内部协议不泄漏的断言。工作流在 VM 中调用 app.js 渲染函数、
 * 材料动作函数与 document 委托入口，并按输入顺序核对返回标记。
 * 示例：node run-material-ui.test.cjs。
 */
"use strict";

const assert = require("node:assert/strict");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

const appNode = { dataset: {}, innerHTML: "" };
const harness = createAppHarness({ selectors: { "#app": appNode } });
harness.vm.runInContext(readAppSource(), harness.context);

const result = harness.vm.runInContext(`(() => {
  state.activeTaskId = "task";
  state.tasks = [{ task_id: "task", title: "T" }];
  state.details.set("task", { ui_state: { material_grouping_mode: "run", collapsed_material_groups: ["run:r1"] } });
  const text = { material_id: "t1", relative_path: "notes.md", material_kind: "text", is_image: false, size_bytes: 8, reading_mode: "full", instruction_mode: "reference", retention: "removable", first_run_id: "r1" };
  const image = { material_id: "i1", relative_path: "shot.png", material_kind: "image", is_image: true, size_bytes: 9, first_run_id: "r1" };
  const idle = { material_id: "t2", relative_path: "idle.md", material_kind: "text", is_image: false, size_bytes: 5, reading_mode: "rough", instruction_mode: "reference", retention: "removable", first_run_id: "r2" };
  state.materials.set("task", [text, image, idle]);
  state.materialGroups.set("task", []);
  state.materialSelections.set("task", { bindings: [{ materialId: "t1", note: "第一行\\n第二行" }, { materialId: "i1", note: "对照" }], requiredImageIds: ["i1"], notes: { t1: "第一行\\n第二行", i1: "对照" } });
  state.materialHistory.set("task", [{ binding_id: "b", run_id: "r1", message_id: "msg1", material_id: "t1", relative_path: "notes.md", material_kind: "text", note: "第一行\\n第二行", ordinal: 0, must_view_requested: false, current_available: true }]);

  const textRow = renderMaterial(text);
  const imageRow = renderImageMaterial(image);
  const idleRow = renderMaterial(idle);
  const groups = renderMaterialGroups(state.tasks[0]);

  // 展开管理详情时，备注输入框必须排在本轮选择控件之后、长期策略详情之前。
  state.openMaterial = "t1";
  const openRow = renderMaterial(text);
  state.openMaterial = null;

  // 取消勾选只隐藏输入框：备注草稿仍按材料 ID 保留，重新勾选恢复原文。
  toggleRunMaterial("t1");
  const unselectedRow = renderMaterial(text);
  const keptNote = state.materialSelections.get("task").notes.t1;
  toggleRunMaterial("t1");
  const reselectedRow = renderMaterial(text);

  // 主输入区不再有第二份材料投影：托盘渲染函数与备注输入框都不得出现在聚焦视图标记中。
  mountCommitmentRecovery = () => {};
  restoreCommitmentPanels = () => {};
  mountPatrolAvatarLayer = () => {};
  renderFocus(state.tasks[0]);

  return {
    textRow, imageRow, idleRow, openRow, unselectedRow, reselectedRow, groups, keptNote,
    focus: document.querySelector("#app").innerHTML,
    trayRenderer: typeof renderRunMaterialTray,
    message: renderMessage({ role: "human", id: "msg1", content: "正文\\n\\n<focus_run_materials>\\n[]\\n</focus_run_materials>" }),
  };
})()`, harness.context);

// 已选材料：备注框就在该材料条目内，且不冒充图片必看控件。
assert.match(result.textRow, /run-material-note"><textarea[^>]*data-field="run-material-note"/);
assert.match(result.textRow, /run-material-note"><textarea[^>]*draggable="false"/);
assert.match(result.textRow, /第一行\n第二行/);
assert.match(result.textRow, /toggle-run-material/);
assert.doesNotMatch(result.textRow, /必须看/);
assert.match(result.openRow, /material-policy-row[\s\S]*run-material-note[\s\S]*material-editor/, "备注框必须位于本轮选择控件之后、长期策略详情之前");

// 图片材料：备注与「必须看」各自独立。
assert.match(result.imageRow, /toggle-image-required[^>]*aria-pressed="true"/);
assert.match(result.imageRow, /run-material-note[\s\S]*对照/);

// 未选材料：没有任何备注输入框。
assert.doesNotMatch(result.idleRow, /run-material-note/);
assert.doesNotMatch(result.idleRow, /data-field="run-material-note"/);
assert.match(result.idleRow, /toggle-run-material" aria-pressed="false"/);

// 取消勾选隐藏输入框但保留草稿，重新勾选恢复原文。
assert.doesNotMatch(result.unselectedRow, /run-material-note/);
assert.equal(result.keptNote, "第一行\n第二行");
assert.match(result.reselectedRow, /第一行\n第二行/);

// 主输入区无托盘：托盘渲染函数已删除，聚焦视图不出现托盘标记或备注输入框。
assert.equal(result.trayRenderer, "undefined");
assert.doesNotMatch(result.focus, /run-material-tray/);
assert.doesNotMatch(result.focus, /本轮材料/);
assert.doesNotMatch(result.focus, /data-field="run-material-note"/);
assert.match(result.focus, /class="composer"/);

// 分组折叠与已选摘要、历史材料卡片保持不变。
assert.match(result.groups, /material-group-body" hidden/);
assert.match(result.groups, /已选 2/);
assert.match(result.message, /message-material-card/);
assert.match(result.message, /第一行\n第二行/);
assert.doesNotMatch(result.message, /focus_run_materials/);

// 备注输入：事件委托经最近的 [data-material-id] 解析材料身份，不再依赖托盘专属属性。
const inputHandler = harness.listeners.get("input").at(-1);
const noteField = {
  value: "改写后的备注",
  id: "",
  matches: selector => selector === '[data-field="run-material-note"]',
  closest: selector => selector === "[data-material-id]" ? { dataset: { materialId: "t1" } } : null,
};
inputHandler({ target: noteField });
assert.equal(harness.vm.runInContext('state.materialSelections.get("task").notes.t1', harness.context), "改写后的备注");

// 拖放隔离：从备注输入框起点拖拽不得产生材料拖放，从材料行非交互区域拖拽仍然生效。
const dragStartHandlers = harness.listeners.get("dragstart");
const dragFrom = originIsControl => {
  const dataTransfer = { data: {}, effectAllowed: null, setData(key, value) { this.data[key] = value; } };
  const materialRow = { dataset: { materialId: "t1" } };
  const event = {
    target: {
      matches: () => false,
      closest: selector => (selector.includes("textarea")
        ? (originIsControl ? {} : null)
        : (originIsControl ? null : materialRow)),
    },
    dataTransfer,
  };
  for (const handler of dragStartHandlers) handler(event);
  return dataTransfer;
};
const fromNote = dragFrom(true);
assert.equal(Object.hasOwn(fromNote.data, "text/focus-material-id"), false, "备注框内的文本手势不得变成材料拖放");
assert.equal(fromNote.effectAllowed, null);
const fromRow = dragFrom(false);
assert.equal(fromRow.data["text/focus-material-id"], "t1");
assert.equal(fromRow.effectAllowed, "move");

// 抓手不变量：守卫必须排除文本编辑控件，但不得排除 button——材料名称摘要本身就是 button，
// 排除它会让整行重排失去可抓取区域。
const queried = [];
for (const handler of dragStartHandlers) {
  handler({
    target: { matches: () => false, closest: selector => { queried.push(selector); return null; } },
    dataTransfer: { data: {}, effectAllowed: null, setData() {} },
  });
}
assert.ok(queried.some(selector => selector.includes("textarea")), "拖放入口必须排除备注等文本编辑控件");
assert.ok(queried.every(selector => !selector.includes("button")), "拖放入口不得排除 button，否则材料名称摘要无法作为重排抓手");

console.log("run-material-ui: all assertions passed");
