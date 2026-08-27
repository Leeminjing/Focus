"use strict";

const assert = require("node:assert/strict");

const windowListeners = new Map();
globalThis.addEventListener = (type, listener) => windowListeners.set(type, listener);
globalThis.removeEventListener = type => windowListeners.delete(type);
globalThis.confirm = () => true;
globalThis.focusDesktop = { runtime: () => ({ session: "test-session" }) };

const nodes = new Map();
const nodeFor = selector => {
  if (!nodes.has(selector)) {
    nodes.set(selector, {
      addEventListener(type, listener) { this.listeners[type] = listener; },
      dataset: {}, hidden: false, id: "", innerHTML: "", listeners: {}, textContent: "",
    });
  }
  return nodes.get(selector);
};
const openButtons = ["view", "edit"].map(mode => ({
  dataset: { docxOpen: mode }, listeners: {},
  addEventListener(type, listener) { this.listeners[type] = listener; },
}));
const container = {
  innerHTML: "",
  querySelectorAll: selector => selector === "[data-docx-open]" ? openButtons : [],
  querySelector: selector => nodeFor(selector),
};

let editorConfig;
globalThis.DocsAPI = {
  DocEditor: function DocEditor(_hostId, config) {
    editorConfig = config;
    this.destroyEditor = () => {};
  },
};
globalThis.fetch = async (url, options = {}) => {
  if (String(url).endsWith("/sessions") && options.method === "POST") {
    return {
      ok: true,
      json: async () => ({
        session: { session_id: "session-a" },
        document_server_url: "http://127.0.0.1:18080",
        config: {},
      }),
    };
  }
  return { ok: true, json: async () => [] };
};

require("./docx-editor.js");

(async () => {
  const focusState = { mainUiUsable: true, mainAgentUsable: true, otherPluginUsable: true };
  globalThis.FocusDocxEditor.mount(
    container,
    { relative_path: "rich.docx" },
    { activeTaskId: "task-a", tasks: [{ task_id: "task-a" }] },
  );
  await openButtons[1].listeners.click();

  assert.equal(typeof editorConfig.events.onError, "function");
  editorConfig.events.onError({ data: { errorDescription: "编辑器连接已断开" } });

  assert.match(nodeFor("[data-docx-stage]").innerHTML, /DOCX 编辑插件不可用/);
  assert.match(nodeFor("[data-docx-stage]").innerHTML, /编辑器连接已断开/);
  assert.equal(nodeFor("[data-docx-status]").textContent, "插件不可用");
  assert.equal(nodeFor("[data-docx-status]").dataset.tone, "danger");
  assert.deepEqual(focusState, {
    mainUiUsable: true,
    mainAgentUsable: true,
    otherPluginUsable: true,
  });

  globalThis.FocusDocxEditor.destroy();
  console.log("docx-fault-isolation: editor disconnect stays inside the plugin panel");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
