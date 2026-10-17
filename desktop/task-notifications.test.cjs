"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { TaskNotifications } = require("./task-notifications.cjs");

function fixture(stored = {}) {
  let tasks = [], saved = stored;
  const shown = [], updates = [];
  const service = new TaskNotifications({ load: () => saved, save: value => { saved = value; },
    listTasks: async () => tasks, show: value => shown.push(value), onTasks: value => updates.push(value) });
  return { service, shown, updates, set: value => { tasks = value; }, saved: () => saved };
}
const task = (id, status, extra = {}) => ({ task_id: id, title: `任务 ${id}`, latest_run: { run_id: id, status }, ...extra });

test("initial results stay quiet; new results notify after attention and only once", async () => {
  const f = fixture();
  f.set([task("old", "success")]);
  await f.service.poll();
  assert.equal(f.shown.length, 0);
  f.set([task("old", "success"), task("done", "success"), task("failed", "error"),
    task("waiting", "interrupted", { attention: [{ id: "approval", kind: "access_approval" }] })]);
  await f.service.poll();
  assert.deepEqual(f.shown.map(item => item.title), ["需要你授权", "任务已完成", "任务运行失败"]);
  assert.equal(f.shown[0].target.taskId, "waiting");
  assert.equal(f.shown[0].target.attentionId, "approval");
  await f.service.poll();
  assert.equal(f.shown.length, 3);
  assert.equal(f.updates.length, 3);
});

test("restart deduplicates attention; settings persist and muted results do not replay", async () => {
  const f = fixture();
  const waiting = task("waiting", "interrupted", { attention: [{ id: "approval", kind: "access_approval", loop_id: "loop" }] });
  f.set([waiting]);
  await f.service.poll();
  assert.equal(f.shown[0].target.loopId, "loop");
  f.service.configure(false);
  const restart = fixture(f.saved());
  assert.equal(restart.service.settings().enabled, false);
  restart.set([waiting]);
  await restart.service.poll();
  restart.set([waiting, task("muted", "error")]);
  await restart.service.poll();
  restart.service.configure(true);
  await restart.service.poll();
  assert.equal(restart.shown.length, 0);
});

test("archived tasks and ongoing loops do not emit misleading completion", async () => {
  const f = fixture();
  await f.service.poll();
  f.set([task("archived", "success", { lifecycle: "archived" }),
    task("loop", "success", { loop: { loop_id: "loop", status: "running" } }),
    task("active", "success", { active_run: { status: "running" } })]);
  await f.service.poll();
  assert.equal(f.shown.length, 0);
  f.set([task("loop", "success", { loop: { loop_id: "loop", status: "completed" } })]);
  await f.service.poll();
  assert.equal(f.shown.length, 1);
});

test("failed polling preserves baseline and overlapping polls never duplicate", async () => {
  const f = fixture();
  f.service.listTasks = async () => { throw new Error("offline"); };
  await assert.rejects(f.service.poll(), /offline/);
  let release;
  f.service.listTasks = () => new Promise(resolve => { release = resolve; });
  const pending = f.service.poll();
  await f.service.poll();
  release([task("old", "success")]);
  await pending;
  assert.equal(f.shown.length, 0);
});
