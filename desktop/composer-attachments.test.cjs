/* 回归普通输入框附件的默认绑定与异步上传任务归属。
 * 输入真实 change/paste 监听器，上传期间切换任务，断言上传和发送材料始终属于事件发生时的任务。
 * 运行：node --test desktop/composer-attachments.test.cjs。
 */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

function sendingSetup() {
  const input = { value: "看看这个报错", focus() {} };
  const cards = { innerHTML: "" }, overlay = { innerHTML: "" }, revoked = [];
  const h = createAppHarness({ selectors: { "#mainInput": input, "#composerAttachments": cards, "#overlayRoot": overlay }, statusNode: "record", globals: {
    FormData: class { append(key, file) { this.file = file; } },
    URL: class extends URL {
      static createObjectURL(file) { return `blob:${file.name}`; }
      static revokeObjectURL(url) { revoked.push(url); }
    },
  } });
  const evaluate = code => h.vm.runInContext(code, h.context);
  evaluate(readAppSource());
  evaluate(`
    state.activeTaskId = "A";
    state.tasks = [{ task_id: "A", workspace_id: "workspace" }];
    state.details.set("A", { messages: [], ui_state: {} });
    refreshMaterialView = () => {};
    renderFocus = () => {};
    listenToRun = () => {};
    persistFocusState = () => {};
    globalThis.requests = [];
    globalThis.uploads = [];
    api = async (path, options) => {
      if (path.endsWith("/upload")) return new Promise((resolve, reject) => uploads.push({ resolve, reject, file: options.body.file }));
      if (path.endsWith("/materials")) return state.materials.get("A") || [];
      if (path.endsWith("/main/runs")) {
        requests.push(JSON.parse(options.body));
        return { run_id: "run", message_id: "message", status: "pending", kind: "main" };
      }
    };
  `);
  return { ...h, input, cards, overlay, revoked, evaluate };
}

const settle = () => new Promise(resolve => setImmediate(resolve));
function pasteFiles(h, names) {
  h.dispatch("paste", { target: h.input, preventDefault() {}, clipboardData: {
    items: names.map(name => ({ type: "image/png", getAsFile: () => ({ name, type: "image/png" }) })),
  } });
}
function attachmentAction(h, action, entry, materialId) {
  h.context.attachmentButton = { dataset: { action: `composer-${action}-attachment` }, closest: () => ({ dataset: {
    composerTask: "A", composerUpload: entry?.id, composerMaterial: materialId,
  } }) };
  return h.evaluate("handleComposerAttachmentAction(attachmentButton)");
}

test("paste then immediate send waits and includes the image in that request", async () => {
  const h = sendingSetup();
  h.dispatch("paste", { target: h.input, preventDefault() {}, clipboardData: {
    items: [{ type: "image/png", getAsFile: () => ({ name: "error.png" }) }],
  } });
  const sending = h.evaluate("sendMain()");
  await Promise.resolve();
  assert.equal(h.context.requests.length, 0, "upload preparation must finish before posting the message");
  h.context.uploads[0].resolve({ material_id: "image", is_image: true, relative_path: "error.png" });
  await sending;
  assert.deepEqual(JSON.parse(JSON.stringify(h.context.requests[0].material_inputs)), [{ material_id: "image", note: "" }]);
  assert.equal(h.evaluate('materialSelection("A").bindings.length'), 0);
});

test("all pasted files appear before the UI action starts and are awaited as one batch", async () => {
  const h = sendingSetup();
  h.evaluate('runUiAction = action => { globalThis.startUploads = action; };');
  pasteFiles(h, ["first.png", "second.png"]);
  assert.match(h.cards.innerHTML, /first.png/);
  assert.match(h.cards.innerHTML, /second.png/);
  assert.match(h.cards.innerHTML, /准备中/);
  const sending = h.evaluate("sendMain()");
  assert.match(h.statusState.text, /正在准备本次附件/);
  h.evaluate("startUploads()");
  h.context.uploads[0].resolve({ material_id: "first", relative_path: "first.png", is_image: true });
  await settle();
  assert.equal(h.context.requests.length, 0);
  h.context.uploads[1].resolve({ material_id: "second", relative_path: "second.png", is_image: true });
  await sending;
  assert.deepEqual(Array.from(h.context.requests[0].material_inputs, item => item.material_id), ["first", "second"]);
  assert.equal(h.cards.innerHTML, "");
});

test("upload failure blocks sending and retains the text, failed card and retry file", async () => {
  const h = sendingSetup();
  pasteFiles(h, ["error.png"]);
  const sending = h.evaluate("sendMain()");
  await settle();
  h.context.uploads[0].reject(new Error("网络断开"));
  await sending;
  assert.equal(h.context.requests.length, 0);
  assert.equal(h.input.value, "看看这个报错");
  assert.match(h.cards.innerHTML, /失败：网络断开/);
  assert.match(h.cards.innerHTML, /composer-retry-attachment/);
  assert.match(h.statusState.text, /附件准备失败/);
  const entry = h.evaluate('[...composerAttachments("A").values()][0]');
  const retry = attachmentAction(h, "retry", entry);
  assert.equal(h.context.uploads[1].file, h.context.uploads[0].file);
  h.context.uploads[1].resolve({ material_id: "image", is_image: true, relative_path: "error.png" });
  await retry;
  assert.match(h.cards.innerHTML, /已就绪/);
  await h.evaluate("sendMain()");
  assert.equal(h.context.requests[0].material_inputs[0].material_id, "image");
});

