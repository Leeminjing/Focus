"use strict";

const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
let removed = 0;
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => { removed += 1; };
require("./docx-editor.js");

const editor = globalThis.FocusDocxEditor;
assert.deepStrictEqual(
  editor._test.sessionPresentation({ status: "saved", dirty: false }),
  { message: "已保存到磁盘", tone: "success" },
);
assert.deepStrictEqual(
  editor._test.sessionPresentation({ status: "recoverable", dirty: true }),
  { message: "存在可恢复版本", tone: "warning" },
);
assert.deepStrictEqual(
  editor._test.sessionPresentation({ status: "saved", last_error: "save failed" }),
  { message: "save failed", tone: "danger" },
);
assert.deepStrictEqual(
  editor._test.sessionPresentation({ status: "editing", callback_status: 6, dirty: false }),
  { message: "已保存到磁盘", tone: "success" },
);
assert.strictEqual(
  editor._test.saveEvidenceAdvanced(
    { saved_hash: "before", document_version: 1, dirty: false },
    { saved_hash: "before", document_version: 1 },
  ),
  false,
);
assert.strictEqual(
  editor._test.saveEvidenceAdvanced(
    { saved_hash: "after", document_version: 2, dirty: false },
    { saved_hash: "before", document_version: 1 },
  ),
  true,
);
assert.strictEqual(
  editor._test.saveEvidenceAdvanced(
    { saved_hash: "after", document_version: 2, dirty: true },
    { saved_hash: "before", document_version: 1 },
  ),
  false,
);

const listeners = [];
const node = {
  addEventListener: (...args) => listeners.push(args),
  dataset: {},
  hidden: false,
  textContent: "",
};
const container = {
  innerHTML: "",
  querySelectorAll: selector => selector === "[data-docx-open]" ? [node, node] : [],
  querySelector: () => node,
};
editor.mount(
  container,
  { relative_path: "rich.docx" },
  { activeTaskId: "task", tasks: [{ task_id: "task" }] },
);
assert.match(container.innerHTML, /Word 式编辑器/);
assert.doesNotMatch(container.innerHTML, /data-docx-anchor hidden/);
assert.ok(listeners.length >= 4);
assert.strictEqual(typeof editor._test.createAnchor, "function");
editor._test.createAnchor();
assert.strictEqual(node.textContent, "请先打开文档");

const css = fs.readFileSync(path.join(__dirname, "docx-editor.css"), "utf8");
assert.match(
  css,
  /\.focus-docx-editor \[hidden\]\s*\{[^}]*display:\s*none\s*!important/,
);
editor.destroy();
assert.strictEqual(container.innerHTML, "");
assert.strictEqual(removed, 2);

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

function editorContainer(name) {
  const nodes = new Map();
  const nodeFor = selector => {
    if (!nodes.has(selector)) {
      nodes.set(selector, {
        addEventListener(event, callback) { this.listeners[event] = callback; },
        dataset: {}, hidden: false, id: "", innerHTML: "", listeners: {},
        textContent: "",
      });
    }
    return nodes.get(selector);
  };
  const host = nodeFor("[data-docx-host]");
  const mode = nodeFor("[data-docx-mode]");
  const save = nodeFor("[data-docx-save]");
  const anchor = nodeFor("[data-docx-anchor]");
  const close = nodeFor("[data-docx-close]");
  const status = nodeFor("[data-docx-status]");
  const stage = nodeFor("[data-docx-stage]");
  const overlays = nodeFor("[data-docx-overlays]");
  overlays.children = [];
  Object.defineProperty(overlays, "innerHTML", {
    get: () => "",
    set: () => { overlays.children = []; },
    configurable: true,
  });
  overlays.append = child => overlays.children.push(child);
  overlays.getBoundingClientRect = () => ({ left: 0, top: 0 });
  const picker = nodeFor("[data-docx-anchor-picker]");
  picker.hidden = true;
  const frame = { getBoundingClientRect: () => ({ left: 100, top: 80, right: 900, bottom: 680 }) };
  host.querySelector = () => null;
  const open = ["view", "edit"].map(modeName => ({
    dataset: { docxOpen: modeName }, listeners: {},
    addEventListener(event, callback) { this.listeners[event] = callback; },
  }));
  return {
    name, innerHTML: "", hostAvailable: true,
    host, mode, save, anchor, close, status, stage, overlays, picker, frame, open,
    querySelectorAll: selector => selector === "[data-docx-open]" ? open : [],
    querySelector(selector) {
      if (selector === "[data-docx-host]") return this.hostAvailable ? host : null;
      if (selector === "[data-docx-host] iframe") return null;
      if (selector === 'iframe[name="frameEditor"]') return frame;
      return nodes.get(selector) || null;
    },
  };
}

