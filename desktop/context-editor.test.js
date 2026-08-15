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
assert.match(expanded, /data-context-message-json/);
const sourceRendered = editor.renderSourceMessages(original, "root");
assert.match(sourceRendered, /data-context-source-index="0"/);
assert.match(sourceRendered, /data-action="context-source-copy"/);
assert.doesNotMatch(sourceRendered, /textarea/);

const parsed = editor.readMessages({
  querySelectorAll() {
    return [{ value: JSON.stringify(original[0]), dataset: { contextMessageIndex: "0" } }];
  },
}, original);
assert.deepEqual(parsed, original);
assert.throws(
  () => editor.readMessages({ querySelectorAll: () => [{ value: "not json", dataset: { contextMessageIndex: "0" } }] }, original),
  /消息 1 不是合法 JSON/,
);

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
