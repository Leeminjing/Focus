/* 逐输入反馈必须保留精确来源，并与传输状态、取消清理及其他输入隔离。 */
const test = require("node:test");
const assert = require("node:assert/strict");
const feedback = require("./workspace-patrol-feedback.js");
const view = require("./workspace-patrol-view.js");
const { create } = require("./workspace-patrol-inputs.js");

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

const row = (disposition = "decision") => ({ submission_id: "one", input_type: "information", status: "accepted", processing_state: "addressed", content: "先别改 UI，把导入失败解决。", processing_result: {
  disposition, explanation: "先定位导入失败。下一步验证错误原因。", mission_revision: 3,
  decision_id: "decision-1", round_id: "round-2", observation_id: "observation-3", observation_generation: 4,
  rationale: "导入失败阻塞现有工作", evidence: [{ fact_id: "fact-1", revision: 2 }],
  related_cancellations: [{ target_kind: "run", target_id: "ui-run", reason: "先解决导入失败", state: "cancel_requested" }],
} });

test("each disposition shows its own explanation and expandable exact sources", () => {
  for (const [disposition, label] of Object.entries({ discussion: "已用于讨论", decision: "已纳入决策", reference: "已作为参考", deferred: "已延期" })) {
    const html = view.history({ rows: [row(disposition)] });
    assert.ok(html.includes(`处理结果 · ${label}`));
    assert.match(html, /先定位导入失败/);
    assert.match(html, /<details[^>]*data-patrol-input-result/);
    assert.match(html, /Mission 版本.*<dd>3<\/dd>/);
    assert.match(html, /decision-1.*round-2.*observation-3/s);
    assert.match(html, /fact-1/);
    assert.match(html, /已请求停止 · 等待清理/);
    assert.doesNotMatch(html, />已停止</);
  }
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
  assert.doesNotMatch(feedback.inputResult(input), /<img|<script|<svg|<iframe/);
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
  store.history(await feedback.refreshInputs(api, "workspace", store.get()));
  assert.equal(store.get().rows.find(item => item.intent_id === "older").processing_result.disposition, "decision");
  assert.equal(store.get().draft.content, "继续编辑");
  assert.equal(store.get().cursor, null);
  assert.deepEqual(calls[1].options.intentIds, ["older"]);
});

test("deferred decisions and execution cleanup both invalidate feedback", () => {
  const projection = { interventions: { input: "observed" }, round: { revision: 1 }, runs: { run: { state: { status: "interrupted", settled_at: null } } } };
  const before = feedback.inputSignature(projection);
  projection.round.revision++;
  assert.notEqual(feedback.inputSignature(projection), before);
  const requested = feedback.inputSignature(projection);
  projection.runs.run.state.settled_at = "2026-10-17T00:00:00Z";
  assert.notEqual(feedback.inputSignature(projection), requested);
});
