/* 逐输入反馈必须保留精确来源，并与传输状态、取消清理及其他输入隔离。 */
const test = require("node:test");
const assert = require("node:assert/strict");
const feedback = require("./workspace-patrol-feedback.js");
const view = require("./workspace-patrol-view.js");
const { create } = require("./workspace-patrol-inputs.js");
const { createElement } = require("../../../../../tests/helpers/test-dom.cjs");
const i18n = require("../../../shared/i18n/i18n.js");

function fragment(html) {
  const node = createElement("div");
  node.innerHTML = html;
  return node;
}

test("stopped and failed work separates saved inputs from reactivation", () => {
  for (const status of ["stopped", "failed"]) {
    const projection = { loop_id: "loop", loop: { state: { status } } };
    const model = { rows: [{ status: "accepted", processing_state: "pending" }] };
    assert.deepEqual(feedback.recovery(projection, model), {
      text: "这条输入已保存，重新启用后处理。", label: "重新启用并处理",
    });
    assert.equal(feedback.recovery(projection, { rows: [] }).label, "重新启用");
    for (const row of [{ status: "failed" }, { status: "sending" }, { status: "accepted", processing_state: "addressed" }, { status: "accepted", processing_result: {} }]) {
      assert.equal(feedback.recovery(projection, { rows: [row] }).label, "重新启用");
    }
  }
  for (const status of ["running", "paused", "waiting_user", "completed"]) {
    assert.equal(feedback.recovery({ loop: { state: { status } } }, { rows: [] }), null);
  }
  assert.equal(feedback.recovery(null, { rows: [] }), null);
});

test("verified workspace delivery is explicit while Patrol remains available", () => {
  const loop = { interaction_mode: "workspace_patrol", status: "waiting_user", health: "idle", waiting_reason: "awaiting_input",
    current_round_id: "round-delivered", goal_revision: 2,
    final_result: { round_id: "round-delivered", goal_revision: 2, summary: "修复导入并生成报告", verification: {
      verification_id: "verified", conclusion: "satisfied", criteria: [
        { check_id: "import", status: "satisfied", explanation: "导入测试通过" },
        { check_id: "optional", status: "unknown", explanation: "可选平台尚未验证" },
      ], unresolved: ["optional"] }, final_path: [{ context_id: "result", revision_id: "revision-result", title: "导入报告" }] } };
  assert.match(feedback.status({ loop: { state: loop } }, { status: "live" }, { rows: [] }).text, /本次工作已交付/);
  const html = view.delivery({ loop: { state: loop } }, { path: "C:/workspace" });
  for (const text of ["本次工作交付", "修复导入并生成报告", "验收依据 · 已通过 1 项", "仍未解决 · 1 项", "导入测试通过", "可选平台尚未验证", "C:/workspace"]) assert.ok(html.includes(text), text);
  assert.ok(html.indexOf('aria-label="查看本次交付"') < html.indexOf('class="patrol-delivery-evidence"'));
  assert.ok(html.indexOf('class="patrol-delivery-unresolved"') < html.indexOf('class="patrol-delivery-evidence"'));
  assert.doesNotMatch(html, /继续提出调整|结果与修改位置|查看结果 ·/);
  assert.match(html, /data-patrol-delivery-context="result"/);
  assert.match(html, /data-revision-id="revision-result"/);
  loop.current_round_id = "discussion";
  assert.equal(feedback.status({ loop: { state: loop } }, { status: "live" }, { rows: [] }).state, "idle");
  assert.match(view.delivery({ loop: { state: loop } }), /最近一次/);
  for (const final_result of [{}, { verification: { verification_id: "bad", conclusion: "unknown" } }]) {
    loop.final_result = final_result;
    assert.equal(feedback.status({ loop: { state: loop } }, { status: "live" }, { rows: [] }).state, "idle");
    assert.equal(view.delivery({ loop: { state: loop } }), "");
  }
});

