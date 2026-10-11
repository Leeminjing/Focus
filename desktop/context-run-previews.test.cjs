/* 本文件验证 Context Run 只读观察器的宿主接入与资源生命周期。
 * 输入为真实 Run GET/SSE 形状、可控异步查询和帧调度；输出为身份隔离、终态收敛、重试、订阅释放及通知合并断言。
 * 工作流模拟切区/换 Run/历史节点、断线查询和旧投影重放，不启动任务或审批；示例：node --test desktop/context-run-previews.test.cjs。
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const { create } = require("./context-run-previews.js");
const run = (id = "r", patch = {}) => ({ run_id: id, task_id: "c", workspace_anchor: { workspace_id: "w" }, execution_thread_id: "thread", message_id: "input", loop_id: "loop", status: "running", ...patch });
const node = (runId = "r", patch = {}) => ({ context_id: "c", current_revision_id: "rev", latest_run: { run_id: runId, status: "running" }, ...patch });
const owner = (nodes = [node()], patch = {}) => ({ workspaceId: "w", loopId: "loop", nodes, ...patch });
const envelope = (id, data, extra = {}) => ({ type: "tokens", id: String(id), data: JSON.stringify({ run_id: "r", workspace_id: "w", thread_id: "thread", data, ...extra }) });
const settled = (status = "success", runId = "r") => ({ type: "end", id: "end", data: JSON.stringify({ run_id: runId, status }) });
const tick = async () => { await Promise.resolve(); await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
function fixture(load = async id => run(id)) {
  const subscriptions = [], queries = [], notifications = [], frames = new Map();
  let frameId = 0;
  const api = create({
    loadRun(id, signal) { queries.push({ id, signal }); return load(id, signal); },
    streams: { subscribe(id, callbacks) { const record = { id, callbacks, closed: false }; subscriptions.push(record); return { close() { record.closed = true; } }; } },
    onChange(previews, structural) { notifications.push({ previews, structural }); },
    schedule(callback) { frames.set(++frameId, callback); return frameId; },
    cancel(id) { frames.delete(id); },
  });
  function flush() { const pending = [...frames.values()]; frames.clear(); pending.forEach(callback => callback()); }
  return { api, subscriptions, queries, notifications, frames, flush };
}

test("only current roots with running Runs load and identity mismatch never subscribes", async () => {
  const f = fixture();
  f.api.sync(owner([node("historic", { historical: true }), node("pending", { context_id: "pending", latest_run: { run_id: "pending", status: "pending" } })]));
  await tick();
  assert.equal(f.queries.length, 0);
  for (const patch of [{ run_id: "other" }, { task_id: "other" }, { workspace_anchor: { workspace_id: "other" } }, { loop_id: "other" }, { execution_thread_id: null }]) {
    const invalid = fixture(async () => run("r", patch));
    invalid.api.sync(owner());
    await tick();
    assert.equal(invalid.subscriptions.length, 0);
    assert.equal(invalid.api.get().c.status, "unavailable");
    assert.match(invalid.api.get().c.notice, /身份不匹配/);
  }
});

test("late GET after workspace switch or Run replacement cannot attach or mutate a new preview", async () => {
  const old = deferred(), next = deferred();
  const f = fixture(id => id === "r" ? old.promise : next.promise);
  f.api.sync(owner());
  f.api.sync(owner([node("new")], { workspaceId: "next", loopId: "next-loop" }));
  assert.equal(f.queries[0].signal.aborted, true);
  old.resolve(run());
  await tick();
  assert.equal(f.subscriptions.length, 0);
  next.resolve(run("new", { workspace_anchor: { workspace_id: "next" }, loop_id: "next-loop" }));
  await tick();
  assert.deepEqual(f.subscriptions.map(item => item.id), ["new"]);
  const sub = f.subscriptions[0];
  f.api.sync(owner([node("third")], { workspaceId: "next", loopId: "next-loop" }));
  assert.equal(sub.closed, true);
  sub.callbacks.onFrame(envelope(1, { message_id: "old", content: "late" }));
  assert.equal(f.api.get().c.run_id, "third");
  assert.equal(f.api.get().c.text, "");
});

test("bursty content batches one notification without structural graph changes", async () => {
  const f = fixture();
  f.api.sync(owner());
  await tick();
  f.flush();
  const source = f.subscriptions[0];
  for (let id = 1; id <= 20; id++) source.callbacks.onFrame(envelope(id, { message_id: "message", content: "字" }));
  assert.equal(f.frames.size, 1);
  f.flush();
  assert.equal(f.notifications.length, 2);
  assert.equal(f.notifications[1].structural, false);
  assert.equal(f.notifications[1].previews.c.text, "字".repeat(20));
  f.api.stop();
  source.callbacks.onFrame(envelope(21, { message_id: "message", content: "迟到" }));
  assert.deepEqual(f.api.get(), {});
  assert.equal(f.frames.size, 0);
});

test("stream end and terminal metadata release, clear content and cannot be re-opened by decorated or stale projection", async () => {
  for (const event of [settled(), { ...envelope(2, { status: "interrupted" }), type: "metadata" }]) {
    const f = fixture();
    f.api.sync(owner());
    await tick();
    f.subscriptions[0].callbacks.onFrame(envelope(1, { message_id: "m", content: "输出" }));
    f.subscriptions[0].callbacks.onFrame(event);
    assert.equal(f.api.get().c.status, "ended");
    assert.equal(f.api.get().c.text, "");
    assert.equal(f.subscriptions[0].closed, true);
    const decorated = f.api.decorate({ nodes: [node()] });
    assert.equal(decorated.nodes[0].latest_run.status, event.type === "end" ? "success" : "interrupted");
    f.api.sync(owner(decorated.nodes));
    f.api.sync(owner());
    await tick();
    assert.equal(f.queries.length, 1);
    assert.equal(f.subscriptions.length, 1);
    f.flush();
    assert.equal(f.notifications.at(-1).structural, true);
  }
});

test("end belonging to another Run does not settle this Context", async () => {
  const f = fixture();
  f.api.sync(owner());
  await tick();
  f.subscriptions[0].callbacks.onFrame(settled("success", "other"));
  assert.equal(f.api.get().c.ended, false);
  assert.equal(f.subscriptions[0].closed, false);
  assert.equal(f.api.decorate({ nodes: [node()] }).nodes[0].latest_run.status, "running");
  f.subscriptions[0].callbacks.onFrame(settled("unknown"));
  assert.equal(f.api.get().c.status, "unavailable");
  assert.equal(f.api.get().c.ended, false);
  assert.equal(f.api.decorate({ nodes: [node()] }).nodes[0].latest_run.status, "running");
});

test("disconnection checks actual Run terminal state once and validates the returned owner", async () => {
  const pending = deferred();
  let count = 0;
  const f = fixture(() => ++count === 1 ? run() : pending.promise);
  f.api.sync(owner());
  await tick();
  const source = f.subscriptions[0];
  source.callbacks.onConnection("reconnecting");
  source.callbacks.onConnection("reconnecting");
  assert.equal(f.queries.length, 2);
  pending.resolve(run("r", { status: "timeout" }));
  await tick();
  assert.equal(source.closed, true);
  assert.equal(f.api.get().c.status, "ended");
  assert.equal(f.api.decorate({ nodes: [node()] }).nodes[0].latest_run.status, "timeout");
  let calls = 0;
  const wrong = fixture(() => ++calls === 1 ? run() : run("other", { status: "success" }));
  wrong.api.sync(owner());
  await tick();
  wrong.subscriptions[0].callbacks.onConnection("reconnecting");
  await tick();
  assert.equal(wrong.api.get().c.status, "unavailable");
  assert.equal(wrong.api.decorate({ nodes: [node()] }).nodes[0].latest_run.status, "running");
});

test("401/403/404 disconnect errors close the preview and explicit retry creates a fresh validated observer", async () => {
  for (const status of [401, 403, 404]) {
    let count = 0;
    const f = fixture(() => { if (++count === 2) throw Object.assign(new Error("denied"), { status }); return run(); });
    f.api.sync(owner());
    await tick();
    const old = f.subscriptions[0];
    old.callbacks.onConnection("reconnecting");
    await tick();
    assert.equal(f.api.get().c.status, "unavailable");
    assert.equal(old.closed, true);
    f.api.retry();
    await tick();
    assert.equal(f.subscriptions.length, 2);
    assert.equal(f.queries[0].signal.aborted, true);
    assert.equal(f.api.get().c.status, "waiting");
  }
});

test("initial terminal GET needs no stream and any origin initial values never expose earlier output", async () => {
  const ended = fixture(async () => run("r", { status: "success" }));
  ended.api.sync(owner());
  await tick();
  assert.equal(ended.subscriptions.length, 0);
  assert.equal(ended.api.get().c.status, "ended");
  for (const origin of ["resume", "patrol", "user"]) {
    const initial = fixture(async () => run("r", { origin }));
    initial.api.sync(owner());
    await tick();
    initial.subscriptions[0].callbacks.onFrame({ ...envelope(1, { messages: [{ id: "input", role: "human", content: "input" }, { id: "old", role: "ai", content: "previous Run" }] }), type: "events" });
    assert.equal(initial.api.get().c.text, "");
    initial.api.sync(owner([node("r", { historical: true })]));
    assert.deepEqual(initial.api.get(), {});
    assert.equal(initial.subscriptions[0].closed, true);
  }
});

test("transient disconnect keeps real text, node removal and stop cancel pending work", async () => {
  let count = 0;
  const f = fixture(() => { if (++count > 1) throw new Error("offline"); return run(); });
  f.api.sync(owner());
  await tick();
  const source = f.subscriptions[0];
  source.callbacks.onFrame(envelope(1, { message_id: "m", content: "真实正文" }));
  source.callbacks.onConnection("reconnecting");
  await tick();
  assert.equal(f.api.get().c.text, "真实正文");
  assert.equal(f.api.get().c.status, "disconnected");
  assert.equal(source.closed, false);
  f.api.sync(owner([]));
  assert.deepEqual(f.api.get(), {});
  assert.equal(source.closed, true);
  f.api.stop();
  assert.equal(f.frames.size, 0);
  assert.ok(f.queries.every(query => query.signal.aborted));
});