test("a failed card also blocks a later send until it is retried or removed", async () => {
  const h = sendingSetup();
  pasteFiles(h, ["error.png"]);
  await settle();
  h.context.uploads[0].reject(new Error("网络断开"));
  await settle();
  await h.evaluate("sendMain()");
  assert.equal(h.context.requests.length, 0);
  const entry = h.evaluate('[...composerAttachments("A").values()][0]');
  await attachmentAction(h, "remove", entry);
  await h.evaluate("sendMain()");
  assert.equal(h.context.requests.length, 1);
  assert.equal(h.context.requests[0].material_inputs.length, 0);
});

test("removing a preparing image releases the send and its late upload never selects it", async () => {
  const h = sendingSetup();
  pasteFiles(h, ["removed.png"]);
  const sending = h.evaluate("sendMain()");
  await settle();
  const entry = h.evaluate('[...composerAttachments("A").values()][0]');
  await attachmentAction(h, "remove", entry);
  await sending;
  assert.equal(h.context.requests[0].material_inputs.length, 0);
  assert.deepEqual(h.revoked, ["blob:removed.png"]);
  h.context.uploads[0].resolve({ material_id: "removed", is_image: true });
  await settle();
  assert.equal(h.evaluate('materialSelection("A").bindings.length'), 0);
  assert.equal(h.cards.innerHTML, "");
});

test("send freezes text and attachment scope; later additions remain selected for the next request", async () => {
  const h = sendingSetup();
  pasteFiles(h, ["first.png"]);
  const sending = h.evaluate("sendMain()");
  pasteFiles(h, ["next.png"]);
  h.input.value = "下一条";
  h.evaluate('composerDraft.claim("A", "下一条")');
  await settle();
  h.context.uploads[1].resolve({ material_id: "next", is_image: true, relative_path: "next.png" });
  await settle();
  h.context.uploads[0].resolve({ material_id: "first", is_image: true, relative_path: "first.png" });
  await sending;
  assert.equal(h.context.requests[0].message, "看看这个报错");
  assert.deepEqual(Array.from(h.context.requests[0].material_inputs, item => item.material_id), ["first"]);
  assert.deepEqual(Array.from(h.evaluate('materialSelection("A").bindings'), item => item.materialId), ["next"]);
  assert.equal(h.input.value, "下一条");
  assert.match(h.cards.innerHTML, /next.png/);
  assert.deepEqual(h.revoked, ["blob:first.png"]);
});

test("local image preview works before upload, and removing a ready card deselects only that material", async () => {
  const h = sendingSetup();
  pasteFiles(h, ["preview.png"]);
  const entry = h.evaluate('[...composerAttachments("A").values()][0]');
  await attachmentAction(h, "preview", entry);
  assert.match(h.overlay.innerHTML, /src="blob:preview.png"/);
  await settle();
  h.context.uploads[0].resolve({ material_id: "image", is_image: true, relative_path: "preview.png" });
  await settle();
  await attachmentAction(h, "remove", entry, "image");
  assert.equal(h.evaluate('materialSelection("A").bindings.length'), 0);
  assert.equal(h.evaluate('state.materials.get("A").length'), 1, "remove must keep the task material");
  assert.equal(h.cards.innerHTML, "");
});

test("removing an existing ready material while waiting also removes it from this request", async () => {
  const h = sendingSetup();
  h.evaluate(`state.materials.set("A", [{material_id:"old", relative_path:"old.png", is_image:true}]); toggleRunMaterial("old");`);
  pasteFiles(h, ["new.png"]);
  const sending = h.evaluate("sendMain()");
  await attachmentAction(h, "remove", null, "old");
  await settle();
  h.context.uploads[0].resolve({ material_id: "new", is_image: true });
  await sending;
  assert.deepEqual(Array.from(h.context.requests[0].material_inputs, item => item.material_id), ["new"]);
});

test("preparation belongs to the sending task across switches, and duplicate send is ignored", async () => {
  const h = sendingSetup();
  pasteFiles(h, ["error.png"]);
  const sending = h.evaluate("sendMain()");
  await h.evaluate("sendMain()");
  h.evaluate('state.activeTaskId = "B"; state.materials.set("B", []);');
  h.input.value = "任务 B 的草稿";
  await settle();
  h.context.uploads[0].resolve({ material_id: "image", is_image: true });
  await sending;
  assert.equal(h.context.requests.length, 1);
  assert.equal(h.context.requests[0].material_inputs[0].material_id, "image");
  assert.equal(h.evaluate('materialSelection("B").bindings.length'), 0);
  assert.equal(h.input.value, "任务 B 的草稿");
});

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

test("failed uploads never select a material; successful receipts need no follow-up listing", async () => {
  const { h } = setup();
  h.vm.runInContext('api = async () => { throw new Error("upload failed"); }', h.context);
  await h.vm.runInContext('uploadMaterialFile({name:"failed.txt"}, "A")', h.context);
  assert.equal(h.vm.runInContext('materialSelection("A").bindings.length', h.context), 1);
  h.vm.runInContext('globalThis.listings = 0; api = async (path, options) => { if (options) return {material_id:"uploaded"}; listings++; throw new Error("refresh failed"); }', h.context);
  await h.vm.runInContext('uploadMaterialFile({name:"ok.txt"}, "A")', h.context);
  assert.deepEqual(Array.from(h.vm.runInContext('materialSelection("A").bindings.map(item => item.materialId)', h.context)), ["existing", "uploaded"]);
  assert.equal(h.context.listings, 0, "a stale follow-up listing must not overwrite a concurrent upload receipt");
});
