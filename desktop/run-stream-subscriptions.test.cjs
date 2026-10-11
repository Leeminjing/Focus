/* 本文件验证共享 Run 传输的真实观察者合同。
 * 输入为可控 EventSource 与两个观察者；输出为连接数量、事件顺序、重放边界、错误隔离和释放断言。
 * 工作流覆盖图先订阅/任务迟到、终态微任务重放、普通重连和超过 512 帧；示例：node --test desktop/run-stream-subscriptions.test.cjs。
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const { create } = require("./run-stream-subscriptions.js");
function fixture(options = {}) {
  const sources = [];
  class Source {
    constructor(url) { this.url = url; this.listeners = new Map(); this.closed = false; sources.push(this); }
    addEventListener(type, listener) { this.listeners.set(type, listener); }
    emit(type, id = "", data = "{}") { this.listeners.get(type)?.({ lastEventId: String(id), data }); }
    close() { this.closed = true; }
  }
  return { streams: create({ apiBase: "http://localhost:7210/", session: "session 1" }, { EventSourceImpl: Source, ...options }), sources };
}
const tick = () => new Promise(resolve => queueMicrotask(resolve));

test("two observers share transport; a later task receives retained output while tool is silent", async () => {
  const { streams, sources } = fixture(), graph = [], task = [];
  const first = streams.subscribe("run/1", { onFrame: frame => graph.push(frame) });
  assert.match(sources[0].url, /runs\/run%2F1\/stream\?session=session%201$/);
  await tick();
  sources[0].emit("metadata", 1, "metadata");
  sources[0].emit("tokens", 2, "assistant output");
  sources[0].emit("events", 3, "tool waiting");
  const second = streams.subscribe("run/1", { onFrame: frame => task.push(frame) });
  assert.equal(task.length, 0);
  await tick();
  assert.deepEqual(task, graph);
  assert.equal(sources.length, 1);
  first.close();
  assert.equal(sources[0].closed, false);
  sources[0].emit("events", 4, "tool result");
  assert.equal(graph.length, 3);
  assert.equal(task.length, 4);
  second.close();
  assert.equal(sources[0].closed, true);
  streams.subscribe("run/1", {});
  assert.equal(sources.length, 2);
});

test("replay is bounded and preserves all event sequence types without duplicating reconnect data", async () => {
  const { streams, sources } = fixture(), received = [];
  streams.subscribe("r", {});
  await tick();
  for (let index = 1; index <= 600; index++) sources[0].emit(index % 2 ? "tokens" : "reasoning", index);
  const sub = streams.subscribe("r", { onFrame: frame => received.push(Number(frame.id)) });
  sources[0].emit("metadata", 601);
  sources[0].emit("tokens", 601);
  await tick();
  assert.equal(received.length, 512);
  assert.equal(received[0], 90);
  assert.equal(received.at(-1), 601);
  sub.close();
});

test("observer exceptions do not block another consumer and network errors retain connection ownership", async () => {
  const errors = [], received = [], states = [];
  const { streams, sources } = fixture({ onObserverError: error => errors.push(error.message) });
  streams.subscribe("r", { onFrame() { throw new Error("consumer failure"); } });
  streams.subscribe("r", { onFrame: frame => received.push(frame.id), onConnection: state => states.push(state) });
  await tick();
  sources[0].emit("open");
  sources[0].emit("tokens", 1);
  sources[0].emit("error", "", "");
  assert.equal(sources[0].closed, false);
  sources[0].emit("open");
  sources[0].emit("tokens", 1);
  sources[0].emit("tokens", 2);
  assert.deepEqual(received, ["1", "2"]);
  assert.deepEqual(errors, ["consumer failure", "consumer failure"]);
  assert.deepEqual(states, ["connecting", "live", "reconnecting", "live"]);
});

test("terminal replay runs only after handle registration and disposed observers see no late frame", async () => {
  const { streams, sources } = fixture();
  streams.subscribe("r", {});
  await tick();
  sources[0].emit("end", "end", '{"run_id":"r","status":"success"}');
  assert.equal(sources[0].closed, true);
  let handle, calls = 0;
  handle = streams.subscribe("r", { onFrame(frame) { assert.equal(frame.type, "end"); handle.close(); calls++; } });
  await tick();
  assert.equal(calls, 1);
  const stopped = streams.subscribe("r", { onFrame() { throw new Error("late callback"); } });
  stopped.close();
  await tick();
});

test("async business callback failure remains isolated from read-only observer", async () => {
  const failures = [], frames = [];
  const { streams, sources } = fixture({ onObserverError: error => failures.push(error.message) });
  streams.subscribe("r", { async onFrame() { throw new Error("failed refresh"); } });
  streams.subscribe("r", { onFrame: frame => frames.push(frame.type) });
  await tick();
  sources[0].emit("end", "end");
  await tick();
  assert.deepEqual(frames, ["end"]);
  assert.deepEqual(failures, ["failed refresh"]);
});

test("a late Main observer retains original interrupt and end business events in order", async () => {
  const { streams, sources } = fixture(), task = [];
  streams.subscribe("r", {});
  await tick();
  sources[0].emit("tokens", 1, "body");
  sources[0].emit("interrupt", 2, '{"data":{"value":{"type":"access_review"}}}');
  sources[0].emit("end", "end", '{"run_id":"r","status":"interrupted"}');
  streams.subscribe("r", { onFrame: frame => task.push(frame) });
  await tick();
  assert.deepEqual(task.map(frame => frame.type), ["tokens", "interrupt", "end"]);
  assert.equal(JSON.parse(task[1].data).data.value.type, "access_review");
  assert.equal(JSON.parse(task[2].data).status, "interrupted");
  assert.equal(sources.length, 1);
});
