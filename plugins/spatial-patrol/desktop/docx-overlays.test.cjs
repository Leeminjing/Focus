"use strict";

const assert = require("node:assert");

let resizeCallback;
let disconnected = false;
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.ResizeObserver = class {
  constructor(callback) { resizeCallback = callback; }
  observe() {}
  disconnect() { disconnected = true; }
};
globalThis.document = {
  createElement: () => ({ className: "", dataset: {}, style: {} }),
};
require("./docx-editor.js");

let layerLeft = 0;
const children = [];
const layer = {
  get innerHTML() { return ""; },
  set innerHTML(_value) { children.length = 0; },
  append: child => children.push(child),
  getBoundingClientRect: () => ({ left: layerLeft, top: 0 }),
};
const frame = { getBoundingClientRect: () => ({ left: 100, top: 50 }) };
const button = { addEventListener() {}, hidden: false };
const container = {
  innerHTML: "",
  querySelectorAll: selector => selector === "[data-docx-open]" ? [button, button] : [],
  querySelector(selector) {
    if (selector === "[data-docx-overlays]") return layer;
    if (selector === 'iframe[name="frameEditor"]') return frame;
    return button;
  },
};

const editor = globalThis.FocusDocxEditor;
editor.mount(
  container,
  { relative_path: "rich.docx" },
  { activeTaskId: "task", tasks: [{ task_id: "task" }] },
);

const kinds = ["text_range", "paragraph", "table", "cell", "image", "shape", "header", "footer", "page_region"];
for (const [index, kind] of kinds.entries()) {
  editor._test.projectionMessage({ data: {
    type: "focus-docx-anchors",
    session_id: null,
    projections: [{
      spatial_id: `spatial-${index}`,
      target_id: `${kind}:${index}`,
      page: 1,
      viewport_rect: { x: index * 5, y: index * 4, width: 20, height: 15 },
    }],
  } });
}
assert.strictEqual(children.length, kinds.length, "all semantic anchor kinds render simultaneously");
assert.deepStrictEqual(children.map(item => item.dataset.targetId), kinds.map((kind, index) => `${kind}:${index}`));
const beforeResize = children[0].style.left;
layerLeft = 25;
resizeCallback();
assert.notStrictEqual(children[0].style.left, beforeResize, "panel resize recalculates iframe-relative overlay position");

editor.destroy();
assert.ok(disconnected, "overlay ResizeObserver is disconnected with the plugin panel");
console.log("docx-overlays: multi-kind overlays and resize refresh passed");
