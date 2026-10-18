/* 回归普通输入框附件的默认绑定与异步上传任务归属。
 * 输入真实 change/paste/drop 监听器，验证纯附件发送、准备失败、移除及上传期间切换任务的归属。
 * 运行：node --test desktop/composer-attachments.test.cjs。
 */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { createAppHarness, readAppSource, createNode } = require("./test-helper.cjs");

function sendingSetup() {
  const classes = new Set();
  const composer = createNode({ classList: {
    add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name),
  } });
  const input = createNode({ id: "mainInput", value: "看看这个报错", closest: selector => selector === "[data-main-composer]" ? composer : null });
  composer.contains = target => target === input;
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
    state.view = "focus";
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
  return { ...h, input, composer, cards, overlay, revoked, evaluate };
}

const settle = () => new Promise(resolve => setImmediate(resolve));
function pasteFiles(h, names) {
  h.dispatch("paste", { target: h.input, preventDefault() {}, clipboardData: {
    items: names.map(name => ({ type: "image/png", getAsFile: () => ({ name, type: "image/png" }) })),
  } });
}
function dragFiles(h, type, files = [], target = h.input, types = ["Files"], relatedTarget = null) {
  const event = { target, relatedTarget, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
    dataTransfer: { types, files, getData: () => "" },
  };
  h.dispatch(type, event);
  return event;
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

test("an image with no text waits for upload and sends without a fabricated message", async () => {
  const h = sendingSetup();
  h.input.value = "  \n ";
  pasteFiles(h, ["screenshot.png"]);
  const sending = h.evaluate("sendMain()");
  await settle();
  assert.equal(h.context.requests.length, 0);
  h.context.uploads[0].resolve({ material_id: "image", is_image: true, relative_path: "screenshot.png" });
  await sending;
  assert.equal(h.context.requests[0].message, "");
  assert.deepEqual(Array.from(h.context.requests[0].material_inputs, item => item.material_id), ["image"]);
  assert.equal(h.input.value, "");
  assert.equal(h.cards.innerHTML, "");
});

test("ready attachments send with empty text, while an empty composer sends nothing", async () => {
  const h = sendingSetup();
  h.input.value = "";
  await h.evaluate("sendMain()");
  assert.equal(h.context.requests.length, 0);
  h.evaluate('state.materials.set("A", [{material_id:"report",relative_path:"report.txt"}]); toggleRunMaterial("report");');
  await h.evaluate("sendMain()");
  assert.equal(h.context.requests[0].message, "");
  assert.equal(h.context.requests[0].material_inputs[0].material_id, "report");
  await h.evaluate("sendMain()");
  assert.equal(h.context.requests.length, 1);
});

test("removing the last preparing attachment cancels an attachment-only send", async () => {
  const h = sendingSetup();
  h.input.value = "";
  pasteFiles(h, ["removed.png"]);
  const sending = h.evaluate("sendMain()");
  await settle();
  await attachmentAction(h, "remove", h.evaluate('[...composerAttachments("A").values()][0]'));
  await sending;
  assert.equal(h.context.requests.length, 0);
  h.context.uploads[0].resolve({ material_id: "removed", is_image: true });
  await settle();
  await h.evaluate("sendMain()");
  assert.equal(h.context.requests.length, 0);
});

test("attachment-only failure retains the image and retries the frozen empty message", async () => {
  const h = sendingSetup();
  h.input.value = "";
  pasteFiles(h, ["retry.png"]);
  const sending = h.evaluate("sendMain()");
  await settle();
  h.context.uploads[0].reject(new Error("网络断开"));
  await sending;
  assert.equal(h.context.requests.length, 0);
  assert.match(h.cards.innerHTML, /失败/);
  const retryUpload = attachmentAction(h, "retry", h.evaluate('[...composerAttachments("A").values()][0]'));
  h.context.uploads[1].resolve({ material_id: "retry", is_image: true });
  await retryUpload;
  h.evaluate(`api = async (_path, options) => { requests.push(options.body); throw new Error("Failed to fetch"); };`);
  await h.evaluate("sendMain()");
  assert.match(h.cards.innerHTML, /retry.png/);
  h.input.value = "下一条消息";
  h.evaluate('composerDraft.claim("A", "下一条消息")');
  h.evaluate(`api = async (_path, options) => { requests.push(options.body); return {run_id:"retry-run",message_id:"retry-message",status:"pending"}; };`);
  await h.evaluate("sendMain(true)");
  assert.equal(h.context.requests[0], h.context.requests[1]);
  assert.equal(JSON.parse(h.context.requests[1]).message, "");
  assert.equal(h.input.value, "下一条消息");
});

test("attachment-only queue delivery keeps its material inputs", async () => {
  const h = sendingSetup();
  h.input.value = "";
  h.evaluate(`state.materials.set("A", [{material_id:"image",is_image:true}]); toggleRunMaterial("image");
    api = async (_path, options) => { requests.push(JSON.parse(options.body)); return {input_id:"queued",status:"queued"}; };`);
  await h.evaluate('sendMain(false, "queue")');
  assert.equal(h.context.requests[0].mode, "queue");
  assert.equal(h.context.requests[0].run.message, "");
  assert.equal(h.context.requests[0].run.material_inputs[0].material_id, "image");
});

test("external file drops show feedback and use the existing batch upload and send path", async () => {
  const h = sendingSetup();
  h.input.value = "";
  const over = dragFiles(h, "dragover");
  assert.equal(over.defaultPrevented, true);
  assert.equal(over.dataTransfer.dropEffect, "copy");
  assert.equal(h.composer.classList.contains("is-file-dragover"), true);
  dragFiles(h, "dragleave", [], h.input, ["Files"], h.input);
  assert.equal(h.composer.classList.contains("is-file-dragover"), true);
  dragFiles(h, "dragleave");
  assert.equal(h.composer.classList.contains("is-file-dragover"), false);
  const dropped = [{ name: "report.txt", type: "text/plain" }, { name: "shot.png", type: "image/png" }];
  assert.equal(dragFiles(h, "drop", dropped).defaultPrevented, true);
  assert.match(h.cards.innerHTML, /report.txt/);
  assert.match(h.cards.innerHTML, /shot.png/);
  const sending = h.evaluate("sendMain()");
  await settle();
  h.context.uploads[0].resolve({ material_id: "report", relative_path: "report.txt" });
  await settle();
  assert.equal(h.context.requests.length, 0);
  h.context.uploads[1].resolve({ material_id: "shot", relative_path: "shot.png", is_image: true });
  await sending;
  assert.deepEqual(Array.from(h.context.requests[0].material_inputs, item => item.material_id), ["report", "shot"]);
});

test("file drops retain the originating task even when switching before upload starts", async () => {
  const h = sendingSetup();
  h.evaluate('runUiAction = action => { globalThis.startUploads = action; };');
  dragFiles(h, "drop", [{ name: "a.txt" }, { name: "b.png" }]);
  h.evaluate('state.activeTaskId = "B"; globalThis.paths = []; api = async (path, options) => { paths.push(path); return {material_id:options.body.file.name}; };');
  await h.evaluate("startUploads()");
  assert.deepEqual(Array.from(h.context.paths), ["/desktop/api/tasks/A/materials/upload", "/desktop/api/tasks/A/materials/upload"]);
  assert.equal(h.evaluate('materialSelection("A").bindings.length'), 2);
  assert.equal(h.evaluate('materialSelection("B").bindings.length'), 0);
});

test("plain text drags, outside drops and disabled composers do not upload files", () => {
  const h = sendingSetup();
  assert.equal(dragFiles(h, "dragover", [], h.input, ["text/plain"]).defaultPrevented, false);
  assert.equal(dragFiles(h, "drop", [], h.input, ["text/plain"]).defaultPrevented, false);
  assert.equal(dragFiles(h, "drop", [{ name: "outside.txt" }], createNode()).defaultPrevented, false);
  h.input.disabled = true;
  const over = dragFiles(h, "dragover");
  assert.equal(over.dataTransfer.dropEffect, "none");
  assert.equal(dragFiles(h, "drop", [{ name: "locked.txt" }]).defaultPrevented, true);
  assert.equal(h.context.uploads.length, 0);
});

test("attachments and quotation cards are included together with no typed text", async () => {
  const h = sendingSetup();
  h.input.value = "";
  h.evaluate(`addFileReference({path:"code.py", start_line:1, end_line:1, text:"check()"}, "A");
    setMainReference({task_id:"A", message_id:"answer", text:"answer", start:0, end:6, offset_basis:"rendered_text_utf16"});`);
  pasteFiles(h, ["shot.png"]);
  const sending = h.evaluate("sendMain()");
  await settle();
  h.context.uploads[0].resolve({ material_id: "shot", is_image: true });
  await sending;
  assert.match(h.context.requests[0].message, /check\(\)/);
  assert.equal(h.context.requests[0].message_reference.message_id, "answer");
  assert.equal(h.context.requests[0].material_inputs[0].material_id, "shot");
  assert.equal(h.evaluate('fileReferences("A").length'), 0);
  assert.equal(h.evaluate('referenceDrafts.get("A")'), null);
});

test("a compression command with an attachment sends the complete message", async () => {
  const h = sendingSetup();
  h.input.value = "@关键字快捷压缩 报错";
  pasteFiles(h, ["shot.png"]);
  const sending = h.evaluate("sendMain()");
  await settle();
  h.context.uploads[0].resolve({ material_id: "shot", is_image: true });
  await sending;
  assert.equal(h.context.requests[0].message, "@关键字快捷压缩 报错");
  assert.equal(h.context.requests[0].material_inputs[0].material_id, "shot");
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
  const input = createNode({ id: "fileInput", files: [{ name: "report.txt" }], value: "report.txt" });
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
  h.dispatch("change", { target: createNode({ id: "fileInput", files: [{ name: "report.txt" }] }) });
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
