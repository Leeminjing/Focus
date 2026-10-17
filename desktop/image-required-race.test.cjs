/*
 * 验证图片必看校验的异步隔离：可控 Promise 挂起真实 toggleImageRequired 调用，期间切换任务、
 * 修改备注和勾选、移除图片，再检查返回时只合并到原任务的最新选择。
 * 示例：node --test desktop/image-required-race.test.cjs。
 */
"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

function setup() {
  const validation = Promise.withResolvers();
  const validationCalls = [];
  const harness = createAppHarness({
    globals: { validation: validation.promise, validationCalls },
  });
  const run = source => harness.vm.runInContext(source, harness.context);
  const selection = taskId => JSON.parse(run(`JSON.stringify(state.materialSelections.get(${JSON.stringify(taskId)}))`));
  run(readAppSource());
  run(`
    state.activeTaskId = "a";
    state.materials.set("a", [
      { material_id: "image", relative_path: "shot.png", is_image: true, size_bytes: 80 },
      { material_id: "text", relative_path: "notes.md", is_image: false, size_bytes: 10 },
    ]);
    toggleRunMaterial("image");
    updateRunMaterialNote("image", "原备注");
    api = path => { validationCalls.push(path); return validation; };
  `);
  return { run, selection, validation, validationCalls };
}

test("validation updates its originating task without changing another task's selection or notes", async () => {
  const { run, selection, validation, validationCalls } = setup();
  const pending = run('toggleImageRequired("image")');
  assert.deepEqual(validationCalls, ["/desktop/api/tasks/a/materials/image/image-validity"]);
  run(`
    state.activeTaskId = "b";
    state.materials.set("b", [{ material_id: "file-b", is_image: false, size_bytes: 10 }]);
    toggleRunMaterial("file-b");
    updateRunMaterialNote("file-b", "B 的备注");
  `);
  const beforeB = selection("b");
  validation.resolve({});
  await pending;
  assert.deepEqual(selection("b"), beforeB);
  assert.deepEqual(selection("a").requiredImageIds, ["image"]);
  assert.equal(selection("a").notes.image, "原备注");
});

test("validation preserves selections and notes edited while it was pending", async () => {
  const { run, selection, validation } = setup();
  const pending = run('toggleImageRequired("image")');
  run(`
    updateRunMaterialNote("image", "更新后的图片备注");
    toggleRunMaterial("text");
    updateRunMaterialNote("text", "新文件备注");
  `);
  const latest = selection("a");
  validation.resolve({});
  await pending;
  assert.deepEqual(selection("a"), { ...latest, requiredImageIds: ["image"] });
});

test("validation does not reselect an image deselected while it was pending", async () => {
  const { run, selection, validation } = setup();
  const pending = run('toggleImageRequired("image")');
  run('toggleRunMaterial("image")');
  const latest = selection("a");
  validation.resolve({});
  await pending;
  assert.deepEqual(selection("a"), latest);
});

test("validation uses the latest material list when an image disappears", async () => {
  const { run, selection, validation } = setup();
  const pending = run('toggleImageRequired("image")');
  run('state.materials.set("a", state.materials.get("a").filter(item => item.material_id !== "image"))');
  validation.resolve({});
  await pending;
  assert.deepEqual(selection("a"), { bindings: [], requiredImageIds: [], notes: {} });
});

test("turning off must-view does not request validation or change notes", async () => {
  const { run, selection, validationCalls } = setup();
  run('state.materialSelections.get("a").requiredImageIds = ["image"]');
  const before = selection("a");
  await run('toggleImageRequired("image")');
  assert.deepEqual(validationCalls, []);
  assert.deepEqual(selection("a"), { ...before, requiredImageIds: [] });
});

test("failed validation preserves edits and does not mark the image as required", async () => {
  const { run, selection, validation } = setup();
  const pending = run('toggleImageRequired("image")');
  run('updateRunMaterialNote("image", "校验期间修改")');
  const latest = selection("a");
  const rejected = assert.rejects(pending, /invalid image/);
  validation.reject(new Error("invalid image"));
  await rejected;
  assert.deepEqual(selection("a"), latest);
});
