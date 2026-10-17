/* 回归普通输入框附件的默认绑定与异步上传任务归属。
 * 输入真实 change/paste 监听器，上传期间切换任务，断言上传和发送材料始终属于事件发生时的任务。
 * 运行：node --test desktop/composer-attachments.test.cjs。
 */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

function setup() {
  const input = {};
  const h = createAppHarness({ selectors: { "#mainInput": input }, globals: {
    FormData: class { append(key, file) { this.file = file; } },
  } });
  h.vm.runInContext(readAppSource(), h.context);
  h.vm.runInContext(`
    state.activeTaskId = "A";
    state.materials.set("A", [{ material_id: "existing" }]);
    state.materialSelections.set("A", runMaterialPicker.toggleMaterial(runMaterialPicker.empty(), "existing"));
    globalThis.calls = [];
    globalThis.materials = { A: [{ material_id: "existing" }], B: [] };
    refreshMaterialView = () => {};
    runUiAction = action => (globalThis.pendingAction = Promise.resolve().then(action));
    api = async (path, options) => {
      const taskId = path.split("/")[4];
      if (options?.method === "POST") {
        calls.push({ taskId, name: options.body.file.name });
        const material = { material_id: options.body.file.name, is_image: true, size_bytes: 10 };
        materials[taskId].push(material);
        state.activeTaskId = "B";
        return material;
      }
      return materials[taskId];
    };
  `, h.context);
  return { h, input };
}

test("new file is selected for its original task and can be deselected", async () => {
  const { h } = setup();
  const input = { id: "fileInput", files: [{ name: "report.txt" }], value: "report.txt" };
  h.dispatch("change", { target: input });
  await h.context.pendingAction;
  assert.deepEqual(Array.from(h.vm.runInContext('materialSelection("A").bindings.map(item => item.materialId)', h.context)), ["existing", "report.txt"]);
  assert.equal(h.vm.runInContext('materialSelection("B").bindings.length', h.context), 0);
  assert.equal(input.value, "");
  h.vm.runInContext('state.activeTaskId = "A"; toggleRunMaterial("report.txt")', h.context);
  assert.deepEqual(Array.from(h.vm.runInContext('materialSelection("A").bindings.map(item => item.materialId)', h.context)), ["existing"]);
});

for (const switchBeforeStart of [false, true]) test(`paste batch stays with its task (${switchBeforeStart ? "before start" : "during first upload"})`, async () => {
  const { h, input } = setup();
  let prevented = false;
  h.dispatch("paste", { target: input, preventDefault() { prevented = true; }, clipboardData: {
    items: ["first.png", "second.png"].map(name => ({ type: "image/png", getAsFile: () => ({ name }) })),
  } });
  if (switchBeforeStart) h.vm.runInContext('state.activeTaskId = "B"', h.context);
  await h.context.pendingAction;
  assert.equal(prevented, true);
  assert.deepEqual(JSON.parse(JSON.stringify(h.context.calls)), [{ taskId: "A", name: "first.png" }, { taskId: "A", name: "second.png" }]);
  const outgoing = h.vm.runInContext('runMaterialPicker.buildOutgoing(state.materials.get("A"), "看看这个报错", materialSelection("A"))', h.context);
  assert.deepEqual(Array.from(outgoing.materialInputs, item => item.material_id), ["existing", "first.png", "second.png"]);
  assert.equal(h.vm.runInContext('materialSelection("B").bindings.length', h.context), 0);
});

test("file selection captures its task before the UI action starts", async () => {
  const { h } = setup();
  h.dispatch("change", { target: { id: "fileInput", files: [{ name: "report.txt" }] } });
  h.vm.runInContext('state.activeTaskId = "B"', h.context);
  await h.context.pendingAction;
  assert.equal(h.context.calls[0].taskId, "A");
});

test("failed uploads never select a material; refresh failure retains a successful upload", async () => {
  const { h } = setup();
  h.vm.runInContext('api = async () => { throw new Error("upload failed"); }', h.context);
  await h.vm.runInContext('uploadMaterialFile({name:"failed.txt"}, "A")', h.context);
  assert.equal(h.vm.runInContext('materialSelection("A").bindings.length', h.context), 1);
  h.vm.runInContext('api = async (path, options) => { if (options) return {material_id:"uploaded"}; throw new Error("refresh failed"); }', h.context);
  await h.vm.runInContext('uploadMaterialFile({name:"ok.txt"}, "A")', h.context);
  assert.deepEqual(Array.from(h.vm.runInContext('materialSelection("A").bindings.map(item => item.materialId)', h.context)), ["existing", "uploaded"]);
});
