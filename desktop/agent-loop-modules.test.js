/*
 * 本文件验证 Loop Store 游标恢复、API/历史快照协议、Portfolio diff、多来源图与 provenance 外置渲染。
 * 输入为重复/乱序事件、模拟 fetch、Lane revisions 和多父边；输出为幂等 cursor、正确请求、完整
 * secondary source 与不污染消息正文的 badge 断言。具体工作流为直接加载无 DOM UMD 模块并调用
 * 纯函数；示例：`node --test desktop/agent-loop-modules.test.js`。
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const LoopApi = require("./loop-api.js");
const LoopStore = require("./loop-store.js");
const LoopView = require("./loop-view.js");
const Portfolio = require("./portfolio-view.js");
const Evolution = require("./context-evolution-view.js");
const Provenance = require("./message-provenance-view.js");


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
