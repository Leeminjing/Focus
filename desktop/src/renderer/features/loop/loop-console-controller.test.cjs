// Deferred responses exercise the real controller and Store, including transports that ignore abort.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const ConsoleStore = require("./loop-console-store.js");
const ConsoleController = require("./loop-console-controller.js");

function fixture(t) {
  const originalFrame = globalThis.requestAnimationFrame;
  const originalCancel = globalThis.cancelAnimationFrame;
  globalThis.requestAnimationFrame = () => 1;
  globalThis.cancelAnimationFrame = () => {};
  const store = ConsoleStore.create();
  const delayed = new Map();
  const calls = [];
  const respond = (method, loopId, options, value) => {
    calls.push({ method, loopId, options });
    const pending = delayed.get(method);
    if (!pending) return Promise.resolve(value);
    delayed.delete(method);
    pending.value = value;
    pending.options = options;
    // Deliberately ignore cancellation: a response may already be in flight.
    return pending.promise;
  };
  const controller = ConsoleController.create({ store, api: {
    console: loopId => respond("console", loopId, {}, {
      loop_id: loopId, initial_context_id: "shared", nodes: [
        { context_id: "shared", revision: { revision_id: "r1" } },
        { context_id: "other", revision: { revision_id: "r1" } },
      ], edges: [],
    }),
    conversation: (loopId, contextId, options) => respond("conversation", loopId, options, {
      context_id: contextId, revision: { revision_id: "r1" }, total: 100,
      messages: [{ index: options.before == null ? 50 : options.before - 1, message: { content: loopId } }],
    }),
    facts: (loopId, options) => respond("facts", loopId, options, {
      facts: [{ fact_id: `${loopId}-${options.before ?? "latest"}` }],
      range: { start: 1, end: 2 }, has_more: true, next_before: 1,
    }),
  } });
  t.after(() => {
    controller.destroy();
    globalThis.requestAnimationFrame = originalFrame;
    globalThis.cancelAnimationFrame = originalCancel;
  });
  function delay(method) {
    let resolve, reject;
    const pending = { promise: new Promise((yes, no) => { resolve = yes; reject = no; }) };
    pending.resolve = () => resolve(pending.value);
    pending.reject = () => reject(new Error("late request failure"));
    delayed.set(method, pending);
    return pending;
  }
  return { store, controller, delay, calls };
}

const operations = [
  { name: "initial manifest", method: "console", run: controller => controller.load("A") },
  { name: "refresh", method: "console", run: controller => controller.refresh() },
  { name: "conversation", method: "conversation", run: controller => controller.selectContext("shared", { force: true }) },
  { name: "older messages", method: "conversation", run: controller => controller.loadOlder() },
  { name: "newer messages", method: "conversation", run: controller => controller.loadNewer() },
  { name: "facts", method: "facts", run: controller => controller.reloadFacts() },
  { name: "older facts", method: "facts", run: controller => controller.loadOlderFacts() },
];

for (const operation of operations) {
  for (const outcome of ["resolve", "reject"]) {
    test(`late ${operation.name} ${outcome} cannot change another Loop with the same Context and revision`, async t => {
      const { store, controller, delay, calls } = fixture(t);
      await controller.load("A");
      const pending = delay(operation.method);
      const stale = operation.run(controller);
      await controller.load("B");
      store.begin("current-operation");
      const current = store.get();
      const requestCount = calls.length;
      pending[outcome]();
      await stale;
      assert.strictEqual(store.get(), current, "stale data, errors and completion must all be ignored");
      assert.equal(calls.length, requestCount, "a stale continuation must not start requests for B");
    });
  }
  test(`late ${operation.name} cannot change state after destroy`, async t => {
    const { store, controller, delay } = fixture(t);
    await controller.load("A");
    const pending = delay(operation.method);
    const stale = operation.run(controller);
    controller.destroy();
    const current = store.get();
    pending.resolve();
    await stale;
    assert.strictEqual(store.get(), current);
  });
  test(`late ${operation.name} cannot change a new visit to the same Loop`, async t => {
    const { store, controller, delay } = fixture(t);
    await controller.load("A");
    const pending = delay(operation.method);
    const stale = operation.run(controller);
    await controller.load("B");
    await controller.load("A");
    const current = store.get();
    pending.resolve();
    await stale;
    assert.strictEqual(store.get(), current);
  });
  test(`current ${operation.name} failures remain visible`, async t => {
    const { store, controller, delay } = fixture(t);
    await controller.load("A");
    const pending = delay(operation.method);
    const current = operation.run(controller);
    pending.reject();
    await current;
    assert.equal(store.get().error, "late request failure");
    assert.equal(store.get().pending, null);
  });
}

test("Loop switch invalidates all-scope facts before the new manifest arrives", async t => {
  const { store, controller, delay } = fixture(t);
  await controller.load("A");
  await controller.setFactScope("all");
  const facts = delay("facts");
  const stale = controller.reloadFacts();
  const manifest = delay("console");
  const next = controller.load("B");
  const current = store.get();
  facts.resolve();
  await stale;
  assert.strictEqual(store.get(), current);
  manifest.resolve();
  await next;
});

test("switching Loops clears conversation and viewport caches even for shared Context IDs", async t => {
  const { store, controller, calls } = fixture(t);
  await controller.load("A");
  store.saveViewport("shared", { scrollTop: 321 });
  await controller.load("B");
  assert.deepEqual(calls.filter(call => call.method === "conversation").map(call => call.loopId), ["A", "B"]);
  assert.equal(store.get().conversation.messages[0].message.content, "B");
  assert.equal(store.get().conversationViewport, null);
});

test("stale pagination cannot block or unlock the new Loop pagination", async t => {
  const { store, controller, delay, calls } = fixture(t);
  await controller.load("A");
  const oldPage = delay("conversation");
  const stale = controller.loadOlder();
  await controller.load("B");
  const newPage = delay("conversation");
  const current = controller.loadOlder();
  assert.ok(newPage.value, "B must be able to paginate while A remains in flight");
  oldPage.resolve();
  await stale;
  const requestCount = calls.length;
  await controller.loadOlder();
  assert.equal(calls.length, requestCount, "A must not release B's in-flight pagination lock");
  newPage.resolve();
  await current;
  assert.equal(store.get().pending, null);
});

test("returning to the same Context ignores its earlier in-flight request", async t => {
  const { store, controller, delay } = fixture(t);
  await controller.load("A");
  const pending = delay("conversation");
  const stale = controller.selectContext("shared", { force: true });
  await controller.selectContext("other");
  await controller.selectContext("shared", { force: true });
  const current = store.get();
  pending.resolve();
  await stale;
  assert.strictEqual(store.get(), current);
});