const row = (disposition = "decision") => ({ submission_id: "one", input_type: "information", status: "accepted", processing_state: "addressed", content: "先别改 UI，把导入失败解决。", processing_result: {
  disposition, explanation: "先定位导入失败。下一步验证错误原因。", mission_revision: 3,
  decision_id: "decision-1", round_id: "round-2", observation_id: "observation-3", observation_generation: 4,
  rationale: "导入失败阻塞现有工作", evidence: [{ fact_id: "fact-1", revision: 2 }],
  related_cancellations: [{ target_kind: "run", target_id: "ui-run", reason: "先解决导入失败", state: "cancel_requested" }],
} });

test("each disposition shows its own explanation and expandable exact sources", () => {
  for (const [disposition, label] of Object.entries({ discussion: "已用于讨论", decision: "已纳入决策", reference: "已作为参考", deferred: "已延期" })) {
    const input = row(disposition);
    const html = view.history({ rows: [input] });
    assert.doesNotMatch(html, /处理结果 ·|普通信息/);
    assert.ok(html.indexOf('先别改 UI') < html.indexOf(label));
    const result = fragment(html).querySelector("details[data-patrol-input-result]");
    assert.equal(result.dataset.patrolDisposition, disposition);
    assert.ok(result.querySelector("summary strong").textContent.includes(label));
    assert.equal(result.querySelector("summary span").textContent, input.processing_result.explanation);
    for (const [field, value] of Object.entries({ mission_revision: "3", decision_id: "decision-1", round_id: "round-2", observation_id: "observation-3" })) {
      assert.equal(result.querySelector(`[data-patrol-source="${field}"]`).textContent, value, field);
    }
    assert.equal(result.querySelector('[data-patrol-source="observation_generation"]').textContent, i18n.t("patrol.feedback.version", { version: 4 }));
    assert.deepEqual(JSON.parse(result.querySelector("pre").textContent.replaceAll("&quot;", '"')), { fact_id: "fact-1", revision: 2 });
    const cancellation = result.querySelector('[data-patrol-cancellation-state="cancel_requested"]');
    assert.equal(cancellation.dataset.patrolTargetKind, "run");
    assert.equal(cancellation.dataset.patrolTargetId, "ui-run");
    assert.match(cancellation.textContent, /等待清理/);
    assert.equal(result.querySelector('[data-patrol-cancellation-state="stopped"]'), null);
  }
});

test("input references stay with the original text and non-default input types remain explicit", () => {
  const input = { ...row(), file_refs: [{ path: 'input.txt', revision: 3, fact_id: 'file', result_key: 'fixed-file' }] };
  const html = view.history({ rows: [input] });
  assert.ok(html.indexOf('先别改 UI') < html.indexOf('input.txt'));
  assert.ok(html.indexOf('input.txt') < html.indexOf('已纳入决策'));
  assert.match(html, /data-submission-id="one"/);
  for (const [input_type, label] of Object.entries({ outcome: "最终结果", boundary: "执行边界", completion_check: "完成检查" })) {
    assert.match(view.history({ rows: [{ ...row(), input_type }] }), new RegExp(label));
  }
});

test("cancellation states remain distinct after removing the processing prefix", () => {
  for (const [state, label] of Object.entries({ cancel_requested: "已请求停止 · 等待清理", stopped: "已停止", already_terminal: "请求时已结束", settled: "已结束", unknown: "停止状态待核实" })) {
    const input = row(); input.processing_result.related_cancellations[0].state = state;
    const html = feedback.inputResult(input);
    assert.ok(html.includes(label));
    if (state === 'cancel_requested') assert.doesNotMatch(html, />已停止</);
    assert.doesNotMatch(html, /处理结果 ·/);
  }
});

