/*
 * 本文件对外提供前端 Live Loop schema、reducer、序列防护、连接恢复、选择器与界面状态的回归测试。
 * 输入为有效/畸形 snapshot、重复/陈旧/缺口事件、HTTP 故障和模拟 Live API；输出为确定性 projection、原子重同步、取消信号、退避及单连接断言。
 * 具体工作流为使用 Node test 直接加载 UMD 模块并驱动 Store/Connection/API，验证新 Round/Patrol 的局部版本与迟到旧实体隔离；示例：`node --test desktop/loop-live-projection.test.js`。
 * Mission交付使用服务端评估，覆盖Patrol真实来源、终态Run身份保留及跨Mission事件隔离。
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const Schema = require("./loop-live-schema.js");
const Reducer = require("./loop-live-reducer.js");
const Selectors = require("./loop-live-selectors.js");
const LiveStore = require("./loop-live-store.js");
const Connection = require("./loop-live-connection.js");
const LoopApi = require("./loop-api.js");
const LoopStore = require("./loop-store.js");
const PortfolioMap = require("./portfolio-map-view.js");

const entity = (id, revision, sequence, state = {}) => ({ entity_id: id, revision, updated_sequence: sequence, state });
const snapshot = (sequence = 0, patch = {}) => ({
  loop_id: "l1",
  last_sequence: sequence,
  loop: sequence ? entity("l1", 1, sequence, { status: "running", health: "observing" }) : null,
  mission: null,
  patrol_session: null,
  round: null,
  contexts: {},
  runs: {},
  curators: {},
  expansions: {},
  directives: {},
  facts: {},
  portfolio: null,
  activity_timeline: [],
  unknown_kinds: [],
  diagnostics: { journal_last_sequence: sequence, projector_last_sequence: sequence, lag: 0, rebuilt: false, updated_at: null },
  ...patch,
});
const event = (sequence, patch = {}) => ({
  event_id: `e${sequence}`,
  loop_id: "l1",
  sequence,
  schema_version: 1,
  kind: "context.run.started",
  entity_type: "run",
  entity_id: "r1",
  entity_revision: sequence,
  correlation_id: "corr-1",
  causation_id: null,
  visibility: { audience: "loop_member", required_permissions: [], evidence_fields: [] },
  payload: { context_id: "c1", status: "running" },
  idempotency_key: `event-${sequence}`,
  occurred_at: "2026-09-19T00:00:00Z",
  retained_until: null,
  ...patch,
});

test("schema loads a normalized snapshot and rejects malformed required state", () => {
  const normalized = Schema.validateSnapshot(snapshot(2, { contexts: { c1: entity("c1", 1, 2, { title: "Testing" }) } }));
  assert.equal(normalized.contexts.c1.state.title, "Testing");
  assert.ok(Object.isFrozen(normalized.contexts));
  assert.throws(() => Schema.validateSnapshot({ loop_id: "l1", last_sequence: "2" }), /last_sequence/);
  assert.throws(() => Schema.validateSnapshot(snapshot(2, { contexts: { wrong: entity("c1", 1, 2) } })), /集合键不一致/);
});

test("lineage events land in the projection and selectors drop self edges and missing endpoints", () => {
  const lineage = {
    "c1:c1": entity("c1:c1", 2, 3, { source_context_id: "c1", target_context_id: "c1" }),
    "c1:c2": entity("c1:c2", 2, 3, { source_context_id: "c1", target_context_id: "c2", source_revision_id: "r1", target_revision_id: "r2" }),
    "c9:c2": entity("c9:c2", 2, 3, { source_context_id: "c9", target_context_id: "c2" }),
  };
  const initial = Schema.validateSnapshot(
    snapshot(3, { contexts: { c1: entity("c1", 1, 1), c2: entity("c2", 1, 1) }, lineage }),
  );
  assert.equal(initial.lineage["c1:c2"].state.source_context_id, "c1", "快照必须保留权威 lineage 集合");
  assert.deepEqual(Selectors.selectLineage(initial), [
    { source_context_id: "c1", target_context_id: "c2", source_revision_id: "r1", target_revision_id: "r2" },
  ], "选择器必须去掉自环与来源已不在 portfolio 的边");

  const reduced = Reducer.reduce(
    Schema.validateSnapshot(snapshot(3, { contexts: { c1: entity("c1", 1, 1), c2: entity("c2", 1, 1), c3: entity("c3", 1, 3) } })),
    event(4, { kind: "context.lineage.derived", entity_type: "context_lineage", entity_id: "c2:c3", entity_revision: 2, payload: { source_context_id: "c2", target_context_id: "c3", source_revision_id: "r2", target_revision_id: "r3" } }),
  );
  assert.equal(reduced.lineage["c2:c3"].state.target_context_id, "c3", "lineage 事件必须归约进 lineage 集合");
  assert.equal(reduced.unknown_kinds.length, 0, "context_lineage 是已知实体类型，不得记为未知 kind");
});


test("reducer is deterministic, deduplicates sequence and ignores stale entity revisions", () => {
  const initial = Schema.validateSnapshot(snapshot());
  const first = Reducer.reduce(initial, event(1));
  assert.equal(Reducer.reduce(first, event(1)), first);
  const stale = Reducer.reduce(first, event(2, { entity_revision: 1, payload: { status: "success" } }));
  assert.equal(stale.runs.r1.state.status, "running");
  assert.equal(stale.last_sequence, 2);
  const replayed = Reducer.reduceBatch(initial, [event(1), event(2, { entity_revision: 2, payload: { context_id: "c1", status: "success" } })]);
  assert.deepEqual(replayed, Reducer.reduceBatch(initial, [event(1), event(2, { entity_revision: 2, payload: { context_id: "c1", status: "success" } })]));
});

test("Round and Patrol identity switches use local revisions and reject late old-round events", () => {
  let state = Reducer.reduce(snapshot(), event(1, { entity_type: "round", entity_id: "round-1", entity_revision: 5, payload: { number: 1, decision_id: "old-decision" } }));
  state = Reducer.reduce(state, event(2, { entity_type: "patrol_session", entity_id: "patrol-1", entity_revision: 20, payload: { round_id: "round-1", phase: "completed" } }));
  state = Reducer.reduce(state, event(3, { entity_type: "round", entity_id: "round-2", entity_revision: 1, payload: { number: 2 } }));
  assert.equal(state.round.entity_id, "round-2");
  assert.equal(state.round.state.decision_id, undefined);
  assert.equal(state.patrol_session, null);
  state = Reducer.reduce(state, event(4, { entity_type: "patrol_session", entity_id: "patrol-2", entity_revision: 1, payload: { round_id: "round-2", phase: "created" } }));
  state = Reducer.reduce(state, event(5, { entity_type: "round", entity_id: "round-1", entity_revision: 6, payload: { number: 1 } }));
  state = Reducer.reduce(state, event(6, { entity_type: "patrol_session", entity_id: "patrol-1", entity_revision: 21, payload: { round_id: "round-1", phase: "cancelled" } }));
  assert.equal(state.round.entity_id, "round-2");
  assert.equal(state.patrol_session.entity_id, "patrol-2");
  assert.equal(state.last_sequence, 6);
});

test("Patrol Mission delivery uses the authoritative assessment and retains its settled Run", () => {
  const store = LoopStore.create();
  store.load({ goal_revision: 1, mission_delivery: { state: "pending", mission_revision: 1 } });
  const delivery = { state: "delivered", mission_revision: 1, directive_id: "d1", run_id: "r1", reason: null };
  const current = snapshot(2, {
    loop: entity("l1", 2, 2, { goal_revision: 1, status: "running" }),
    directives: { d1: entity("d1", 4, 2, { origin: "patrol", state: "settled", mission_revision: 1,
      run_id: null, mission_delivery: delivery }) },
  });
  store.projectLive(current);
  assert.deepEqual(store.get().snapshot.mission_delivery, delivery);
  const newer = { ...delivery, mission_revision: 2, state: "authorized", directive_id: "d2", run_id: null };
  store.projectLive(snapshot(4, { loop: entity("l1", 3, 4, { goal_revision: 2 }), directives: {
    ...current.directives, d2: entity("d2", 2, 3, { origin: "patrol", mission_revision: 2, mission_delivery: newer }),
    late: entity("late", 7, 4, { origin: "patrol", mission_revision: 1, mission_delivery: delivery }),
  } }));
  assert.deepEqual(store.get().snapshot.mission_delivery, newer);
});

test("ordinary Patrol directives cannot assert Mission delivery without its assessment", () => {
  const store = LoopStore.create();
  store.load({ goal_revision: 1, mission_delivery: { state: "pending", mission_revision: 1 } });
  store.projectLive(snapshot(2, { loop: entity("l1", 1, 1, { goal_revision: 1 }),
    directives: { d1: entity("d1", 3, 2, { origin: "patrol", mission_revision: 1, run_id: "r1", state: "run_started" }) } }));
  assert.equal(store.get().snapshot.mission_delivery.state, "pending");
  store.projectLive(snapshot(3, { loop: entity("l1", 1, 1, { goal_revision: 1 }),
    directives: { old: entity("old", 3, 3, { origin: "mission_bootstrap", mission_revision: 1, run_id: "legacy" }) } }));
  assert.equal(store.get().snapshot.mission_delivery.run_id, "legacy");
});

test("a reconnected Mission snapshot outranks older events and accepts a newer delivery event", () => {
  const store = LoopStore.create();
  const delivered = { state: "delivered", mission_revision: 1, directive_id: "d1", run_id: "r1" };
  const authorized = { ...delivered, state: "authorized", run_id: null };
  const restored = snapshot(5, { loop: entity("l1", 2, 5, { goal_revision: 1, mission_delivery: delivered }),
    directives: { d1: entity("d1", 2, 2, { origin: "patrol", mission_delivery: authorized }) } });
  store.projectLive(restored);
  assert.deepEqual(store.get().snapshot.mission_delivery, delivered);
  const blocked = { ...authorized, state: "blocked", reason: "delivery_failed" };
  store.projectLive({ ...restored, last_sequence: 6, directives: {
    d1: entity("d1", 3, 6, { origin: "patrol", mission_delivery: blocked }),
  } });
  assert.deepEqual(store.get().snapshot.mission_delivery, blocked);
});

test("user interventions retain acceptance before a Run and ignore a late old phase", () => {
  let state = Schema.emptyProjection("l1");
  state = Reducer.reduce(state, event(1, { kind: "intervention.accepted", entity_type: "intervention", entity_id: "u1",
    correlation_id: "u1", payload: { intent_kind: "direct_message", origin: "user", target_context_id: "c1", state: "accepted", run_id: null } }));
  assert.equal(state.interventions.u1.state.state, "accepted");
  assert.equal(Object.keys(state.runs).length, 0);
  assert.equal(Selectors.selectCausality(state, "c1").length, 1);
  state = Reducer.reduce(state, event(2, { kind: "intervention.settled", entity_type: "intervention", entity_id: "u1",
    entity_revision: 6, correlation_id: "u1", payload: { state: "settled", run_id: "r1" } }));
  state = Reducer.reduce(state, event(3, { kind: "intervention.observed", entity_type: "intervention", entity_id: "u1",
    entity_revision: 3, correlation_id: "u1", payload: { state: "observed" } }));
  assert.equal(state.interventions.u1.state.state, "settled");
  assert.equal(state.interventions.u1.state.run_id, "r1");
});

test("accounting events update budget occupancy independently of frozen Loop control revision", () => {
  const legacy = LoopStore.create();
  let state = Schema.validateSnapshot(snapshot(1, { loop: entity("l1", 4, 1, { status: "paused", revision: 4, goal_revision: 1, usage: { model_calls: 0 } }) }));
  state = Reducer.reduce(state, event(2, { kind: "loop.accounting.updated", entity_type: "accounting", entity_id: "l1", entity_revision: 2, payload: { accounting: { consumption: { actual: { model_calls: 1 }, occupied: { model_calls: 2 } } }, usage: { model_calls: 1 } } }));
  legacy.projectLive(state, null);
  assert.equal(state.loop.revision, 4);
  assert.equal(legacy.get().snapshot.status, "paused");
  assert.equal(legacy.get().snapshot.usage.model_calls, 1);
  assert.equal(legacy.get().snapshot.accounting.consumption.occupied.model_calls, 2);
});

test("Run activity updates only from confirmed model and tool events", () => {
  let state = Reducer.reduce(snapshot(), event(1, { entity_revision: 1 }));
  state = Reducer.reduce(state, event(2, {
    kind: "context.model.completed",
    entity_type: "model_call",
    entity_id: "message-1",
    entity_revision: 1,
    payload: { run_id: "r1", context_id: "c1", summary: "模型已完成一次响应" },
    occurred_at: "2026-09-19T00:01:00Z",
  }));
  assert.equal(state.runs.r1.state.last_activity_at, "2026-09-19T00:01:00Z");
  assert.equal(state.runs.r1.state.last_activity_kind, "context.model.completed");
  state = Reducer.reduce(state, event(3, { kind: "transport.heartbeat", entity_type: "transport", entity_id: "connection", payload: {} }));
  assert.equal(state.runs.r1.state.last_activity_at, "2026-09-19T00:01:00Z");
});

test("entity reducers merge transition payloads without erasing snapshot card fields", () => {
  const initial = Schema.validateSnapshot(snapshot(1, { runs: { r1: entity("r1", 1, 1, { context_id: "c1", status: "running", origin: "patrol", input_tokens: 42 }) } }));
  const settled = Reducer.reduce(initial, event(2, { entity_type: "context_run", entity_revision: 2, payload: { status: "success", error: null } }));
  assert.equal(settled.runs.r1.state.status, "success");
  assert.equal(settled.runs.r1.state.context_id, "c1");
  assert.equal(settled.runs.r1.state.input_tokens, 42);
});

test("fact upserts normalize event payloads to the snapshot read model", () => {
  const initial = Schema.validateSnapshot(snapshot());
  const next = Reducer.reduce(initial, event(1, {
    kind: "fact.upserted", entity_type: "fact", entity_id: "f1", entity_revision: 2,
    payload: { fact_type: "test", state: "verified", source_context_id: "c1", source_run_id: "r1", presentation: { title: "Regression", summary: "18 passed", metrics: { passed: 18 } } },
  }));
  assert.deepEqual(Selectors.selectFacts(next), [{ fact_id: "f1", revision: 2, fact_type: "test", state: "verified", source_context_id: "c1", source_run_id: "r1", presentation: { title: "Regression", summary: "18 passed", metrics: { passed: 18 } }, kind: "test", status: "verified", context_id: "c1", run_id: "r1", title: "Regression", summary: "18 passed", metrics: { passed: 18 }, outcome_status: undefined, correlation_id: "corr-1" }]);
});

test("context expansion lifecycle is projected with stable blocker and causal state", () => {
  const initial = Schema.validateSnapshot(snapshot());
  const detected = Reducer.reduce(initial, event(1, {
    kind: "context_expansion.detected",
    entity_type: "context_expansion",
    entity_id: "x1",
    entity_revision: 1,
    payload: { opportunity_id: "o1", source_context_id: "c1", state: "detected", safe_summary: "发现独立测试方向" },
  }));
  const blocked = Reducer.reduce(detected, event(2, {
    kind: "context_expansion.blocked",
    entity_type: "context_expansion",
    entity_id: "x1",
    entity_revision: 2,
    payload: { state: "blocked", blocker_code: "workspace_isolation_unavailable", safe_summary: "无法分配隔离 Worktree" },
  }));

  assert.deepEqual(Selectors.selectExpansions(blocked), [{
    expansion_id: "x1",
    revision: 2,
    opportunity_id: "o1",
    source_context_id: "c1",
    state: "blocked",
    safe_summary: "无法分配隔离 Worktree",
    correlation_id: "corr-1",
    blocker_code: "workspace_isolation_unavailable",
  }]);
  assert.equal(blocked.activity_timeline[1].detail.blocker_code, "workspace_isolation_unavailable");
});

test("derived Context card appears only after the committed context event", () => {
  const initial = Schema.validateSnapshot(snapshot(1, {
    contexts: {
      root: entity("root", 1, 1, { title: "Root", role: "primary", status: "active", lane_id: "primary" }),
    },
  }));
  const proposed = Reducer.reduce(initial, event(2, {
    kind: "context_expansion.proposed",
    entity_type: "context_expansion",
    entity_id: "expansion-1",
    entity_revision: 1,
    payload: { state: "proposed", summary: "Patrol 提议测试分支", context_id: "derived" },
  }));
  const committed = Reducer.reduce(proposed, event(3, {
    kind: "context_expansion.committed",
    entity_type: "context_expansion",
    entity_id: "expansion-1",
    entity_revision: 2,
    payload: { state: "committed", summary: "Context 已原子提交", context_id: "derived" },
  }));
  const beforeManifest = {
    health: "publishing",
    nodes: Selectors.selectContextCards(committed).map(card => ({ context_id: card.id, ...card })),
    edges: [],
  };
  assert.doesNotMatch(PortfolioMap.render(beforeManifest, "root"), /data-context-id="derived"/);

  const visible = Reducer.reduce(committed, event(4, {
    kind: "context.created",
    entity_type: "context",
    entity_id: "derived",
    entity_revision: 1,
    payload: {
      title: "Independent verification",
      role: "side",
      status: "active",
      lane_id: "testing",
      current_revision_id: "derived-r1",
    },
  }));
  const afterManifest = {
    health: "dispatching",
    nodes: Selectors.selectContextCards(visible).map(card => ({ context_id: card.id, ...card })),
    edges: [{ source_context_id: "root", target_context_id: "derived", target_revision_id: "derived-r1" }],
  };
  assert.match(PortfolioMap.render(afterManifest, "root"), /data-context-id="derived"/);
});

test("all stable no-expansion and terminal reasons survive the projection boundary", () => {
  const reasonCodes = ["context_budget_exhausted", "duplicate_expansion", "workspace_isolation_unavailable", "stale_source", "compiler_failed"];
  let projection = Schema.validateSnapshot(snapshot());
  reasonCodes.forEach((blockerCode, index) => {
    projection = Reducer.reduce(projection, event(index + 1, {
      kind: "context_expansion.blocked",
      entity_type: "context_expansion",
      entity_id: `x${index}`,
      entity_revision: 1,
      payload: { state: "blocked", blocker_code: blockerCode, safe_summary: `blocked:${blockerCode}` },
    }));
  });
  projection = Reducer.reduce(projection, event(reasonCodes.length + 1, {
    kind: "context_expansion.superseded",
    entity_type: "context_expansion",
    entity_id: "xs",
    entity_revision: 1,
    payload: { state: "superseded", safe_summary: "equivalent Lane won" },
  }));

  const expansions = Selectors.selectExpansions(projection);
  assert.deepEqual(new Set(expansions.map(item => item.blocker_code).filter(Boolean)), new Set(reasonCodes));
  assert.equal(expansions.find(item => item.expansion_id === "xs").state, "superseded");
});

test("reducer pauses on a sequence gap and remains forward compatible with unknown entities", () => {
  const initial = Schema.validateSnapshot(snapshot());
  assert.throws(() => Reducer.reduce(initial, event(2)), error => error instanceof Reducer.SequenceGapError && error.expected === 1);
  const next = Reducer.reduce(initial, event(1, { kind: "future.widget.changed", entity_type: "widget", entity_id: "w1" }));
  assert.deepEqual(next.unknown_kinds, ["future.widget.changed"]);
  assert.equal(next.last_sequence, 1);
});

test("selectors expose scoped Patrol, Context, causality, fact and summary views", () => {
  const projection = Schema.validateSnapshot(snapshot(7, {
    patrol_session: entity("p1", 2, 7, { phase: "curating", safe_summary: "并行检查" }),
    round: entity("round-2", 2, 7, { number: 2 }),
    contexts: { c1: entity("c1", 1, 7, { title: "Testing" }), c2: entity("c2", 1, 7, { title: "Implementation" }) },
    runs: { r1: entity("r1", 1, 7, { context_id: "c1", status: "running", directive_id: "d1" }) },
    directives: { d1: entity("d1", 2, 7, { target_context_id: "c1", state: "delivered", correlation_id: "corr-1" }) },
    facts: { f1: entity("f1", 3, 7, { context_id: "c1", kind: "test", status: "verified" }) },
  }));
  assert.equal(Selectors.selectPatrol(projection).phase, "curating");
  assert.equal(Selectors.selectContextCards(projection)[0].active_run.status, "running");
  assert.equal(Selectors.selectFacts(projection, { contextId: "c1" })[0].fact_id, "f1");
  assert.equal(Selectors.selectGraphActivity(projection)[0].run.status, "running");
  assert.equal(Selectors.selectSummary(projection).active_contexts, 1);
});

test("snapshot replacement preserves valid UI state and drops an invalid selection", () => {
  const store = LiveStore.create();
  store.setUi({ selected_context_id: "c1", composer_draft: "不要丢", filters: { fact_kind: "test" }, scroll_anchors: { c1: 320 } });
  store.replaceSnapshot(snapshot(1, { contexts: { c1: entity("c1", 1, 1) } }));
  store.replaceSnapshot(snapshot(4, { contexts: { c1: entity("c1", 2, 4), c2: entity("c2", 1, 4) } }));
  assert.equal(store.get().ui.selected_context_id, "c1");
  assert.equal(store.get().ui.composer_draft, "不要丢");
  assert.equal(store.get().ui.scroll_anchors.c1, 320);
  store.replaceSnapshot(snapshot(5, { contexts: { c2: entity("c2", 1, 5) } }));
  assert.equal(store.get().ui.selected_context_id, "c2");
});

test("connection owns one stream and atomically resynchronizes a recoverable gap", async () => {
  const store = LiveStore.create();
  let snapshotReads = 0;
  let streams = 0;
  let release;
  const api = {
    liveSnapshot: async () => {
      snapshotReads += 1;
      return snapshot(snapshotReads === 1 ? 1 : 3, { contexts: { c1: entity("c1", snapshotReads, snapshotReads === 1 ? 1 : 3) } });
    },
    liveStream: async (_loopId, after, onFrame, signal) => {
      streams += 1;
      if (streams === 1) {
        assert.equal(after, 1);
        onFrame(event(3));
      }
      await new Promise(resolve => {
        release = resolve;
        signal.addEventListener("abort", resolve, { once: true });
      });
      return { resync: false };
    },
  };
  const connection = Connection.create({ api, store, wait: async () => {} });
  const first = connection.start("l1");
  const duplicate = connection.start("l1");
  assert.equal(first, duplicate);
  for (let index = 0; index < 20 && snapshotReads < 2; index += 1) await new Promise(resolve => setImmediate(resolve));
  assert.equal(snapshotReads, 2);
  assert.equal(store.get().projection.last_sequence, 3);
  assert.equal(store.get().connection.status, "live");
  connection.stop();
  release?.();
  await first;
});

test("legacy tool facts stay outside public facts while activity remains visible", () => {
  const initial = Schema.validateSnapshot(snapshot(1, { facts: {
    old: entity("old", 1, 1, { kind: "tool", status: "verified" }),
    tests: entity("tests", 1, 1, { kind: "test", status: "verified" }),
  } }));
  assert.deepEqual(Object.keys(initial.facts), ["tests"]);
  const replayed = Reducer.reduce(initial, event(2, { kind: "fact.upserted", entity_type: "fact", entity_id: "old", payload: { fact_type: "tool", state: "verified" } }));
  assert.equal(replayed.last_sequence, 2);
  assert.equal(replayed.facts.old, undefined);
  const active = Reducer.reduce(replayed, event(3, { kind: "context.tool.completed", entity_type: "run", payload: { status: "running", current_action: "pytest" } }));
  assert.equal(active.runs.r1.state.current_action, "pytest");
  assert.ok(Selectors.selectFacts(active).every((fact) => fact.kind !== "tool"));
});

test("initial HTTP 500 stays in connection state and recovers on the next snapshot", async () => {
  const store = LiveStore.create();
  let reads = 0;
  let streams = 0;
  let release;
  const api = {
    liveSnapshot: async () => {
      reads += 1;
      if (reads === 1) throw Object.assign(new Error("Internal Server Error"), { status: 500 });
      return snapshot(1);
    },
    liveStream: async (_loopId, _after, _onFrame, signal) => {
      streams += 1;
      await new Promise(resolve => {
        release = resolve;
        signal.addEventListener("abort", resolve, { once: true });
      });
    },
  };
  const connection = Connection.create({ api, store, wait: async () => {} });
  const started = connection.start("l1");
  for (let index = 0; index < 30 && streams < 1; index += 1) await new Promise(resolve => setImmediate(resolve));
  assert.equal(reads, 2);
  assert.equal(streams, 1);
  assert.equal(store.get().connection.status, "live");
  assert.equal(store.get().connection.error, null);
  connection.stop();
  release?.();
  await started;
});

test("repeated snapshot failures use bounded backoff without changing Loop business state", async () => {
  const store = LiveStore.create();
  const legacy = LoopStore.create();
  legacy.load({ loop_id: "l1", status: "running" });
  legacy.beginControl("pause");
  const waits = [];
  let connection;
  connection = Connection.create({
    api: { liveSnapshot: async () => { throw Object.assign(new Error("暂时不可用"), { status: 503 }); } },
    store,
    maxBackoff: 500,
    wait: async milliseconds => {
      waits.push(milliseconds);
      legacy.projectConnection(store.get().connection);
      if (waits.length === 3) connection.stop();
    },
  });
  await connection.start("l1");
  assert.deepEqual(waits, [250, 500, 500]);
  assert.equal(legacy.get().snapshot.status, "running");
  assert.equal(legacy.get().pendingControl, "pause");
});

test("nonretryable snapshot failures stop at one request", async () => {
  for (const status of [401, 403, 404, 422]) {
    const store = LiveStore.create();
    let reads = 0;
    const connection = Connection.create({
      api: { liveSnapshot: async () => { reads += 1; throw Object.assign(new Error(`HTTP ${status}`), { status }); } },
      store,
      wait: async () => assert.fail("terminal error must not retry"),
    });
    await connection.start("l1");
    assert.equal(reads, 1);
    assert.equal(store.get().connection.status, "unavailable");
  }
});

test("network and HTTP 429 snapshot failures retry and recover", async () => {
  for (const status of [null, 429]) {
    const store = LiveStore.create();
    let reads = 0;
    let observedSequence = null;
    let connection;
    connection = Connection.create({
      api: {
        liveSnapshot: async () => {
          reads += 1;
          if (reads === 1) throw Object.assign(new Error("temporary"), status === null ? {} : { status });
          return snapshot(1);
        },
        liveStream: async () => { observedSequence = store.get().projection.last_sequence; connection.stop(); },
      },
      store,
      wait: async () => {},
    });
    await connection.start("l1");
    assert.equal(reads, 2);
    assert.equal(observedSequence, 1);
  }
});

test("stopping an initial snapshot aborts its request and ignores a late response", async () => {
  const store = LiveStore.create();
  let release;
  let requestSignal;
  const connection = Connection.create({
    api: {
      liveSnapshot: (_loopId, signal) => new Promise(resolve => { requestSignal = signal; release = resolve; }),
      liveStream: async () => assert.fail("stopped snapshot must not open SSE"),
    },
    store,
  });
  const started = connection.start("l1");
  connection.stop();
  assert.equal(requestSignal.aborted, true);
  release(snapshot(1));
  await started;
  assert.equal(store.get().projection, null);
  assert.equal(store.get().connection.status, "idle");
});

test("switching Loops rejects the previous snapshot and opens only the new stream", async () => {
  const store = LiveStore.create();
  let releaseOld;
  const streams = [];
  const connection = Connection.create({
    api: {
      liveSnapshot: loopId => loopId === "l1"
        ? new Promise(resolve => { releaseOld = resolve; })
        : Promise.resolve({ ...snapshot(2), loop_id: "l2" }),
      liveStream: async (loopId, _after, _onFrame, signal) => {
        streams.push(loopId);
        await new Promise(resolve => signal.addEventListener("abort", resolve, { once: true }));
      },
    },
    store,
  });
  const old = connection.start("l1");
  const current = connection.start("l2");
  releaseOld(snapshot(1));
  for (let index = 0; index < 20 && !streams.length; index += 1) await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(streams, ["l2"]);
  assert.equal(store.get().projection.loop_id, "l2");
  connection.stop();
  await Promise.all([old, current]);
});

test("switching Loops clears the old projection before the new snapshot arrives", async () => {
  const store = LiveStore.create();
  let releaseNew;
  let releaseStream;
  const connection = Connection.create({
    api: {
      liveSnapshot: loopId => loopId === "l1"
        ? Promise.resolve(snapshot(1))
        : new Promise(resolve => { releaseNew = resolve; }),
      liveStream: async (_loopId, _after, _onFrame, signal) => new Promise(resolve => {
        releaseStream = resolve;
        signal.addEventListener("abort", resolve, { once: true });
      }),
    },
    store,
  });
  const old = connection.start("l1");
  for (let index = 0; index < 20 && !releaseStream; index += 1) await new Promise(resolve => setImmediate(resolve));
  assert.equal(store.get().projection.loop_id, "l1");
  const current = connection.start("l2");
  assert.equal(store.get().projection, null);
  releaseNew({ ...snapshot(2), loop_id: "l2" });
  for (let index = 0; index < 20 && store.get().projection?.loop_id !== "l2"; index += 1) await new Promise(resolve => setImmediate(resolve));
  assert.equal(store.get().projection.loop_id, "l2");
  connection.stop();
  releaseStream?.();
  await Promise.all([old, current]);
});

test("switching during retry wait cancels the old Loop retry", async () => {
  const store = LiveStore.create();
  const reads = [];
  let waiting;
  let releaseStream;
  const connection = Connection.create({
    api: {
      liveSnapshot: async loopId => {
        reads.push(loopId);
        if (loopId === "l1") throw Object.assign(new Error("temporary"), { status: 500 });
        return { ...snapshot(2), loop_id: "l2" };
      },
      liveStream: async (_loopId, _after, _onFrame, signal) => new Promise(resolve => {
        releaseStream = resolve;
        signal.addEventListener("abort", resolve, { once: true });
      }),
    },
    store,
    wait: (_milliseconds, signal) => new Promise(resolve => {
      waiting = true;
      signal.addEventListener("abort", resolve, { once: true });
    }),
  });
  const old = connection.start("l1");
  for (let index = 0; index < 20 && !waiting; index += 1) await new Promise(resolve => setImmediate(resolve));
  const current = connection.start("l2");
  for (let index = 0; index < 20 && !releaseStream; index += 1) await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(reads, ["l1", "l2"]);
  assert.equal(store.get().projection.loop_id, "l2");
  connection.stop();
  releaseStream?.();
  await Promise.all([old, current]);
});

test("clearing Live state leaves the committed Loop snapshot intact", () => {
  const legacy = LoopStore.create();
  legacy.load({ loop_id: "l1", status: "running" });
  legacy.projectLive(snapshot(1), { status: "live" });
  legacy.clearLive({ status: "syncing" });
  assert.equal(legacy.get().snapshot.status, "running");
  assert.equal(legacy.get().live, null);
  assert.equal(legacy.get().connection.status, "syncing");
});

test("gap resynchronization retries a transient snapshot error and resumes once", async () => {
  const store = LiveStore.create();
  let reads = 0;
  let streams = 0;
  let observedSequence = null;
  const starts = [];
  let connection;
  connection = Connection.create({
    api: {
      liveSnapshot: async () => {
        reads += 1;
        if (reads === 2) throw Object.assign(new Error("暂时失败"), { status: 500 });
        return snapshot(reads === 1 ? 1 : 3);
      },
      liveStream: async (_id, after, onFrame) => {
        streams += 1;
        starts.push(after);
        if (streams === 1) onFrame(event(3));
        observedSequence = store.get().projection.last_sequence;
        connection.stop();
      },
    },
    store,
    wait: async () => {},
  });
  await connection.start("l1");
  assert.equal(reads, 3);
  assert.deepEqual(starts, [1, 3]);
  assert.equal(observedSequence, 3);
});

test("Live Snapshot forwards the connection abort signal through the HTTP API", async () => {
  let signalReceived;
  const api = LoopApi.create({ apiBase: "http://localhost", session: "session" }, async (_url, options) => {
    signalReceived = options.signal;
    return Response.json(snapshot(1));
  });
  const controller = new AbortController();
  await api.liveSnapshot("l1", controller.signal);
  assert.equal(signalReceived, controller.signal);
});

test("app delegates Loop domain reduction and stream recovery to dedicated modules", () => {
  const source = fs.readFileSync(require.resolve("./app.js"), "utf8");
  assert.doesNotMatch(source, /function startLoopStream|function scheduleLoopRefresh|function scheduleLoopPoll/);
  assert.doesNotMatch(source, /loopApi\.events\(|loopStore\.apply\(/);
  assert.match(source, /loopConnection\.start\(loopId\)\.catch/);
});

test("long-running selectors bound graph effects and fact rows while durable state remains queryable", () => {
  const directives = {};
  const facts = {};
  for (let index = 1; index <= 600; index += 1) {
    directives[`d${index}`] = entity(`d${index}`, 1, index, { target_context_id: "c1", state: "delivered" });
    facts[`f${index}`] = entity(`f${index}`, 1, index, { context_id: "c1", kind: "test", status: "verified" });
  }
  const projection = Schema.validateSnapshot(snapshot(600, { directives, facts }));
  assert.equal(Object.keys(projection.facts).length, 600);
  assert.equal(Selectors.selectGraphActivity(projection).length, Selectors.MAX_VISIBLE_GRAPH_ACTIVITY);
  assert.equal(Selectors.selectFacts(projection).length, Selectors.MAX_VISIBLE_FACTS);
});

test("bursty canonical reduction stays deterministic within the existing interaction budget", () => {
  let projection = Schema.validateSnapshot(snapshot());
  const started = performance.now();
  for (let sequence = 1; sequence <= 2000; sequence += 1) {
    projection = Reducer.reduce(projection, event(sequence, { entity_revision: sequence, payload: { context_id: "c1", status: sequence === 2000 ? "success" : "running" } }));
  }
  const elapsed = performance.now() - started;
  assert.equal(projection.last_sequence, 2000);
  assert.equal(projection.runs.r1.state.status, "success");
  assert.equal(projection.activity_timeline.length, 200);
  assert.ok(elapsed < 500, `2000-event reduction took ${elapsed.toFixed(1)}ms`);
});

test("live workspace exposes semantic labels and a reduced-motion equivalent", () => {
  const styles = fs.readFileSync(require.resolve("./styles/loop-console.css"), "utf8");
  const view = fs.readFileSync(require.resolve("./loop-view.js"), "utf8");
  assert.match(styles, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(view, /aria-label="Loop 实时活动"/);
  assert.match(view, /aria-live="polite"/);
});
