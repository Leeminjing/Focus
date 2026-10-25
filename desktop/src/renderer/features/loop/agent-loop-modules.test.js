/* Shared Loop API, graph, fact timeline and Observation regressions used by Patrol. */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const LoopApi = require("./loop-api.js");
const PortfolioMap = require("../map/portfolio-map-view.js");
const PatrolView = require("../patrol/workspace/workspace-patrol-view.js");
const ObservationView = require("../patrol/workspace/observation-view.js");
const Facts = require("./loop-facts-view.js");

test("api preserves a plain-text HTTP failure instead of exposing JSON syntax errors", async () => {
  const api = LoopApi.create({ apiBase: "http://focus", session: "secret" }, async () => new Response("Internal Server Error", {
    status: 500,
    statusText: "Internal Server Error",
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  }));
  await assert.rejects(api.workspacePatrol("workspace"), error => {
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


test("shared api exposes topology, paginated conversation and facts", async () => {
  const calls = [];
  const api = LoopApi.create({ apiBase: "http://focus", session: "secret" }, async (url, options = {}) => {
    calls.push({ url, options });
    return { ok: true, json: async () => url.endsWith("/tasks/c%2F1") ? { ui_state: { _main_run_equipment: { permissions: ["read"], skills: ["testing"], access_mode: "workspace" } } } : {} };
  });
  await api.console("l 1");
  await api.conversation("l 1", "c/1", { before: 48, limit: 24 });
  await api.facts("l 1", { contextId: "c/1", kind: "test" });
  assert.equal(calls[0].url, "http://focus/desktop/api/agent-loops/l%201/console");
  assert.match(calls[1].url, /conversation\?before=48&limit=24$/);
  assert.match(calls[2].url, /facts\?context_id=c%2F1&kind=test$/);
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
  assert.match(html, /3<\/strong> 个 Context · 来源关系/);
});


test("portfolio node and Context inspection render a shared descriptor only once", () => {
  const descriptor = "建立一个可构建、可安装、可加载的 Obsidian 插件最小仓库骨架和测试基线";
  const manifest = { health: "observing", nodes: [{ context_id: "c1", title: descriptor, topic: descriptor, purpose: descriptor, status: "active", revision: { generation: 1 }, counts: { runs: 1 } }], edges: [] };

  const map = PortfolioMap.render(manifest, "c1");
  assert.equal(map.split(descriptor).length - 1, 1, "拓扑图节点不得重复渲染同一段描述");
  assert.doesNotMatch(map, /context-node-purpose/);

  const distinct = PortfolioMap.render({ health: "observing", nodes: [{ context_id: "c2", topic: "Testing", purpose: "Verify failures", status: "active", counts: {} }], edges: [] }, "c2");
  assert.doesNotMatch(distinct, /context-node-purpose/, "紧凑节点将目的保留在按需检查中");
  const inspection = PatrolView.context({context_id:"c2"}, {title:"Testing",purpose:"Verify failures"}, "overview");
  assert.match(inspection, /Verify failures/, "独立目的在概要检查中仍可访问");
});


test("portfolio map derives layers from cross-context edges and renders direction", () => {
  const manifest = {
    health: "observing",
    nodes: [
      { context_id: "primary", topic: "Primary execution", purpose: "Primary execution", status: "active", counts: {} },
      { context_id: "derived", topic: "Derived work", purpose: "Derived work", status: "active", counts: {} },
      { context_id: "isolated", topic: "Independent work", purpose: "Independent work", status: "active", counts: {} },
    ],
    edges: [
      { source_context_id: "primary", target_context_id: "primary", target_revision_id: "rev-self" },
      { source_context_id: "primary", target_context_id: "derived", target_revision_id: "rev-derived" },
    ],
  };
  const html = PortfolioMap.render(manifest, "derived");

  assert.equal((html.match(/data-edge-id=/g) || []).length, 1, "自环不得作为拓扑连线渲染");
  assert.match(html, /<title>primary → rev-derived<\/title>/);
  assert.match(html, /marker-end="url\(#portfolio-edge-arrow\)"/, "派生连线必须带方向标记");
  const leftOf = id => {
    const tag = new RegExp(`<button[^>]*data-context-id="${id}"[^>]*>`).exec(html)[0];
    return Number(/left:(\d+)px/.exec(tag)[1]);
  };
  assert.ok(leftOf("derived") > leftOf("primary"), "横向派生层级必须严格深于来源");
  assert.equal(leftOf("isolated"), leftOf("primary"), "不参与任何派生依赖的 Context 必须稳定落在根层");
});


test("distinct source revisions keep exact edges across repeated DOM reconciliation", () => {
  const previousDocument = global.document;
  const document = require("../../../../tests/helpers/test-dom.cjs").createDocument();
  global.document = document;
  const host = document.createElement("div");
  const manifest = {
    nodes: [{context_id:"A", title:"A"}, {context_id:"B", title:"B"}],
    edges: ["A1", "A2"].map(source_revision_id => ({source_context_id:"A", source_revision_id, target_context_id:"B", target_revision_id:"B1"})),
  };
  try {
    host.innerHTML = PortfolioMap.render(manifest);
    for (let index = 0; index < 4; index++) {
      assert.equal(PortfolioMap.reconcile(host, manifest), true);
      const edges = host.querySelectorAll("path[data-edge-id]");
      assert.equal(edges.length, 2);
      assert.equal(new Set(edges.map(edge => edge.getAttribute("data-edge-id"))).size, 2);
      assert.deepEqual(edges.map(edge => edge.querySelector("title").textContent).sort(), ["A1 → B1", "A2 → B1"]);
    }
    PortfolioMap.reconcile(host, {...manifest, edges:[manifest.edges[1]]});
    assert.equal(host.querySelectorAll("path[data-edge-id]").length, 1);
    assert.equal(host.querySelector("path[data-edge-id] title").textContent, "A2 → B1");
  } finally { global.document = previousDocument; }
});

test("Observation distinguishes inventory pagination from source and history completeness", () => {
  const summary = {
    observation_id:"O1", round_number:1, availability:"available", evidence_visible:true,
    frozen_at:"2026-10-10T00:00:00Z", previous_progress:{progress_id:"P0",item_count:0,history_complete:false},
    sources:{total:1,complete:false}, lineage:{node_count:1,edge_count:0,complete:true},
  };
  const html = ObservationView.render(summary, {sources:{items:[{kind:"test",source_id:"S1"}],total:1,has_more:false}});
  assert.match(html, /冻结来源覆盖：不完整/);
  assert.match(html, /前序历史覆盖：不完整/);
  assert.match(html, /来源关系覆盖：完整/);
  assert.match(html, /当前冻结清单已读取 1 \/ 1 项/);
  assert.doesNotMatch(html, /加载下一页/);
  const legacy = ObservationView.render({...summary,availability:"legacy",sources:{total:0},previous_progress:{item_count:0},lineage:{node_count:0}});
  assert.match(legacy, /冻结来源覆盖：未确认/);
  assert.match(legacy, /前序历史覆盖：未确认/);
  assert.match(legacy, /不使用当前 Progress 回填/);
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
  for (const status of ["执行中", "待执行", "有运行结果"]) assert.ok(html.includes(status), status);
  assert.equal((html.match(/class="portfolio-context-node/g) || []).length, 3);
  assert.match(html, /data-directive-id="d1"/);
  assert.match(html, /directive-path is-delivered is-active/);
});

test("fact timeline retains committed identity, revision and evidence summary", () => {
  const html = Facts.renderTimeline([{ fact_id: "f1", revision: 3, context_id: "c1", kind: "test", status: "verified", title: "Regression", summary: "18 passed" }]);
  assert.match(html, /data-fact-id="f1"/);
  assert.match(html, /data-fact-revision="3"/);
  assert.match(html, /18 passed/);
});
