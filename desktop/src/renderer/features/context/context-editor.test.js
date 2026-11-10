const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

global.window = global;
vm.runInThisContext(fs.readFileSync(require.resolve("./context-editor.js"), "utf8"));

const editor = global.FocusContextEditor;
const original = [{ role: "tool", content: "关注结果", tool_call_id: "call-1", name: "search" }];
const clone = editor.cloneMessages(original);
clone[0].content = "changed";
assert.equal(original[0].content, "关注结果");

const messages = [{ role: "human", content: "A" }, { role: "ai", content: "B" }];
editor.move(messages, 1, 0);
assert.equal(messages[0].role, "ai");
const uiKeys = editor.createUiKeys([{ id: "same" }, { id: "same" }, {}], (() => {
  let value = 0;
  return () => `ui-${value += 1}`;
})());
assert.deepEqual(uiKeys, ["ui-1", "ui-2", "ui-3"]);
const rendered = editor.renderMessages(original, ["ui-1"]);
assert.doesNotMatch(rendered, /data-context-message-json/);
assert.doesNotMatch(rendered, /draggable="true"/);
assert.match(rendered, /data-context-pointer-handle/);
assert.match(rendered, /data-action="context-message-toggle"/);
assert.doesNotMatch(rendered, /context-message-(?:up|down)/);
const expanded = editor.renderMessages(original, ["ui-1"], "ui-1");
assert.match(expanded, /tool_call_id/);
assert.match(expanded, /data-context-message-content/);
assert.doesNotMatch(expanded, /data-context-message-json/);
assert.match(rendered, /data-action="context-message-duplicate"[^>]*>创建副本/);
assert.doesNotMatch(rendered, /<header>[\s\S]*?tool_call_id[\s\S]*?<\/header>/);
const jsonExpanded = editor.renderMessages(original, ["ui-1"], "ui-1", false, "json");
assert.match(jsonExpanded, /data-context-message-json/);
assert.doesNotMatch(jsonExpanded, /data-context-message-content/);
const sourceRendered = editor.renderSourceMessages(original, "root");
assert.match(sourceRendered, /data-context-source-index="0"/);
assert.match(sourceRendered, /data-action="context-source-copy"/);
assert.doesNotMatch(sourceRendered, /textarea/);

const parsed = editor.readMessages({
  querySelectorAll(selector) {
    return selector === "[data-context-message-json]" ? [{ value: JSON.stringify(original[0]), dataset: { contextMessageIndex: "0" } }] : [];
  },
}, original);
assert.deepEqual(parsed, original);
assert.throws(
  () => editor.readMessages({ querySelectorAll: selector => selector === "[data-context-message-json]" ? [{ value: "not json", dataset: { contextMessageIndex: "0" } }] : [] }, original),
  /消息 1 不是合法 JSON/,
);

const structured = [{
  role: "ai", content: [
    { type: "text", text: "first", annotations: [{ unknown: true }] },
    { type: "image_url", image_url: { url: "data:image/png;base64,preserve", detail: "high" } },
    { type: "output_text", text: "last", extension: [1, 2] },
    { type: "future_type", text: "do not edit", extra: { exact: true } },
  ], tool_calls: [{ id: "call", function: { name: "search", arguments: '{"q":"exact"}' } }],
  additional_kwargs: { nested: { unknown: [null, false, 0] } },
}];
const bodyEdited = editor.readMessages({ querySelectorAll: selector => selector === "[data-context-message-content]" ? [
  { value: "edited first", dataset: { contextMessageIndex: "0", contextPartIndex: "0" } },
  { value: "edited last", dataset: { contextMessageIndex: "0", contextPartIndex: "2" } },
] : [] }, structured);
const expectedBody = editor.cloneMessages(structured);
expectedBody[0].content[0].text = "edited first";
expectedBody[0].content[2].text = "edited last";
assert.deepEqual(bodyEdited, expectedBody);
assert.equal(structured[0].content[0].text, "first");
assert.deepEqual(editor.readMessages({ querySelectorAll: () => [] }, structured), structured);
const structuredMarkup = editor.renderMessages(structured, ["mixed"], "mixed");
assert.equal((structuredMarkup.match(/data-context-message-content/g) || []).length, 2);
assert.doesNotMatch(structuredMarkup.match(/class="context-message-preview">(.*?)<\/span>/)[1], /base64|future_type|tool_calls/);
const jsonEdited = editor.cloneMessages(structured);
jsonEdited[0].additional_kwargs.future = { version: 3 };
assert.deepEqual(editor.readMessages({ querySelectorAll: selector => selector === "[data-context-message-json]" ? [
  { value: JSON.stringify(jsonEdited[0]), dataset: { contextMessageIndex: "0" } },
] : [] }, structured), jsonEdited);

const taskRoot = { task_id: "root", workspace_id: "workspace" };
const taskChild = { task_id: "child", workspace_id: "workspace" };
const taskBlocked = { task_id: "blocked", workspace_id: "workspace" };
const taskLegacy = { task_id: "legacy", workspace_id: "workspace" };
const taskUnrelated = { task_id: "unrelated", workspace_id: "workspace" };
const taskUnrelatedChild = { task_id: "unrelated-child", workspace_id: "workspace" };
const trees = new Map([["workspace", [
  { context_id: "child", parents: [{ context_id: "root" }] },
  { context_id: "blocked", parents: [{ context_id: "root" }] },
  { context_id: "root", parents: [] },
  { context_id: "unrelated", parents: [] },
  { context_id: "unrelated-child", parents: [{ context_id: "unrelated" }] },
]]]);
assert.deepEqual(
  editor.orderTasksByTree([taskBlocked, taskChild, taskLegacy, taskUnrelatedChild, taskUnrelated, taskRoot], trees).map(task => task.task_id),
  ["root", "child", "blocked", "unrelated", "unrelated-child", "legacy"],
);
assert.deepEqual(
  editor.contextFamilyTasks(
    [{ task_id: "other", workspace_id: "other" }, taskBlocked, taskChild, taskLegacy, taskUnrelatedChild, taskUnrelated, taskRoot],
    trees,
    "child",
  ).map(task => task.task_id),
  ["root", "child", "blocked"],
);
assert.deepEqual(
  editor.contextFamilyTasks([taskLegacy, taskRoot], trees, "legacy").map(task => task.task_id),
  ["legacy"],
);

console.log("context editor checks passed");