async function mountingRaceDoesNotCrossEditorInstances() {
  const firstSession = deferred();
  const secondSession = deferred();
  const sessionRequests = [firstSession, secondSession];
  globalThis.fetch = async (url, options = {}) => {
    if (String(url).endsWith("/sessions") && options.method === "POST") {
      return sessionRequests.shift().promise;
    }
    return { ok: true, json: async () => [] };
  };
  const containers = [];
  const created = [];
  globalThis.DocsAPI = {
    DocEditor: function DocEditor(hostId) {
      const owner = containers.find(item => item.host.id === hostId);
      assert.ok(owner, `editor host ${hostId} must belong to a mounted container`);
      owner.hostAvailable = false;
      created.push(owner.name);
      this.destroyEditor = () => {};
    },
  };
  const response = sessionId => ({
    ok: true,
    json: async () => ({
      session: { session_id: sessionId },
      document_server_url: "http://127.0.0.1:18080",
      config: {},
    }),
  });
  const appState = { activeTaskId: "task", tasks: [{ task_id: "task" }] };
  const first = editorContainer("first");
  const second = editorContainer("second");
  containers.push(first, second);
  editor.mount(first, { relative_path: "rich.docx" }, appState);
  const firstOpen = first.open[1].listeners.click();
  editor.mount(second, { relative_path: "rich.docx" }, appState);
  const secondOpen = second.open[1].listeners.click();
  firstSession.resolve(response("session-first"));
  await new Promise(resolve => setImmediate(resolve));
  secondSession.resolve(response("session-second"));
  await Promise.all([firstOpen, secondOpen]);

  assert.deepStrictEqual(created, ["second"]);
  assert.doesNotMatch(second.stage.innerHTML, /Cannot set properties of null/);
  editor.destroy();
}

async function forceSaveWaitsForCommittedEvidence() {
  const appState = { activeTaskId: "task", tasks: [{ task_id: "task" }] };
  const target = editorContainer("save-evidence");
  const originalSetInterval = globalThis.setInterval;
  globalThis.setInterval = () => null;
  let sessionReads = 0;
  let editorEvents;
  globalThis.fetch = async (url, options = {}) => {
    const value = String(url);
    if (value.endsWith("/sessions") && options.method === "POST") {
      return {
        ok: true,
        json: async () => ({
          session: { session_id: "session-save" },
          document_server_url: "http://127.0.0.1:18080",
          config: {},
        }),
      };
    }
    if (value.endsWith("/anchors")) return { ok: true, json: async () => [] };
    if (value.endsWith("/force-save")) return { ok: true, json: async () => ({ accepted: true }) };
    if (value.endsWith("/sessions/session-save")) {
      sessionReads += 1;
      return {
        ok: true,
        json: async () => sessionReads < 3
          ? { saved_hash: "before", document_version: 1, dirty: true, status: "dirty" }
          : { saved_hash: "after", document_version: 2, dirty: false, status: "editing", callback_status: 6 },
      };
    }
    throw new Error(`unexpected request: ${value}`);
  };
  globalThis.DocsAPI = {
    DocEditor: function DocEditor(_hostId, config) {
      editorEvents = config.events;
      this.destroyEditor = () => {};
    },
  };
  try {
    editor.mount(target, { relative_path: "rich.docx" }, appState);
    await target.open[1].listeners.click();
    const saving = target.save.listeners.click();
    await new Promise(resolve => setImmediate(resolve));
    assert.strictEqual(target.status.textContent, "正在保存…");
    await saving;
    assert.strictEqual(target.status.textContent, "已保存到磁盘");
    assert.strictEqual(target.status.dataset.tone, "success");
    editorEvents.onDocumentStateChange({ data: false });
    assert.strictEqual(target.status.textContent, "已保存到磁盘");
    editorEvents.onDocumentStateChange({ data: true });
    assert.strictEqual(target.status.textContent, "编辑器内有未保存更改");
  } finally {
    editor.destroy();
    globalThis.setInterval = originalSetInterval;
  }
}

