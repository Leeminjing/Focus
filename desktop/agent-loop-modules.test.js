/*
 * 本文件验证 Loop Mission 编辑器、Store/Console Store、API/完整会话协议、Portfolio 图、事实、终止态只读和 provenance 外置渲染。
 * 输入为重复/乱序事件、千条会话页、模拟 fetch、Lane revisions 和多父边；输出为幂等 cursor、固定
 * 消息窗口、视口恢复、正确请求、完整 secondary source 与不污染消息正文的 badge 断言。具体工作流为
 * 直接加载无 DOM UMD 模块并调用纯函数；示例：`node --test desktop/agent-loop-modules.test.js`。
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const LoopApi = require("./loop-api.js");
const LoopStore = require("./loop-store.js");
const MissionEditor = require("./loop-mission-editor.js");
globalThis.FocusLoopMissionEditor = MissionEditor;
const LoopView = require("./loop-view.js");
const Portfolio = require("./portfolio-view.js");
const Evolution = require("./context-evolution-view.js");
const Provenance = require("./message-provenance-view.js");
const ConsoleStore = require("./loop-console-store.js");
const PortfolioMap = require("./portfolio-map-view.js");
const Conversation = require("./context-conversation-view.js");
const Facts = require("./loop-facts-view.js");
const ConsoleController = require("./loop-console-controller.js");


test("mission editor separates outcome boundaries and evidence-backed checks", () => {
  const mission = MissionEditor.buildMission({
    outcome: "交付 Live Loop",
    in_scope: ["Loop 页面"],
    required_invariants: ["保留 Kernel 权威"],
    prohibited_actions: ["伪造活动"],
    completion_checks: [{ check_id: "tests", claim: "测试通过", evidence: "test", required: true }],
  });
  assert.equal(mission.outcome, "交付 Live Loop");
  assert.deepEqual(mission.boundaries.required_invariants, ["保留 Kernel 权威"]);
  assert.deepEqual(mission.completion_checks[0].expected_evidence_kinds, ["test"]);
  assert.match(MissionEditor.render(mission), /最终结果/);
  assert.match(MissionEditor.render(mission), /执行边界/);
  assert.match(MissionEditor.render(mission), /完成检查/);
  assert.match(MissionEditor.render(mission), /aria-label="最终结果"/);
  assert.match(MissionEditor.render(mission), /aria-label="允许触及的范围"/);
  assert.match(MissionEditor.render(mission), /aria-label="必须保持的不变量"/);
  assert.match(MissionEditor.render(mission), /aria-label="禁止或超范围动作"/);
  assert.doesNotMatch(MissionEditor.render(mission), /Task Contract/);
});


test("mission editor rejects identical text across semantic roles", () => {
  assert.throws(() => MissionEditor.buildMission({
    outcome: "测试通过",
    required_invariants: ["保留 Kernel"],
    completion_checks: [{ check_id: "tests", claim: "测试通过", evidence: "test" }],
  }), /不能同时属于最终结果和完成检查/);
});


test("store replays cursor events once and reconciles stale controls", () => {
  const store = LoopStore.create();
  store.load({ loop_id: "l1", status: "running" });
  store.beginControl("pause");
  store.apply([{ event_id: "e1", cursor: 1, type: "Started" }, { event_id: "e1", cursor: 1, type: "Started" }]);
  store.apply([{ event_id: "old", cursor: 0 }, { event_id: "e2", cursor: 2 }]);
  store.reconcile({ loop_id: "l1", status: "paused" });
  assert.equal(store.get().events.length, 2);
  assert.equal(store.get().cursor, 2);
  assert.equal(store.get().pendingControl, null);
  assert.equal(store.get().snapshot.status, "paused");
});


test("api sends session header and cursor", async () => {
  const calls = [];
  const api = LoopApi.create({ apiBase: "http://focus", session: "secret" }, async (url, options) => {
    calls.push({ url, options });
    return { ok: true, json: async () => [] };
  });
  await api.events("loop one", 7);
  assert.equal(calls[0].url, "http://focus/desktop/api/agent-loops/loop%20one/events?after=7");
  assert.equal(calls[0].options.headers["X-Focus-Session"], "secret");
});

test("api preserves a plain-text HTTP failure instead of exposing JSON syntax errors", async () => {
  const api = LoopApi.create({ apiBase: "http://focus", session: "secret" }, async () => new Response("Internal Server Error", {
    status: 500,
    statusText: "Internal Server Error",
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  }));
  await assert.rejects(api.start({}), error => {
    assert.equal(error.status, 500);
    assert.match(error.message, /Internal Server Error/);
    assert.doesNotMatch(error.message, /Unexpected token|JSON/);
    return true;
  });
});


test("api sends grant mutations through the authority boundary", async () => {
  const calls = [];
  const api = LoopApi.create({ apiBase: "http://focus", session: "secret" }, async (url, options) => {
    calls.push({ url, options });
    return { ok: true, json: async () => ({ loop_id: "l1" }) };
  });
  await api.mutateGrant("loop one", { command: "revoke" });
  assert.equal(calls[0].url, "http://focus/desktop/api/agent-loops/loop%20one/grant");
  assert.deepEqual(JSON.parse(calls[0].options.body), { command: "revoke" });
});

test("api activates an explicitly confirmed mission revision", async () => {
  const calls = [];
  const api = LoopApi.create({ apiBase: "http://focus", session: "secret" }, async (url, options) => {
    calls.push({ url, options });
    return { ok: true, json: async () => ({ loop_id: "l1", goal_revision: 2 }) };
  });
  const mission = { outcome: "完成发布", boundaries: {}, completion_checks: [{ check_id: "tests", claim: "测试通过", expected_evidence_kinds: ["test"] }] };
  await api.reviseMission("loop one", mission);
  assert.equal(calls[0].url, "http://focus/desktop/api/agent-loops/loop%20one/missions");
  assert.deepEqual(JSON.parse(calls[0].options.body), { confirmation: "activate", mission });
});


test("api reads an immutable historical revision outside the Loop route prefix", async () => {
  const calls = [];
  const api = LoopApi.create({ apiBase: "http://focus", session: "secret" }, async (url, options) => {
    calls.push({ url, options });
    return { ok: true, json: async () => ({ ref: { revision_id: "r one" } }) };
  });
  const revision = await api.revision("r one");
  assert.equal(calls[0].url, "http://focus/desktop/api/context-revisions/r%20one");
  assert.equal(revision.ref.revision_id, "r one");
});


test("console api exposes topology, paginated conversation, facts and scoped intervention", async () => {
  const calls = [];
  const api = LoopApi.create({ apiBase: "http://focus", session: "secret" }, async (url, options = {}) => {
    calls.push({ url, options });
    return { ok: true, json: async () => url.endsWith("/tasks/c%2F1") ? { ui_state: { _main_run_equipment: { permissions: ["read"], skills: ["testing"], access_mode: "workspace" } } } : {} };
  });
  await api.console("l 1");
  await api.conversation("l 1", "c/1", { before: 48, limit: 24 });
  await api.facts("l 1", { contextId: "c/1", kind: "test" });
  await api.intervene("l 1", { mode: "patrol_context_intent", context_id: "c/1", content: "Run tests" });
  await api.directMessage("c/1", "Continue the run");
  await api.restoreCompression("c/1", "compressed-message");
  assert.equal(calls[0].url, "http://focus/desktop/api/agent-loops/l%201/console");
  assert.match(calls[1].url, /conversation\?before=48&limit=24$/);
  assert.match(calls[2].url, /facts\?context_id=c%2F1&kind=test$/);
  assert.deepEqual(JSON.parse(calls[3].options.body), { mode: "patrol_context_intent", context_id: "c/1", content: "Run tests" });
  assert.equal(calls[4].url, "http://focus/desktop/api/tasks/c%2F1");
  assert.equal(calls[5].url, "http://focus/desktop/api/tasks/c%2F1/main/runs");
  assert.deepEqual(JSON.parse(calls[5].options.body), { message: "Continue the run", model_name: null, skills: ["testing"], permissions: ["read"], access_mode: "workspace" });
  assert.equal(calls[6].url, "http://focus/desktop/api/compression/quick-apply");
  assert.deepEqual(JSON.parse(calls[6].options.body).ranges, [{ source_ids: ["compressed-message"], restore: true }]);
});


test("console store prepends history without duplicating message indices", () => {
  const store = ConsoleStore.create();
  store.loadManifest({ initial_context_id: "c1", nodes: [{ context_id: "c1" }] });
  store.loadConversation({ context_id: "c1", messages: [{ index: 2 }, { index: 3 }], range: { start: 2, end: 4 }, has_more: true, next_before: 2 });
  store.prependConversation({ context_id: "c1", messages: [{ index: 0 }, { index: 1 }, { index: 2 }], range: { start: 0, end: 3 }, has_more: false, next_before: null });
  assert.deepEqual(store.get().conversation.messages.map(item => item.index), [0, 1, 2, 3]);
  assert.equal(store.get().conversation.has_more, false);
});


test("console store keeps a fixed message bound while paging through thousands of messages", () => {
  const store = ConsoleStore.create();
  store.loadManifest({ initial_context_id: "c1", nodes: [{ context_id: "c1", revision: { revision_id: "r1" } }] });
  store.loadConversation({ context_id: "c1", revision: { revision_id: "r1" }, total: 1000, messages: Array.from({ length: 48 }, (_, offset) => ({ index: 952 + offset })), range: { start: 952, end: 1000 } });
  for (let end = 952; end > 0; end -= 48) {
    const start = Math.max(0, end - 48);
    store.mergeConversation({ context_id: "c1", revision: { revision_id: "r1" }, total: 1000, messages: Array.from({ length: end - start }, (_, offset) => ({ index: start + offset })), range: { start, end } }, "older");
    assert.ok(store.get().conversation.messages.length <= ConsoleStore.MAX_CONVERSATION_MESSAGES);
  }
  assert.equal(store.get().conversation.range.start, 0);
  assert.equal(store.get().conversation.has_more, false);
  assert.equal(store.get().conversation.has_newer, true);
  assert.equal(store.get().conversation.messages.length, ConsoleStore.MAX_CONVERSATION_MESSAGES);
});


test("console store restores each revision window and viewport when returning to a Context", () => {
  const store = ConsoleStore.create();
  store.loadManifest({
    initial_context_id: "c1",
    nodes: [
      { context_id: "c1", revision: { revision_id: "r1" } },
      { context_id: "c2", revision: { revision_id: "r2" } },
    ],
  });
  store.loadConversation({ context_id: "c1", revision: { revision_id: "r1" }, total: 4, messages: [{ index: 2 }, { index: 3 }], range: { start: 2, end: 4 } });
  store.saveViewport("c1", { scrollTop: 321 });
  store.selectContext("c2");
  store.loadConversation({ context_id: "c2", revision: { revision_id: "r2" }, total: 1, messages: [{ index: 0 }], range: { start: 0, end: 1 } });
  store.saveViewport("c2", { scrollTop: 17 });
  store.selectContext("c1");
  assert.deepEqual(store.get().conversation.messages.map(item => item.index), [2, 3]);
  assert.equal(store.get().conversationViewport.scrollTop, 321);
});


test("console store paginates facts without duplicating stable fact ids", () => {
  const store = ConsoleStore.create();
  store.loadFacts({ facts: [{ fact_id: "f3" }, { fact_id: "f4" }], range: { start: 2, end: 4 }, has_more: true, next_before: 2 });
  store.prependFacts({ facts: [{ fact_id: "f1" }, { fact_id: "f2" }, { fact_id: "f3" }], range: { start: 0, end: 3 }, has_more: false, next_before: null });
  assert.deepEqual(store.get().facts.facts.map(item => item.fact_id), ["f1", "f2", "f3", "f4"]);
  assert.equal(store.get().facts.has_more, false);
});


test("console controller batches bursty store changes into one animation frame", () => {
  const originalFrame = global.requestAnimationFrame;
  const originalCancel = global.cancelAnimationFrame;
  const frames = [];
  global.requestAnimationFrame = callback => { frames.push(callback); return frames.length; };
  global.cancelAnimationFrame = () => {};
  const store = ConsoleStore.create();
  let renders = 0;
  const controller = ConsoleController.create({ api: {}, store, onChange: () => { renders += 1; } });
  try {
    store.setFactFilter("test");
    store.setFactScope("all");
    store.setMessageFilter("tool");
    assert.equal(frames.length, 1);
    frames.shift()();
    assert.equal(renders, 1);
  } finally {
    controller.destroy();
    global.requestAnimationFrame = originalFrame;
    global.cancelAnimationFrame = originalCancel;
  }
});


test("console controller ignores a stale conversation after rapid Context selection", async () => {
  const originalFrame = global.requestAnimationFrame;
  const originalCancel = global.cancelAnimationFrame;
  global.requestAnimationFrame = callback => { callback(); return 1; };
  global.cancelAnimationFrame = () => {};
  const store = ConsoleStore.create();
  let slow = false;
  let release;
  const api = {
    console: async () => ({ initial_context_id: "c1", nodes: [{ context_id: "c1" }, { context_id: "c2" }], edges: [] }),
    facts: async () => ({ facts: [] }),
    conversation: async (_loopId, contextId) => {
      if (slow && contextId === "c1") await new Promise(resolve => { release = resolve; });
      return { context_id: contextId, messages: [], range: { start: 0, end: 0 }, has_more: false };
    },
  };
  const controller = ConsoleController.create({ api, store });
  try {
    await controller.load("l1");
    store.loadManifest({ initial_context_id: "c1", nodes: [{ context_id: "c1", revision: { revision_id: "r2" } }, { context_id: "c2" }], edges: [] });
    slow = true;
    const stale = controller.selectContext("c1");
    const current = controller.selectContext("c2");
    await current;
    release();
    await stale;
    assert.equal(store.get().selectedContextId, "c2");
    assert.equal(store.get().conversation.context_id, "c2");
  } finally {
    controller.destroy();
    global.requestAnimationFrame = originalFrame;
    global.cancelAnimationFrame = originalCancel;
  }
});


test("console controller follows the fact run cursor and prepends older evidence", async () => {
  const originalFrame = global.requestAnimationFrame;
  const originalCancel = global.cancelAnimationFrame;
  global.requestAnimationFrame = callback => { callback(); return 1; };
  global.cancelAnimationFrame = () => {};
  const store = ConsoleStore.create();
  const beforeValues = [];
  const api = {
    console: async () => ({ initial_context_id: "c1", nodes: [{ context_id: "c1" }], edges: [] }),
    conversation: async () => ({ context_id: "c1", revision: { revision_id: "r1" }, messages: [], range: { start: 0, end: 0 }, has_more: false }),
    facts: async (_loopId, options) => {
      beforeValues.push(options.before ?? null);
      return options.before == null
        ? { facts: [{ fact_id: "f2" }], range: { start: 1, end: 2 }, has_more: true, next_before: 1 }
        : { facts: [{ fact_id: "f1" }], range: { start: 0, end: 1 }, has_more: false, next_before: null };
    },
  };
  const controller = ConsoleController.create({ api, store });
  try {
    await controller.load("l1");
    await controller.loadOlderFacts();
    assert.deepEqual(beforeValues, [null, 1]);
    assert.deepEqual(store.get().facts.facts.map(item => item.fact_id), ["f1", "f2"]);
  } finally {
    controller.destroy();
    global.requestAnimationFrame = originalFrame;
    global.cancelAnimationFrame = originalCancel;
  }
});


test("console controller reloads authoritative facts for type and abnormal status filters", async () => {
  const originalFrame = global.requestAnimationFrame;
  const originalCancel = global.cancelAnimationFrame;
  global.requestAnimationFrame = callback => { callback(); return 1; };
  global.cancelAnimationFrame = () => {};
  const store = ConsoleStore.create();
  const requests = [];
  const api = {
    console: async () => ({ initial_context_id: "c1", nodes: [{ context_id: "c1" }], edges: [] }),
    conversation: async () => ({ context_id: "c1", revision: { revision_id: "r1" }, messages: [], range: { start: 0, end: 0 }, has_more: false }),
    facts: async (_loopId, options) => {
      requests.push({ kind: options.kind ?? null, status: options.status ?? null });
      return { facts: [], range: { start: 0, end: 0 }, has_more: false, next_before: null };
    },
  };
  const controller = ConsoleController.create({ api, store });
  try {
    await controller.load("l1");
    await controller.setFactFilter("test");
    await controller.setFactStatus("failed");
    assert.deepEqual(requests, [
      { kind: null, status: null },
      { kind: "test", status: null },
      { kind: "test", status: "failed" },
    ]);
  } finally {
    controller.destroy();
    global.requestAnimationFrame = originalFrame;
    global.cancelAnimationFrame = originalCancel;
  }
});


test("console controller binds and disconnects native pagination and resize observers", () => {
  const originals = {
    frame: global.requestAnimationFrame,
    cancel: global.cancelAnimationFrame,
    intersection: global.IntersectionObserver,
    resize: global.ResizeObserver,
  };
  global.requestAnimationFrame = callback => { callback(); return 1; };
  global.cancelAnimationFrame = () => {};
  const intersections = [];
  const resizes = [];
  global.IntersectionObserver = class {
    constructor(callback, options) { this.callback = callback; this.options = options; this.disconnected = false; intersections.push(this); }
    observe(target) { this.target = target; }
    disconnect() { this.disconnected = true; }
  };
  global.ResizeObserver = class {
    constructor(callback) { this.callback = callback; this.disconnected = false; resizes.push(this); }
    observe(target) { this.target = target; }
    disconnect() { this.disconnected = true; }
  };
  const historySentinel = {};
  const newerSentinel = {};
  const factSentinel = {};
  const transcript = {
    dataset: { contextId: "c1" },
    scrollTop: 0,
    querySelector: selector => ({
      "[data-loop-history-sentinel]": historySentinel,
      "[data-loop-newer-sentinel]": newerSentinel,
    })[selector] || null,
  };
  const factList = { querySelector: selector => selector === "[data-loop-fact-sentinel]" ? factSentinel : null };
  const layout = { style: { gridTemplateColumns: "", removeProperty() {} }, getBoundingClientRect: () => ({ left: 0, width: 1000 }) };
  const listeners = new Map();
  const handle = {
    addEventListener(type, callback) { listeners.set(type, callback); },
    removeEventListener(type) { listeners.delete(type); },
    setAttribute() {},
    hasPointerCapture() { return false; },
  };
  const container = {
    querySelector(selector) {
      return ({
        ".loop-console-main": layout,
        "[data-loop-console-resizer]": handle,
        "[data-loop-transcript]": transcript,
        "[data-loop-fact-list]": factList,
      })[selector] || null;
    },
  };
  const store = ConsoleStore.create();
  const controller = ConsoleController.create({ api: {}, store });
  try {
    controller.bind(container);
    assert.equal(intersections.length, 3);
    assert.equal(intersections[0].options.root, transcript);
    assert.equal(intersections[1].options.root, transcript);
    assert.equal(intersections[2].options.root, factList);
    assert.equal(resizes.length, 1);
    assert.equal(resizes[0].target, layout);
  } finally {
    controller.destroy();
    assert.ok(intersections.every(observer => observer.disconnected));
    assert.ok(resizes.every(observer => observer.disconnected));
    global.requestAnimationFrame = originals.frame;
    global.cancelAnimationFrame = originals.cancel;
    global.IntersectionObserver = originals.intersection;
    global.ResizeObserver = originals.resize;
  }
});


test("portfolio, conversation and facts views expose selected Context without polluting message body", () => {
  const manifest = { health: "observing", nodes: [{ context_id: "c1", title: "Tests", topic: "Testing", purpose: "Verify failures", status: "active", revision: { generation: 2 }, counts: { runs: 3 } }], edges: [] };
  const map = PortfolioMap.render(manifest, "c1");
  assert.match(map, /Testing/);
  assert.match(map, /data-action="loop-select-context"/);
  const state = { manifest, selectedContextId: "c1", interventionMode: "direct_context_message", messageFilter: "all", messageSearch: "", factFilter: "all", factScope: "current", conversation: { revision: { generation: 2 }, total: 2, has_more: false, messages: [{ index: 0, message: { role: "human", content: "Run tests" }, provenance: { source_kind: "delegated_patrol" } }, { index: 1, message: { id: "compressed-1", role: "human", content: "Prior summary", compression: { source: [{ id: "old-1", role: "tool", content: "large output" }] } } }] }, facts: { facts: [{ fact_id: "f1", context_id: "c1", kind: "test", status: "verified", title: "测试结果", summary: "12 passed", metrics: { passed: 12, failed: 0, skipped: 0, count_status: "exact" }, evidence: { message_id: "m1" } }] } };
  const conversation = Conversation.render(state);
  assert.match(conversation, /完整 Context 会话/);
  assert.match(conversation, /Patrol delegated/);
  assert.match(conversation, />Run tests</);
  assert.match(conversation, /data-action="loop-restore-compression"/);
  assert.match(conversation, /data-context-id="c1" data-message-id="compressed-1"/);
  const facts = Facts.render(state);
  assert.match(facts, /12 passed/);
});


test("portfolio map renders every source edge for a multi-parent Context", () => {
  const manifest = {
    health: "observing",
    nodes: [
      { context_id: "implementation", topic: "Implementation", purpose: "Build", status: "active", counts: {} },
      { context_id: "testing", topic: "Testing", purpose: "Verify", status: "active", counts: {} },
      { context_id: "release", topic: "Release", purpose: "Synthesize", status: "active", counts: {} },
    ],
    edges: [
      { source_context_id: "implementation", target_context_id: "release" },
      { source_context_id: "testing", target_context_id: "release" },
    ],
  };
  const html = PortfolioMap.render(manifest, "release");
  assert.equal((html.match(/<path /g) || []).length, 2);
  assert.match(html, /3<\/strong> 个 Context · Evolution Graph/);
});


test("portfolio node and conversation header render a shared descriptor only once", () => {
  const descriptor = "建立一个可构建、可安装、可加载的 Obsidian 插件最小仓库骨架和测试基线";
  const manifest = { health: "observing", nodes: [{ context_id: "c1", title: descriptor, topic: descriptor, purpose: descriptor, status: "active", revision: { generation: 1 }, counts: { runs: 1 } }], edges: [] };

  const map = PortfolioMap.render(manifest, "c1");
  assert.equal(map.split(descriptor).length - 1, 1, "拓扑图节点不得重复渲染同一段描述");
  assert.doesNotMatch(map, /context-node-purpose/);

  const state = { manifest, selectedContextId: "c1", interventionMode: "direct_context_message", messageFilter: "all", messageSearch: "", factFilter: "all", factScope: "current", conversation: { revision: { generation: 1 }, total: 1, has_more: false, messages: [] } };
  const conversation = Conversation.render(state);
  assert.equal(conversation.split(descriptor).length - 1, 1, "会话页头不得重复渲染同一段描述");
  assert.match(conversation, /条消息/);

  const distinct = PortfolioMap.render({ health: "observing", nodes: [{ context_id: "c2", topic: "Testing", purpose: "Verify failures", status: "active", counts: {} }], edges: [] }, "c2");
  assert.match(distinct, /context-node-purpose/, "名称与描述不同时必须保留描述行");
});


test("api replays persisted SSE frames by cursor", async () => {
  const encoder = new TextEncoder();
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode('id: e8\nevent: RoundObserved\ndata: {"event_id":"e8","cursor":8,"type":"RoundObserved"}\n\n'));
      controller.close();
    },
  });
  const api = LoopApi.create({ apiBase: "http://focus", session: "secret" }, async () => ({ ok: true, body }));
  const received = [];
  const cursor = await api.stream("l1", 7, events => received.push(...events));
  assert.equal(cursor, 8);
  assert.equal(received[0].event_id, "e8");
});


test("portfolio and evolution preserve keep and secondary sources", () => {
  const previous = { lanes: [{ lane_id: "a", revision_id: "r1" }] };
  const current = { lanes: [{ lane_id: "a", revision_id: "r1", purpose: "test" }, { lane_id: "b", revision_id: "r2", purpose: "review" }] };
  assert.deepEqual(Portfolio.diff(previous, current).map(item => item.change), ["keep", "create"]);
  const graph = Evolution.project({ revisions: [{ revision_id: "r3", context_id: "c", generation: 3 }], edges: [{ target_revision_id: "r3", source_revision_id: "r1", position: 0 }, { target_revision_id: "r3", source_revision_id: "r2", position: 1 }] });
  assert.equal(graph[0].sources.length, 2);
  assert.equal(graph[0].first_parent.source_revision_id, "r1");
  const rendered = Evolution.render({ revisions: [{ revision_id: "r3", context_id: "c", generation: 3 }], edges: [] }, {}, { final_path: [{ context_id: "c" }] });
  assert.match(rendered, /data-action="open-loop-revision"/);
  assert.match(rendered, /is-adopted/);
});


test("delegated source badge stays outside message content", () => {
  const message = { content: "Run tests." };
  const badge = Provenance.badge({ source_kind: "delegated_patrol", provenance_id: "p1" });
  assert.equal(message.content, "Run tests.");
  assert.match(badge, /Patrol 依据授权生成/);
  assert.doesNotMatch(message.content, /Patrol|delegated/);
});


test("loop view exposes every hard portfolio budget", () => {
  const start = LoopView.render(null, { title: "Ship Focus", active_run: { run_id: "run-initial" } });
  assert.match(start, /name="autonomousCompression" type="checkbox" checked/);
  assert.match(start, /原文保留可恢复，授权可随时撤销/);
  for (const field of ["maxRounds", "maxDurationSeconds", "maxModelCalls", "maxInputTokens", "maxOutputTokens", "maxRetries", "maxLanes", "maxContexts", "maxProviders", "maxNewLanesPerRound", "maxConcurrentRuns", "maxNoProgress"]) {
    assert.match(start, new RegExp(`name="${field}"`));
  }
  assert.doesNotMatch(start, /disabled>授权 Patrol 并启动/);
  const terminalStart = LoopView.render(null, { title: "Ship Focus", latest_direct_user_run: { run_id: "run-finished", status: "success" } });
  assert.match(terminalStart, /run-finished（success）/);
  assert.doesNotMatch(terminalStart, /disabled>授权 Patrol 并启动/);
  const dashboard = LoopView.render({
    snapshot: {
      loop_id: "l1", status: "running", health: "observing", holder_id: "patrol:l1",
      goal_revision: 1, authority_revision: 1, goal: { goal: "Ship Focus" },
      usage: { rounds: 2, duration_seconds: 90, model_calls: 4, input_tokens: 512, output_tokens: 128, retries: 1, lanes: 3, contexts: 5, providers: 2 },
      grant: { capabilities: ["continue_context"], context_scope: ["c1"], permission_scope: ["read"], delegable_gates: [], budgets: { max_rounds: 20, max_duration_seconds: 3600, max_model_calls: 200, max_input_tokens: 10000, max_output_tokens: 2000, max_retries: 4, max_lanes: 8, max_contexts: 16, max_providers: 4, max_new_lanes_per_round: 3, max_concurrent_runs: 4, max_no_progress: 3 } },
    },
    related: {},
  });
  for (const label of ["Rounds 2 / 20", "Duration 90 / 3600s", "Calls 4 / 200", "Input 512 / 10000", "Output 128 / 2000", "Retries 1 / 4", "Lanes 3 / 8", "Contexts 5 / 16", "Providers 2 / 4", "撤销 Patrol 授权"]) {
    assert.match(dashboard, new RegExp(label));
  }
});

test("loop view shows one active mission revision and labels legacy history", () => {
  const html = LoopView.render({
    snapshot: {
      loop_id: "mission-history", status: "running", health: "observing", active_mission_revision: 2, goal_revision: 2, authority_revision: 3, usage: {},
      mission: { revision: 2, outcome: "交付 Live Loop", boundaries: { in_scope: [], required_invariants: [], prohibited_actions: [] }, completion_checks: [{ check_id: "tests", claim: "测试通过", expected_evidence_kinds: ["test"] }] },
      grant: { capabilities: [], context_scope: [], permission_scope: [], delegable_gates: [], budgets: {} },
    },
    related: { audit: { runs: [], mission_history: { active_revision: 2, revisions: [
      { kind: "structured_mission", revision: 2, active: true, outcome: "交付 Live Loop" },
      { kind: "legacy_goal_contract", revision: 1, active: false, goal: "旧目标", task_contract: "旧 Task Contract 原文" },
    ] } } },
  }, {}, { manifest: { nodes: [] } });
  assert.match(html, /交付 Live Loop · Mission R2/);
  assert.match(html, /tests/);
  assert.match(html, /Mission R2 · 当前/);
  assert.match(html, /旧版 Goal Contract R1/);
  assert.match(html, /旧 Task Contract 原文/);
  assert.equal((html.match(/· 当前/g) || []).length, 1);
});

test("loop view exposes autonomous compression status without injecting it into conversation", () => {
  const html = LoopView.render({
    snapshot: {
      loop_id: "l-compression", status: "running", health: "resuming", goal_revision: 1, authority_revision: 1,
      goal: { goal: "Long task" }, usage: {},
      grant: { capabilities: ["apply_context_compression"], context_scope: ["context-1"], permission_scope: ["read"], delegable_gates: ["compression"], compression_policy: { version: 1 }, budgets: {} },
    },
    related: { audit: {
      pending_decisions: [{ pending_decision_id: "pending-1", kind: "compression", status: "resolving", payload: { context_id: "context-1" } }],
      decisions: [{ decision_id: "decision-1", rationale: "Keep this execution identity and remove obsolete debugging history." }],
      compression_candidates: [{ candidate_id: "candidate-1", context_id: "context-1", status: "accepted", estimated_reduction: 1400, before_tokens: 2000, after_tokens: 600, protection_evidence: [{ reason: "current_direct_user_message", overlap: false }], source_ranges: [{ source_ids: ["m1", "m2"] }] }],
      compression_resolutions: [{ resolution_id: "resolution-1", decision_id: "decision-1", run_id: "run-1", status: "resuming", actual_before_tokens: null, actual_after_tokens: null, actual_reduction: null }],
    } },
  }, {}, { manifest: { nodes: [] } });
  assert.match(html, /Patrol Context 压缩 · 正在恢复 Run/);
  assert.match(html, /预计减少 1400 tokens/);
  assert.match(html, /等待稳定 checkpoint 证据/);
  assert.match(html, /current_direct_user_message/);
  assert.match(html, /Keep this execution identity/);
  assert.match(html, /打开完整 Context 会话并查看\/恢复来源/);
  assert.match(html, /允许 Patrol 自主压缩当前 Context/);
  assert.doesNotMatch(html, /Delegated HumanMessage.*compression/s);
});

test("loop view labels checkpoint-derived compression usage as actual", () => {
  const html = LoopView.render({
    snapshot: {
      loop_id: "l-compression", status: "running", health: "observing", goal_revision: 1, authority_revision: 1,
      goal: { goal: "Long task" }, usage: {}, grant: { budgets: {} },
    },
    related: { audit: {
      compression_candidates: [{ candidate_id: "candidate-1", context_id: "context-1", status: "accepted", estimated_reduction: 1400, before_tokens: 2000, after_tokens: 600, source_ranges: [{ source_ids: ["m1", "m2"] }] }],
      compression_resolutions: [{ resolution_id: "resolution-1", status: "applied", actual_before_tokens: 1800, actual_after_tokens: 320, actual_reduction: 1480 }],
    } },
  }, {}, { manifest: { nodes: [] } });
  assert.match(html, /实际减少 1480 tokens/);
  assert.match(html, /1800 → 320/);
  assert.doesNotMatch(html, /等待稳定 checkpoint 证据/);
});


test("loop view keeps long goals compact and terminal history has explicit exits", () => {
  const longGoal = "修复 Teleport Kubernetes Service 中 kubectl exec 交互会话失败及相关 Forwarder 生命周期问题。".repeat(8);
  const state = {
    snapshot: {
      loop_id: "terminal-loop",
      status: "stopped",
      health: "idle",
      goal_revision: 1,
      authority_revision: 1,
      goal: { goal: longGoal, task_contract: "保留现有行为", acceptance_criteria: [{ criterion_id: "c1", text: "测试通过" }] },
      usage: { rounds: 1, model_calls: 3, contexts: 1 },
      grant: { budgets: { max_rounds: 20, max_model_calls: 150 } },
    },
    related: {},
  };
  const html = LoopView.render(state, {}, { manifest: { nodes: [{ context_id: "root" }] } });
  assert.match(html, /<h2>Portfolio Map<\/h2>/);
  assert.match(html, /<details class="loop-goal-disclosure">/);
  assert.doesNotMatch(html, new RegExp(`<h2>${longGoal}`));
  assert.match(html, /data-action="loop-exit"/);
  assert.match(html, /data-action="loop-prepare-new"/);
  assert.match(html, /退出不会删除审计记录/);
});


test("terminal conversation is read-only and a successor needs an unbound direct run", () => {
  const manifest = { nodes: [{ context_id: "c1", title: "Primary", purpose: "Execute", status: "success", counts: {} }] };
  const terminal = Conversation.render({
    manifest,
    selectedContextId: "c1",
    terminal: true,
    interventionMode: "direct_context_message",
    messageFilter: "all",
    messageSearch: "",
    conversation: { messages: [], range: { start: 0 }, total: 0, has_more: false },
  });
  assert.match(terminal, /历史只读/);
  assert.doesNotMatch(terminal, /id="loopInterventionForm"/);

  const blocked = LoopView.render(null, { title: "Next", latest_direct_user_run: { run_id: "used", origin: "direct_user", loop_id: "old-loop" } });
  assert.match(blocked, /旧 Run 已归属于历史 Loop/);
  assert.match(blocked, /disabled>授权 Patrol 并启动/);
  const ready = LoopView.render(null, { title: "Next", latest_direct_user_run: { run_id: "fresh", origin: "direct_user", loop_id: null } });
  assert.match(ready, /fresh（unknown）/);
  assert.doesNotMatch(ready, /disabled>授权 Patrol 并启动/);
});

test("option 3 renders committed Patrol activity and connection recovery without hiding the workspace", () => {
  const state = {
    snapshot: {
      loop_id: "l1", status: "running", health: "observing", waiting_reason: null,
      mission: { outcome: "交付 Live Loop", boundaries: {}, completion_checks: [] },
      active_mission_revision: 1, goal_revision: 1, authority_revision: 1,
      usage: { rounds: 12, contexts: 3 }, grant: { budgets: { max_rounds: 20 }, capabilities: [], context_scope: [], permission_scope: [], delegable_gates: [] },
    },
    related: {},
    connection: { status: "resyncing" },
    live: {
      round: { state: { number: 12 } },
      patrol_session: { state: { phase: "curating", status: "running", safe_summary: "向 3 个 Curator 分派证据检查", wait_reason: null } },
      curators: {
        a: { updated_sequence: 5, state: { state: "analyzing", scope: "testing", safe_summary: "检查失败测试" } },
        b: { updated_sequence: 4, state: { state: "reading", scope: "implementation", safe_summary: "读取 workspace" } },
      },
      activity_timeline: [{ event_id: "e5", entity_type: "curator", kind: "curator.analyzing", summary: "Testing Curator 正在分析", occurred_at: "2026-09-19T10:31:07Z" }],
    },
  };
  const html = LoopView.render(state, {}, { manifest: { nodes: [], edges: [] }, facts: { facts: [] }, graphActivity: [] });
  assert.match(html, /patrol-activity-rail/);
  assert.match(html, /向 3 个 Curator 分派证据检查/);
  assert.match(html, /查看记录/);
  assert.match(html, /Testing Curator 正在分析/);
  assert.match(html, /重同步/);
});

test("option 3 shows three simultaneous Context states and real directive causality", () => {
  const manifest = {
    health: "observing",
    nodes: [
      { context_id: "c1", title: "Implementation", status: "active", latest_run: { run_id: "r1", status: "running", origin: "patrol", input_tokens: 10, output_tokens: 5 }, counts: {} },
      { context_id: "c2", title: "Testing", status: "active", latest_run: { run_id: "r2", status: "pending", origin: "patrol", input_tokens: 20, output_tokens: 7 }, counts: {} },
      { context_id: "c3", title: "Review", status: "active", latest_run: { run_id: "r3", status: "success", origin: "user", input_tokens: 5, output_tokens: 3 }, counts: {} },
    ],
    edges: [],
  };
  const html = PortfolioMap.render(manifest, "c2", [{ id: "d1", state: "delivered", origin: "patrol", target_context_id: "c2", run: { status: "running" } }]);
  assert.equal((html.match(/context-node-live/g) || []).length, 3);
  assert.match(html, /data-directive-id="d1"/);
  assert.match(html, /directive-path is-delivered is-active/);
});

test("option 3 conversation causality and facts use stable committed identities", () => {
  const conversation = Conversation.render({
    manifest: { nodes: [{ context_id: "c1", title: "Testing", topic: "Testing", purpose: "Verify", status: "active", counts: {} }] },
    selectedContextId: "c1",
    conversation: { messages: [], range: { start: 0, end: 0 }, total: 0 },
    causality: [{ entity_type: "directive", kind: "directive.authorized", summary: "Kernel 已授权" }],
    messageFilter: "all", messageSearch: "", interventionMode: "direct_context_message",
  });
  const facts = Facts.render({ facts: { facts: [{ fact_id: "f1", revision: 3, context_id: "c1", kind: "test", status: "verified", title: "Regression", summary: "18 passed" }] }, factFilter: "all", factStatus: "all", factScope: "all" });
  assert.match(conversation, /Live causality/);
  assert.match(conversation, /Kernel 已授权/);
  assert.match(facts, /data-fact-id="f1"/);
  assert.match(facts, /data-fact-revision="3"/);
});
