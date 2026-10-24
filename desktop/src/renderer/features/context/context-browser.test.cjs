"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");

function harness() {
  const requests = [], sources = [], status = [];
  const node = { dataset: { browserStatus: "A" }, textContent: "" };
  class Source {
    constructor(url) { this.url = url; this.listeners = {}; sources.push(this); }
    addEventListener(type, callback) { this.listeners[type] = callback; }
    close() { this.closed = true; }
    emit(value) { this.listeners.browser({ data: JSON.stringify(value) }); }
  }
  const context = vm.createContext({ module: { exports: {} },
    crypto: { randomUUID: () => "window-id" },
    document: { querySelectorAll: () => [node], addEventListener() {} } });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "context-browser.js"), "utf8"), context);
  const controller = context.module.exports.create({ runtime: { apiBase: "http://localhost:1234", session: "session" },
    EventSourceImpl: Source, onStatus: (...args) => status.push(args), api: async (url, options) => {
      requests.push({ url, options });
      return { context_id: url.split("/")[4], state: "available", browser_id: "native", foreground: false };
    } });
  return { controller, requests, sources, node, status };
}

test("Context selection only presents the native browser and preserves paused handoff state", async () => {
  const h = harness();
  await h.controller.select("A");
  assert.equal(h.requests[0].url, "/desktop/api/contexts/A/browser/present");
  assert.equal(h.requests[0].options.method, "POST");
  assert.deepEqual(JSON.parse(h.requests[0].options.body), { client_id: "window-id", sequence: 1 });
  assert.match(h.status[0][0], /Windows/);
  h.sources[0].emit({ context_id: "A", state: "paused", shared_context_ids: ["A", "B"] });
  assert.match(h.node.textContent, /交还/);
  assert.match(h.node.textContent, /2 个 Context/);
  assert.equal(h.requests.length, 1, "state event must never resume or acquire");
  await h.controller.select("B");
  assert.equal(h.sources[0].closed, true);
  assert.equal(h.requests[1].url, "/desktop/api/contexts/B/browser/present");
  assert.equal(JSON.parse(h.requests[1].options.body).sequence, 2);
  assert.equal(h.requests.some(item => /create|hide|minimize|acquire/.test(item.url)), false);
  h.controller.close();
  assert.equal(h.sources[1].closed, true);
});

test("passive status rendering never presents or repeatedly reloads the browser", async () => {
  const h = harness();
  h.controller.watch("A");
  await new Promise(resolve => setImmediate(resolve));
  h.controller.watch("A");
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].url, "/desktop/api/contexts/A/browser");
  h.controller.close();
});
