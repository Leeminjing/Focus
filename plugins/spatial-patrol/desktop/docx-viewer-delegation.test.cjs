"use strict";

const assert = require("node:assert");
let mounted = null;
globalThis.FocusDocxEditor = {
  mount: (...args) => { mounted = args; return true; },
};
require("./viewer.js");

const container = { innerHTML: "" };
const material = { relative_path: "complex.docx" };
const appState = { activeTaskId: "task", tasks: [{ task_id: "task" }] };
globalThis.FocusSpatialViewer.mountPanel(container, material, appState);
assert.ok(mounted, "DOCX must delegate to FocusDocxEditor");
assert.strictEqual(mounted[0], container);
assert.strictEqual(mounted[1], material);

console.log("docx-viewer-delegation: DOCX bypasses text extraction");
