"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const LoopApi = require("../loop/loop-api.js");
const PatrolView = require("../patrol/workspace/workspace-patrol-view.js");
require("./context-inspection-view.js");
const { createDocument } = require("../../../../tests/helpers/test-dom.cjs");

test("clicking Context sources uses current conversation metadata without retired API calls", async () => {
  const document = createDocument();
  const panel = document.createElement("aside");
  panel.innerHTML = '<div data-patrol-context-content></div>';
  const calls = [];
  const api = LoopApi.create({ apiBase: "http://focus", session: "test" }, async url => {
    calls.push(url);
    assert.match(url, /\/contexts\/(current|source)\/conversation$/);
    assert.doesNotMatch(url, /context-revisions|\/revisions|revision_id=/);
    return { ok: true, json: async () => ({ context_id: url.includes("/source/") ? "source" : "current",
      revision: { revision_id: "current-token" }, content_hash: "current-content-hash",
      sources: [{ context_id: "source" }], messages: [], total: 0, has_more: false }) };
  });
  const context = vm.createContext({ document, AbortController, CSS: { escape: value => value },
    FocusWorkspacePatrolView: PatrolView, FocusMessageReading: { handleCopy: () => false },
    FocusSurfaceTransition: { finish() {}, visible: async (node, visible) => { node.hidden = !visible; node.inert = !visible; } },
  });
  vm.runInContext(fs.readFileSync(require.resolve("./context-inspector.js"), "utf8"), context);
  const inspector = context.FocusContextInspector.create({ api, onOpenTask: async () => {},
    getManifest: () => ({ nodes: [{ context_id: "source", title: "来源会话" }], edges: [] }) });
  inspector.mount(panel);
  // A stale graph token must not turn a normal inspection into historical browsing.
  await inspector.select("loop", "current", { current_revision_id: "retired-token" }, null, "retired-token");
  const click = async target => {
    panel.dispatchEvent({ type: "click", target, stopPropagation() {} });
    await new Promise(resolve => setImmediate(resolve));
  };
  await click(panel.querySelector('[data-patrol-context-tab="sources"]'));
  assert.equal(calls.length, 1);
  assert.match(panel.textContent, /current-content-hash/);
  assert.match(panel.textContent, /来源会话/);
  assert.equal(panel.querySelector('[role="alert"]'), null);
  assert.equal(panel.querySelector("[data-patrol-inspect-revision]"), null);
  await click(panel.querySelector('[data-context-related="source"]'));
  assert.equal(calls.length, 2);
  assert.match(calls[1], /\/contexts\/source\/conversation$/);
  inspector.dispose();
});

test("pagination conflict preserves loaded text until the user explicitly refreshes current content", async () => {
  const document = createDocument();
  const panel = document.createElement("aside");
  panel.innerHTML = '<div data-patrol-context-content></div>';
  let advanced = false;
  const calls = [];
  const api = LoopApi.create({ apiBase: "http://focus", session: "test" }, async url => {
    calls.push(new URL(url));
    const state = new URL(url).searchParams.get("revision_id");
    if (advanced && state === "old-current-token") {
      return Response.json({ detail: "Context 内容已更新，请刷新当前内容后继续阅读" }, { status: 409 });
    }
    return Response.json({ context_id: "current", revision: { revision_id: advanced ? "new-current-token" : "old-current-token" },
      content_hash: advanced ? "new-hash" : "old-hash", sources: [], total: advanced ? 1 : 2,
      range: { start: advanced ? 0 : 1, end: advanced ? 1 : 2 }, next_before: advanced ? null : 1,
      messages: [{ index: advanced ? 0 : 1, message: { role: "user", content: advanced ? "new current body" : "loaded old body" } }],
      has_more: !advanced });
  });
  const context = vm.createContext({ document, AbortController, CSS: { escape: value => value },
    FocusWorkspacePatrolView: PatrolView, FocusMessageReading: { handleCopy: () => false },
    FocusSurfaceTransition: { finish() {}, visible: async (node, visible) => { node.hidden = !visible; node.inert = !visible; } },
  });
  vm.runInContext(fs.readFileSync(require.resolve("./context-inspector.js"), "utf8"), context);
  const inspector = context.FocusContextInspector.create({ api, onOpenTask: async () => {} });
  const click = async target => {
    panel.dispatchEvent({ type: "click", target, stopPropagation() {} });
    await new Promise(resolve => setImmediate(resolve));
  };
  inspector.mount(panel);
  await inspector.select("loop", "current", {});
  await click(panel.querySelector('[data-patrol-context-tab="conversation"]'));
  assert.match(panel.textContent, /loaded old body/);
  advanced = true;
  await click(panel.querySelector("[data-patrol-context-older]"));
  assert.equal(calls.length, 2);
  assert.equal(calls[1].searchParams.get("revision_id"), "old-current-token");
  assert.equal(calls[1].searchParams.get("before"), "1");
  assert.equal(inspector.selection().revisionId, "old-current-token");
  assert.match(panel.textContent, /loaded old body/);
  assert.doesNotMatch(panel.textContent, /new current body/);
  assert.match(panel.querySelector('[role="alert"]').textContent, /内容已更新/);
  const retry = panel.querySelector("[data-context-inspection-retry]");
  assert.equal(retry.textContent, "刷新当前内容");
  await click(retry);
  assert.equal(calls.length, 3);
  assert.equal(calls[2].search, "");
  assert.equal(inspector.selection().revisionId, "new-current-token");
  assert.match(panel.textContent, /new current body/);
  assert.doesNotMatch(panel.textContent, /loaded old body/);
  assert.equal(panel.querySelector('[role="alert"]'), null);
  inspector.dispose();
});