async function anchorButtonWaitsForAnExactDocumentClick() {
  const appState = { activeTaskId: "task", tasks: [{ task_id: "task" }] };
  const target = editorContainer("anchor-point");
  const originalSetInterval = globalThis.setInterval;
  const originalDocument = globalThis.document;
  globalThis.setInterval = () => null;
  globalThis.document = {
    createElement() {
      return { className: "", dataset: {}, style: {} };
    },
  };
  const requests = [];
  globalThis.fetch = async (url, options = {}) => {
    const value = String(url);
    if (value.endsWith("/sessions") && options.method === "POST") {
      return {
        ok: true,
        json: async () => ({
          session: { session_id: "session-anchor" },
          document_server_url: "http://127.0.0.1:18080",
          config: {},
        }),
      };
    }
    if (value.endsWith("/anchors") && !options.method) return { ok: true, json: async () => [] };
    if (value.endsWith("/anchors") && options.method === "POST") {
      requests.push(JSON.parse(options.body));
      const ordinal = requests.length;
      return {
        ok: true,
        json: async () => ({
          spatial_id: ordinal === 1 ? "spatial-one" : "spatial-two",
          region: {
            target: { target_id: "paragraph:shared" },
            projection: { page: 1, rect: { x: .25, y: .5, width: 0, height: 0 } },
          },
          viewport_projection: {
            target_id: "paragraph:shared", page: 1, point: { x: .25, y: .5 },
            viewport_rect: { x: 200 + ordinal * 10, y: 300 + ordinal * 10, width: 0, height: 0 },
          },
        }),
      };
    }
    throw new Error(`unexpected request: ${value}`);
  };
  globalThis.DocsAPI = {
    DocEditor: function DocEditor() { this.destroyEditor = () => {}; },
  };

  try {
    editor.mount(target, { relative_path: "rich.docx" }, appState);
    await target.open[1].listeners.click();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepStrictEqual(requests, [], "opening and pressing the button must not persist an anchor");

    target.anchor.listeners.click();
    assert.strictEqual(target.picker.hidden, false);
    assert.strictEqual(target.status.textContent, "请点击文档中的锚点位置");
    assert.deepStrictEqual(requests, []);

    await target.picker.listeners.click({ clientX: 300, clientY: 380 });
    assert.deepStrictEqual(requests, [{ viewport_x: 200, viewport_y: 300 }]);
    assert.strictEqual(target.picker.hidden, true);
    assert.strictEqual(target.status.textContent, "已建立空间锚点 spati");
    assert.strictEqual(target.overlays.children.length, 1);
    assert.strictEqual(target.overlays.children[0].dataset.spatialId, "spatial-one");

    target.anchor.listeners.click();
    await target.picker.listeners.click({ clientX: 360, clientY: 440 });
    assert.deepStrictEqual(requests[1], { viewport_x: 260, viewport_y: 360 });
    assert.strictEqual(target.overlays.children.length, 2);
    assert.deepStrictEqual(
      target.overlays.children.map(marker => marker.dataset.spatialId),
      ["spatial-one", "spatial-two"],
      "anchors on the same semantic target must remain independent by spatial_id",
    );
    editor._test.projectionMessage({ data: {
      type: "focus-docx-projection", session_id: "session-anchor",
      projection: { target_id: "paragraph:shared", page: 1,
        viewport_rect: { x: 80, y: 90, width: 0, height: 0 } },
    } });
    assert.strictEqual(target.overlays.children.length, 2,
      "caret notifications must not create a third, unpersisted anchor");
    editor._test.projectionMessage({ data: {
      type: "focus-docx-anchors", session_id: "session-anchor",
      projections: [
        { spatial_id: "spatial-one", target_id: "paragraph:shared", page: 1,
          viewport_rect: { x: 80, y: 90, width: 0, height: 0 } },
        { spatial_id: "spatial-two", target_id: "paragraph:shared", page: 1,
          viewport_rect: { x: 120, y: 130, width: 0, height: 0 } },
      ],
    } });
    assert.deepStrictEqual(target.overlays.children.map(marker => marker.style.top), ["170px", "210px"],
      "scroll projections must update each spatial identity independently");
    editor._test.projectionMessage({ data: {
      type: "focus-docx-anchors", session_id: "session-anchor",
      projections: [{ spatial_id: "spatial-one", projection_unavailable: true }],
    } });
    assert.deepStrictEqual(target.overlays.children.map(marker => marker.dataset.spatialId), ["spatial-two"],
      "an unavailable projection must not retain a stale on-screen marker");
  } finally {
    editor.destroy();
    globalThis.setInterval = originalSetInterval;
    globalThis.document = originalDocument;
  }
}

mountingRaceDoesNotCrossEditorInstances().then(forceSaveWaitsForCommittedEvidence).then(anchorButtonWaitsForAnExactDocumentClick).then(() => {
  console.log("docx-editor: state presentation, cleanup, and mount ownership passed");
}).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
