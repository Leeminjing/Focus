"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");

test('restored manual browser exposes uncertain prior work without acquiring control', async () => {
  const h = harness();
  await h.controller.select('A', { present: false });
  h.sources[0].emit({ context_id: 'A', state: 'manual', recovered: true,
    interrupted_operation: { kind: 'navigate', result_unknown: true, settled: false } });
  assert.match(h.node.textContent, /恢复/);
  assert.match(h.node.textContent, /结果未知/);
  assert.match(h.node.textContent, /交还/);
  assert.equal(h.requests.length, 1);
  h.controller.close();
});

test('live recoverable browser explains that closing Focus preserves its pages', () => {
  const { recoverySummary, statusLabel } = require('./context-browser.js');
  const text = recoverySummary({ state: 'automatic', can_reconnect: true });
  assert.match(text, /关闭 Focus/);
  assert.match(text, /仍会运行/);
  assert.match(text, /交还/);
  assert.match(text, /重新观察/);
  for (const state of ['closed', 'disconnected', 'faulted']) {
    assert.equal(recoverySummary({ state, can_reconnect: true }), '');
  }
  assert.equal(recoverySummary({ state: 'automatic', can_reconnect: false }), '');
  assert.match(statusLabel({ state: 'disconnected', detail: { reason: 'recovery_disabled' } }), /恢复已关闭.*原窗口.*重连/);
});

function harness(apiImpl) {
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
    document: { querySelectorAll: selector => selector === '[data-browser-status]' ? [node] : [], addEventListener() {} } });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "context-browser.js"), "utf8"), context);
  const controller = context.module.exports.create({ runtime: { apiBase: "http://localhost:1234", session: "session" },
    EventSourceImpl: Source, onStatus: (...args) => status.push(args), api: async (url, options) => {
      requests.push({ url, options });
      if (apiImpl) return await apiImpl(url, options);
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

test("page presentation targets the existing page without requesting control", async () => {
  const h = harness();
  await h.controller.select("A", { pageId: "child" });
  assert.deepEqual(JSON.parse(h.requests[0].options.body), { client_id: "window-id", sequence: 1, page_id: "child" });
  assert.equal(h.requests.length, 1);
  h.controller.close();
});

test("late responses and closed event sources cannot overwrite current state", async () => {
  let finish;
  const h = harness(async (url, options) => {
    const sequence = JSON.parse(options.body).sequence;
    if (sequence === 1) return await new Promise(resolve => { finish = resolve; });
    return { context_id: url.split("/")[4], state: "manual" };
  });
  const old = h.controller.select("A");
  await h.controller.select("B");
  await h.controller.select("A");
  h.sources[0].emit({ context_id: "A", state: "automatic" });
  finish({ context_id: "A", state: "available" });
  await old;
  assert.match(h.node.textContent, /人工控制/);
  h.controller.close();
});

test("an older presentation failure cannot overwrite feedback from a newer success for the same Context", async () => {
  let fail;
  const h = harness(async (url, options) => {
    if (JSON.parse(options.body).sequence === 1) {
      return await new Promise((resolve, reject) => { fail = reject; });
    }
    return { context_id: "A", state: "manual", foreground: false };
  });
  const old = h.controller.select("A");
  await h.controller.select("A");
  const currentStatus = h.status.slice();
  assert.equal(currentStatus.length, 1);
  assert.equal(currentStatus[0][1], false);
  fail(new Error("older presentation failed"));
  await old;
  assert.deepEqual(h.status, currentStatus);
  assert.match(h.node.textContent, /人工控制/);
  h.controller.close();
});

test("the latest presentation failure still reports an error", async () => {
  const h = harness(async () => { throw new Error("current presentation failed"); });
  await h.controller.select("A");
  assert.deepEqual(h.status, [["current presentation failed", true]]);
  h.controller.close();
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

test("download and permission records render safe readable results with native-only decisions", () => {
  const { renderInteractions, statusLabel } = require("./context-browser.js");
  const value = { downloads: [{ download_id: "file", page_id: "child", state: "in_progress", bytes_received: 512,
    total_bytes: 2048, path: 'C:/Downloads/<file>.txt', url: 'https://site.example/file?x=<script>' }],
    permissions: [{ request_id: "request", page_id: "parent", kind: "geolocation", state: "pending", url: "https://site.example/" }],
    rejected: [{ reason: "download_limit" }], interaction_pending: true };
  const html = renderInteractions(value);
  assert.match(html, /512 \/ 2048/);
  assert.match(html, /value="512"/);
  assert.match(html, /C:\/Downloads\/&lt;file&gt;\.txt/);
  assert.match(html, /child/);
  assert.match(html, /位置/);
  assert.match(html, /https:\/\/site\.example\//);
  assert.match(html, /原生/);
  assert.doesNotMatch(html, /<script>|data-(grant|cancel-download)/);
  assert.match(statusLabel({ state: "manual", site_interactions: value }), /权限待决定/);
  assert.match(renderInteractions({ downloads: [{ state: "cancelled", reason: "human_cancel", page_id: "child" }] }), /已取消/);
});
