/*
 * 本文件验证任意材料选择托盘、逐材料备注和历史消息卡片。输入为文本/图片材料、折叠偏好、
 * binding 草稿与线程历史；输出为所有材料都有本轮开关、托盘不受折叠影响、图片必看独立、
 * 备注原文呈现且内部协议不泄漏的断言。具体工作流在 VM 中调用 app.js 纯渲染函数。
 * 示例：node --test run-material-ui.test.cjs。
 */
"use strict";

const assert = require("node:assert/strict");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

const harness = createAppHarness();
harness.vm.runInContext(readAppSource(), harness.context);
const result = harness.vm.runInContext(`(() => {
  state.activeTaskId = "task";
  state.tasks = [{ task_id: "task", title: "T" }];
  state.details.set("task", { ui_state: { material_grouping_mode: "run", collapsed_material_groups: ["run:r1"] } });
  const text = { material_id: "t1", relative_path: "notes.md", material_kind: "text", is_image: false, size_bytes: 8, reading_mode: "full", instruction_mode: "reference", retention: "removable", first_run_id: "r1" };
  const image = { material_id: "i1", relative_path: "shot.png", material_kind: "image", is_image: true, size_bytes: 9, first_run_id: "r1" };
  state.materials.set("task", [text, image]);
  state.materialGroups.set("task", []);
  state.materialSelections.set("task", { bindings: [{ materialId: "t1", note: "第一行\\n第二行" }, { materialId: "i1", note: "对照" }], requiredImageIds: ["i1"], notes: { t1: "第一行\\n第二行", i1: "对照" } });
  state.materialHistory.set("task", [{ binding_id: "b", run_id: "r1", message_id: "msg1", material_id: "t1", relative_path: "notes.md", material_kind: "text", note: "第一行\\n第二行", ordinal: 0, must_view_requested: false, current_available: true }]);
  const tray = renderRunMaterialTray("task");
  const textRow = renderMaterial(text);
  const imageRow = renderImageMaterial(image);
  const groups = renderMaterialGroups(state.tasks[0]);
  const message = renderMessage({ role: "human", id: "msg1", content: "正文\\n\\n<focus_run_materials>\\n[]\\n</focus_run_materials>" });
  return { tray, textRow, imageRow, groups, message };
})()`, harness.context);

assert.match(result.textRow, /toggle-run-material/);
assert.doesNotMatch(result.textRow, /必须看/);
assert.match(result.imageRow, /toggle-image-required[^>]*aria-pressed="true"/);
assert.match(result.tray, /notes\.md/);
assert.match(result.tray, /第一行\n第二行/);
assert.match(result.groups, /material-group-body" hidden/);
assert.match(result.groups, /已选 2/);
assert.match(result.message, /message-material-card/);
assert.match(result.message, /第一行\n第二行/);
assert.doesNotMatch(result.message, /focus_run_materials/);

console.log("run-material-ui: all assertions passed");