test("unresolved checks remain visible even without a criterion row", () => {
  const loop = { interaction_mode: 'workspace_patrol', current_round_id: 'done', goal_revision: 2,
    final_result: { round_id: 'done', goal_revision: 2, completion_checks: [{ check_id: 'missing', claim: '外部平台验证' }],
      verification: { verification_id: 'v', conclusion: 'satisfied', criteria: [], unresolved: ['missing'] } } };
  const html = feedback.delivery({ loop: { state: loop } });
  assert.match(html, /仍未解决 · 1 项.*外部平台验证/s);
  assert.ok(html.indexOf('外部平台验证') < html.indexOf('<details'));
  assert.match(html, />已验收</);
  loop.goal_revision = 3;
  assert.match(feedback.delivery({ loop: { state: loop } }), /最近一次已验收交付/);
  loop.final_result.verification.unresolved = [];
  assert.doesNotMatch(feedback.delivery({ loop: { state: loop } }), /patrol-delivery-unresolved/);
});

test("delivery content is escaped and legacy results stay visible without claiming a new completion", () => {
  const loop = { interaction_mode: "workspace_patrol", status: "waiting_user", waiting_reason: "awaiting_input", final_result: {
    summary: '<img src=x onerror="bad()">', verification: { verification_id: "legacy", conclusion: "satisfied", criteria: [
      { check_id: "<script>", status: "satisfied", explanation: "<svg onload=bad()>" },
    ], unresolved: [] }, final_path: [{ context_id: '" onclick="bad()', revision_id: '<script>', title: '<iframe>' }] } };
  const html = view.delivery({ loop: { state: loop } }, { path: '<script>' });
  assert.doesNotMatch(html, /<img|<script|<svg|<iframe|" onclick=/);
  assert.match(html, /最近一次/);
  assert.equal(feedback.status({ loop: { state: loop } }, { status: "live" }, { rows: [] }).state, "idle");
});

test("pending and legacy states never invent an effect or claim work completion", () => {
  for (const [state, label] of Object.entries({ pending: "等待 Patrol 处理", observed: "正在处理", addressed: "已处理 · 暂无处理说明", superseded: "已被替代" })) {
    const html = feedback.inputResult({ status: "accepted", processing_state: state });
    assert.ok(html.includes(label));
    assert.doesNotMatch(html, /已纳入决策|任务已完成/);
  }
  assert.equal(feedback.inputResult({ ...row(), status: "failed" }), "");
  assert.equal(feedback.inputResult({ ...row(), status: "sending" }), "");
});

test("all explanation, evidence and identity strings are escaped", () => {
  const input = row();
  input.processing_result.explanation = '<img src=x onerror="injected()">';
  input.processing_result.rationale = "<script>bad()</script>";
  input.processing_result.evidence = [{ fact_id: "</pre><svg onload=bad()>" }];
  input.processing_result.related_cancellations[0].target_id = '<iframe src="bad">';
  input.processing_result.disposition = '\" onclick="bad()';
  input.processing_result.related_cancellations[0].state = '\" onclick="bad()';
  input.processing_result.related_cancellations[0].target_kind = '\" onclick="bad()';
  input.processing_result.changes = [{ operation: '<script>', section: '<svg>', quote: '<img src=x onerror="bad()">' }];
  const html = feedback.inputResult(input);
  assert.doesNotMatch(html, /<img|<script|<svg|<iframe|"\s+onclick=/);
  assert.equal(fragment(html).querySelector("[onclick]"), null);
});

test("stop receipts preserve each execution state and identity independently of display order", () => {
  const input = row();
  const states = ["cancel_requested", "stopped", "already_terminal", "settled", "unknown", "future_state"];
  input.processing_result.related_cancellations = states.map((state, i) => ({ state, target_kind: "worker", target_id: `worker-${i}`, reason: `reason-${i}` })).reverse();
  const result = fragment(feedback.inputResult(input));
  for (const [i, state] of states.entries()) {
    const receipt = result.querySelector(`[data-patrol-cancellation-state="${state}"]`);
    assert.equal(receipt.dataset.patrolTargetId, `worker-${i}`);
    assert.equal(receipt.dataset.patrolTargetKind, "worker");
    assert.equal(receipt.querySelector("p").textContent, `reason-${i}`);
    if (state === "future_state") assert.match(receipt.querySelector("strong").textContent, /待核实/);
    else if (state === "cancel_requested") assert.match(receipt.querySelector("strong").textContent, /等待清理/);
  }
});

test("unknown enum values including object property names keep safe domain fallbacks", () => {
  for (const unknown of ["future_state", "constructor", "toString", "__proto__"]) {
    const input = row(unknown);
    input.processing_result.related_cancellations = [{ state: unknown, target_kind: unknown, target_id: "target", reason: "reason" }];
    input.processing_result.changes = [{ operation: unknown, section: unknown, quote: "quote" }];
    const result = fragment(feedback.inputResult(input));
    assert.equal(result.querySelector("summary strong").textContent, i18n.t("patrol.feedback.processed"));
    assert.equal(result.querySelector("li strong").textContent, i18n.t("patrol.feedback.cancel_unknown"));
    assert.ok(result.textContent.includes(unknown), "unknown operation/section/target remains visible");
    const pending = fragment(feedback.inputResult({ status: "accepted", processing_state: unknown }));
    assert.equal(pending.querySelector("p").textContent, i18n.t("patrol.feedback.pending"));
  }
});

test("history refresh updates old loaded inputs without changing the paging cursor or draft", async () => {
  const store = create();
  store.history({ items: [{ ...row("deferred"), intent_id: "older" }], next_before: null }, false, true);
  store.edit({ content: "继续编辑" });
  const calls = [];
  const api = { workspaceInputs: async (workspace, before, options) => {
    calls.push({ workspace, before, options });
    return options ? { items: [{ ...row(), intent_id: "older" }] } : { items: [{ ...row("reference"), submission_id: "two", intent_id: "latest" }], next_before: "latest" };
  } };
  store.history(await feedback.refreshInputs(api, "workspace", store.get(), { latest: true, intentIds: ["older"] }));
  assert.equal(store.get().rows.find(item => item.intent_id === "older").processing_result.disposition, "decision");
  assert.equal(store.get().draft.content, "继续编辑");
  assert.equal(store.get().cursor, null);
  assert.deepEqual(calls[1].options.intentIds, ["older"]);
});

test("unrelated execution changes do not refresh history; settlement targets only related inputs", () => {
  const refresh = feedback.createInputRefresh();
  const model = { rows: [{ ...row(), intent_id: "older" }] };
  const before = { loop_id: "loop", interventions: {}, runs: { "ui-run": { state: { status: "interrupted", settlement_status: "pending" } } } };
  assert.equal(refresh.observe(before, model), false);
  const unrelated = { ...before, loop: { state: { status: "paused" } }, round: { entity_id: "r", state: { status: "running" } },
    runs: { ...before.runs, unrelated: { state: { status: "running" } } } };
  assert.equal(refresh.observe(unrelated, model), false);
  const settled = { ...unrelated, runs: { ...unrelated.runs, "ui-run": { state: { status: "interrupted", settlement_status: "settled" } } } };
  assert.equal(refresh.observe(settled, model), true);
  assert.deepEqual(refresh.take(), { latest: false, intentIds: ["older"] });
});

test("committed rounds refresh deferred inputs including an input inherited from an earlier loop", () => {
  const refresh = feedback.createInputRefresh();
  const model = { rows: [{ ...row("deferred"), intent_id: "deferred", loop_id: "previous-loop", round_id: "previous-round" },
    { ...row(), intent_id: "done", processing_result: { disposition: "decision" } }] };
  const before = { loop_id: "loop", interventions: {}, round: { entity_id: "r", state: { status: "running" } } };
  refresh.observe(before, model);
  assert.equal(refresh.observe({ ...before, round: { entity_id: "r", state: { authority_committed_at: "now", decision_id: "d" } } }, model), true);
  assert.deepEqual(refresh.take(), { latest: true, intentIds: ["deferred"] });
});

test("directive transitions and their launched Run settlement refresh only the referenced input", () => {
  const refresh = feedback.createInputRefresh();
  const model = { rows: [{ intent_id: "i", processing_state: "addressed", processing_result: {
    related_cancellations: [{ target_kind: "directive", target_id: "d", state: "cancel_requested" }] } }] };
  const before = { loop_id: "loop", directives: { d: { state: { state: "active", run_id: "run" } } },
    runs: { run: { state: { status: "interrupted", settlement_status: "pending" } } } };
  refresh.observe(before, model);
  const cancelled = { ...before, directives: { d: { state: { state: "cancelled", run_id: "run" } } } };
  assert.equal(refresh.observe(cancelled, model), true);
  assert.deepEqual(refresh.take().intentIds, ["i"]);
  assert.equal(refresh.observe({ ...cancelled, runs: { run: { state: { status: "interrupted", settlement_status: "settled" } } } }, model), true);
  assert.deepEqual(refresh.take(), { latest: false, intentIds: ["i"] });
});

test("worker cleanup follows its own Round barrier when no worker collection is published", () => {
  const refresh = feedback.createInputRefresh();
  const model = { rows: [{ intent_id: "i", processing_state: "addressed", processing_result: {
    related_cancellations: [{ target_kind: "worker", target_id: "w", round_id: "r", state: "cancel_requested" }] } }] };
  const before = { loop_id: "loop", rounds: { r: { state: { status: "running", barrier: {} } } } };
  refresh.observe(before, model);
  assert.equal(refresh.observe({ ...before, rounds: { r: { state: { status: "settled", barrier: { worker: "settled" } } } } }, model), true);
  assert.deepEqual(refresh.take(), { latest: false, intentIds: ["i"] });
});

test("refresh plans merge changed input IDs and restore failures without rescanning loaded history", async () => {
  const refresh = feedback.createInputRefresh();
  const model = { rows: Array.from({ length: 1000 }, (_, i) => ({ intent_id: `i-${i}`, processing_state: "addressed" })), cursor: "cursor" };
  const before = { loop_id: "loop", interventions: {} };
  refresh.observe(before, model);
  const one = { ...before, interventions: { "i-1": { revision: 2 } } };
  refresh.observe(one, model);
  const plan = refresh.take();
  refresh.observe({ ...one, interventions: { ...one.interventions, "i-2": { revision: 2 } } }, model);
  refresh.restore(plan);
  const merged = refresh.take();
  assert.deepEqual(merged, { latest: true, intentIds: ["i-2", "i-1"] });
  const calls = [];
  const result = await feedback.refreshInputs({ workspaceInputs: async (_workspace, _before, options) => {
    calls.push(options);
    return options ? { items: [{ intent_id: "i-1" }] } : { items: [{ intent_id: "i-2" }], next_before: "next" };
  } }, "workspace", model, merged);
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1].intentIds, ["i-1"], "exclude IDs already returned in the latest page");
  assert.equal(result.items.length, 2);
});

test("targeted reads retain the cursor and batch only affected IDs in groups of at most 100", async () => {
  const calls = [], intentIds = Array.from({ length: 205 }, (_, i) => `i-${i}`);
  const result = await feedback.refreshInputs({ workspaceInputs: async (_workspace, _before, options) => {
    calls.push(options.intentIds);
    return { items: options.intentIds.map(intent_id => ({ intent_id })) };
  } }, "workspace", { cursor: "cursor" }, { latest: false, intentIds });
  assert.deepEqual(calls.map(ids => ids.length), [100, 100, 5]);
  assert.equal(result.next_before, "cursor");
  assert.equal(result.items.length, 205);
});
