"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { subscribeTaskChanges } = require("./task-state-stream.cjs");

test("fragmented authenticated events reconcile and stop releases the reader", async () => {
  let changes = 0, cancelled = 0;
  const stop = subscribeTaskChanges({ url: "http://localhost/task-changes/stream",
    headers: { "X-Focus-Session": "test" }, onChange: () => { changes += 1; },
    fetchImpl: async (_url, options) => {
      assert.equal(options.headers["X-Focus-Session"], "test");
      return new Response(new ReadableStream({
        start(controller) {
          const encoder = new TextEncoder();
          for (const frame of [": keepalive\n\nevent: chan", "ged\r", "\ndata: {}\r\n\r\n"]) {
            controller.enqueue(encoder.encode(frame));
          }
          options.signal.addEventListener("abort", () => controller.error(new Error("aborted")), { once: true });
        },
        cancel() { cancelled += 1; },
      }));
    },
  });
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(changes, 2); // connection reconciliation + one changed frame
  stop();
  await stop.finished;
  assert.ok(cancelled <= 1);
});

test("reconnection refreshes even if a notification was missed", async () => {
  let connections = 0, changes = 0;
  const stop = subscribeTaskChanges({ url: "http://localhost/events", headers: {}, retryMs: 1,
    onChange: () => { changes += 1; },
    fetchImpl: async (_url, { signal }) => {
      connections += 1;
      return new Response(new ReadableStream({ start(controller) {
        if (connections === 1) controller.close();
        else signal.addEventListener("abort", () => controller.error(new Error("aborted")), { once: true });
      } }));
    },
  });
  await new Promise(resolve => setTimeout(resolve, 20));
  stop();
  await stop.finished;
  assert.equal(connections, 2);
  assert.equal(changes, 2);
});
