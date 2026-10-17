/* 真实预算读面选择器、预留量、终态、等待和草稿冻结回归；不启动模型。 */
const test = require("node:test"), assert = require("node:assert/strict");
const Budget = require("./workspace-patrol-budget.js");
const Inputs = require("./workspace-patrol-inputs.js");
function projection() {
  return { loop_id: "l", loop: { state: { status: "running", created_at: "2026-10-17T00:00:00Z", authority_revision: 2,
    grant: { status: "active", budgets: { max_duration_seconds: 1800, max_model_calls: 100, max_concurrent_runs: 2 } },
    usage: { model_calls: 70 } } }, accounting: { state: { usage: { model_calls: 90 }, accounting: {
      consumption: { actual: { model_calls: 80 }, occupied: { model_calls: 90 }, unreported_reservations: { model_calls: 10 } } } } },
    runs: { a: { state: { status: "running" } }, b: { state: { status: "pending" } }, c: { state: { status: "completed" } } } };
}
test("remaining budget uses occupied consumption while reporting actual and reservation separately", () => {
  const model = Budget.summary(projection(), Date.parse("2026-10-17T00:10:00Z"));
  const calls = model.rows.find(row => row.key === "model_calls");
  assert.equal(calls.actual, 80); assert.equal(calls.reserved, 10); assert.equal(calls.remaining, 10);
  assert.equal(model.rows.find(row => row.key === "duration_seconds").remaining, 1200);
  assert.equal(model.running, 1); assert.equal(model.queued, 1); assert.equal(model.concurrency, 2);
  assert.equal(model.near, true); assert.equal(model.canIncrease, true);
});
test("unlimited and unsynchronized consumption remain distinct from zero", () => {
  const p = projection(); delete p.loop.state.created_at;
  const model = Budget.summary(p);
  assert.equal(model.rows.find(row => row.key === "duration_seconds").occupied, undefined);
  assert.equal(model.rows.find(row => row.key === "input_tokens").limit, undefined);
  assert.equal(model.rows.find(row => row.key === "input_tokens").remaining, null);
});
test("only a budget wait allows adding budget from waiting state; terminal never does", () => {
  const p = projection(); p.loop.state.status = "waiting_user";
  p.wait_requests = { w: { state: { status: "open", kind: "permission" } } };
  assert.equal(Budget.summary(p).canIncrease, false);
  p.wait_requests.w.state.kind = "budget_action";
  assert.equal(Budget.summary(p).canIncrease, true);
  p.loop.state.status = "completed";
  assert.equal(Budget.summary(p).canIncrease, false);
});
test("first preset freezes with submission and retry, without leaking into the next input", () => {
  const store = Inputs.create(() => "a");
  store.edit({ content: "任务", budget_preset: "long" });
  const row = store.submit();
  store.edit({ budget_preset: "normal" });
  store.failed("a", new Error("offline"));
  assert.equal(store.retry("a").request.budget_preset, "long");
  assert.equal(row.request.budget_preset, "long");
  assert.equal(store.get().draft.budget_preset, "normal");
});
test("ended duration freezes at completion and cannot grow when reading history", () => {
  const p = projection(); p.loop.state.status = "stopped"; p.loop.state.completed_at = "2026-10-17T00:20:00Z";
  const model = Budget.summary(p, Date.parse("2026-10-18T00:00:00Z"));
  assert.equal(model.rows.find(row => row.key === "duration_seconds").occupied, 1200);
});
test("preset selected before binding follows the draft even without attachments or text", () => {
  const intake = require("./workspace-patrol-intake.js").create({ runtime: {}, api: {}, storage: null });
  intake.store(null).edit({ budget_preset: "long" });
  intake.claimUnbound("workspace-a");
  assert.equal(intake.store("workspace-a").get().draft.budget_preset, "long");
  intake.claimUnbound("workspace-b");
  assert.equal(intake.store("workspace-b").get().draft.budget_preset, undefined);
});
