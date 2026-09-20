/*
 * 本文件验证前端 Live Loop schema、纯 reducer、序列防护、单连接恢复、选择器与界面状态保留。
 * 输入为有效/畸形 snapshot、重复/陈旧/缺口事件和模拟 Live API；输出为确定性 projection、原子重同步及无重复连接断言。
 * 具体工作流为使用 Node test 直接加载 UMD 模块并驱动 Store/Connection；示例：`node --test desktop/loop-live-projection.test.js`。
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

test("app delegates Loop domain reduction and stream recovery to dedicated modules", () => {
  const source = fs.readFileSync(require.resolve("./app.js"), "utf8");
  assert.doesNotMatch(source, /function startLoopStream|function scheduleLoopRefresh|function scheduleLoopPoll/);
  assert.doesNotMatch(source, /loopApi\.events\(|loopStore\.apply\(/);
  assert.match(source, /loopConnection\?\.start/);
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
  assert.match(view, /aria-label="Patrol 实时活动"/);
  assert.match(view, /aria-live="polite"/);
});
