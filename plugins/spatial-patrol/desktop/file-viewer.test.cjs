/* Removing spatial controls must preserve shared previews and the DOCX editor. */
"use strict";

const assert = require("node:assert/strict");
require("./viewer.js");
require("./entry.js");

const calls = [];
let closeListener;
const previewHost = {};
const container = {
  innerHTML: "",
  querySelector: selector => selector === "[data-viewer-close]"
    ? { addEventListener: (_, listener) => { closeListener = listener; } } : previewHost,
};
const appState = { activeTaskId: "task", tasks: [{ task_id: "task" }] };
globalThis.FocusFilePreview = { mount: (host, options) => calls.push({ host, options }) };
globalThis.FocusDocxEditor = {
  destroy: () => calls.push("destroy"),
  mount: (...args) => calls.push({ editor: args }),
};
let closed = false;
globalThis.__focusCloseFilePanel = () => { closed = true; };
const reference = () => {};
const view = globalThis.__focusPluginViews["spatial-viewer"];

for (const name of ["image.png", "document.pdf", "notes.md", "legacy.doc", "app.py"]) {
  view.mountPanel(container, { relative_path: name }, appState, { onReference: reference });
  const result = calls.at(-1);
  assert.equal(result.host, previewHost);
  assert.deepEqual(result.options, { name, target: { task_id: "task", content_ref: name }, onReference: reference });
}
closeListener();
assert.equal(closed, true);
view.mountPanel(container, { relative_path: "edit.docx" }, appState);
assert.equal(calls.at(-1).editor[1].relative_path, "edit.docx");
assert.equal(calls.at(-1).editor[2], appState);
assert.equal(view.getFocus, undefined);
assert.equal(view.sendFocusedMessage, undefined);
console.log("file-viewer: shared previews and DOCX editor routing passed");
