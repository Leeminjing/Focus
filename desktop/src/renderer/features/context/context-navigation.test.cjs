const assert = require("node:assert/strict");
const navigation = require("./context-navigation.js");

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
  navigation.orderTasksByTree([taskBlocked, taskChild, taskLegacy, taskUnrelatedChild, taskUnrelated, taskRoot], trees).map(task => task.task_id),
  ["root", "child", "blocked", "unrelated", "unrelated-child", "legacy"],
);
assert.deepEqual(
  navigation.contextFamilyTasks(
    [{ task_id: "other", workspace_id: "other" }, taskBlocked, taskChild, taskLegacy, taskUnrelatedChild, taskUnrelated, taskRoot],
    trees,
    "child",
  ).map(task => task.task_id),
  ["root", "child", "blocked"],
);
assert.deepEqual(
  navigation.contextFamilyTasks([taskLegacy, taskRoot], trees, "legacy").map(task => task.task_id),
  ["legacy"],
);
