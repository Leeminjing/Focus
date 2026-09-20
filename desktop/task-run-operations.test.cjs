/**
 * 本文件对外提供任务级 Run 操作状态的隔离回归测试。
 * 输入为两个 task、独立请求和 Run 事件；输出为互不污染的 pending/error/stream 快照。
 * 具体工作流为载入纯 store、并发变更两个 task、完成其中一个请求并验证另一个保持原状。
 * 示例：`node --test desktop/task-run-operations.test.cjs`。
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const context = { window: {} };
vm.createContext(context);
vm.runInContext(readFileSync(path.join(__dirname, "task-run-operations.js"), "utf8"), context);
const Operations = context.window.FocusTaskRunOperations;

test("submission state is isolated by task and request", () => {
  const store = Operations.create();
  store.begin("task-a", "request-a");
  store.begin("task-b", "request-b");
  store.accept("task-a", "request-a", { run_id: "run-a", status: "accepted" });
  assert.equal(store.get("task-a").submission.status, "accepted");
  assert.equal(store.get("task-b").submission.status, "pending");
  store.fail("task-b", "request-b", "network");
  assert.equal(store.get("task-a").error, null);
  assert.equal(store.get("task-b").error, "network");
});

test("stale completion cannot clear a newer request", () => {
  const store = Operations.create();
  store.begin("task-a", "request-old");
  store.begin("task-a", "request-new");
  store.fail("task-a", "request-old", "late");
  assert.equal(store.get("task-a").submission.request_id, "request-new");
  assert.equal(store.get("task-a").submission.status, "pending");
});

test("background Run updates stay with their owner while another task is pending", () => {
  const store = Operations.create();
  store.begin("task-b", "request-b");
  store.begin("task-a", "request-a");
  store.accept("task-a", "request-a", { run_id: "run-a", status: "accepted" });
  store.updateRun("task-a", "run-a", { status: "running" });
  assert.equal(store.get("task-a").active_run.status, "running");
  assert.equal(store.get("task-b").submission.request_id, "request-b");
  assert.equal(store.get("task-b").active_run, null);
});

test("renderer has no global Main Run lock and reports duplicate clicks", () => {
  const source = readFileSync(path.join(__dirname, "app.js"), "utf8");
  assert.doesNotMatch(source, /mainSubmitting/);
  assert.match(source, /这条消息正在提交/);
  assert.match(source, /taskRunOperations\?\.get\(taskId\)/);
  assert.match(source, /const ownerTaskId = run\.task_id/);
});
