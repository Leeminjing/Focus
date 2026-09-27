/*
 * 本文件对外提供 Context Expansion 预算表单与状态呈现的自动化断言。
 * 输入为有效 API 策略快照、表单字段与阻断 session；输出为默认/显式值一致、数字 payload、真实阶段及逐项用量可见的测试结果。
 * 具体工作流为用公开 UI 模块渲染、读取并比对同一策略，不依赖真实 Vault 或模型。
 * 示例：`node --test desktop/loop-expansion-budget.test.cjs`。
 */
const assert = require("node:assert/strict");
const test = require("node:test");
const Budget = require("./loop-expansion-budget.js");
const LoopView = require("./loop-view.js");

test("start and edit UI expose every effective expansion resource", () => {
  const start = LoopView.render(null, { title: "Large task" });
  for (const key of Object.keys(Budget.DEFAULTS).filter(key => key !== "version")) {
    assert.match(start, new RegExp(`name="expansion_${key}"`));
  }
  const values = new Map(Object.entries(Budget.DEFAULTS).filter(([key]) => key !== "version").map(([key, value]) => [`expansion_${key}`, String(value)]));
  assert.deepEqual(Budget.read(values), Budget.DEFAULTS);
  assert.deepEqual(Budget.submission(values, true), {});
  assert.deepEqual(Budget.submission(values), { expansion_resources: Budget.DEFAULTS });
  assert.match(Budget.renderInputs({ ...Budget.DEFAULTS, max_queries: 24 }), /value="24"/);
  values.set("expansion_max_queries", "24");
  assert.equal(Budget.submission(values, true).expansion_resources.max_queries, 24);
  values.delete("expansion_max_queries");
  assert.throws(() => Budget.read(values), /max_queries/);
});

test("running status uses API limits and identifies causal blocker", () => {
  const resources = {
    effective: { policy: { ...Budget.DEFAULTS, max_unique_candidates: 8192 }, source: "explicit", grant_revision: 4, global_model_calls_remaining: 20, global_input_tokens_remaining: 100000 },
    latest_session: {
      session_id: "session-1", state: "blocked", stage: "work_spec",
      limits: { max_queries: 16, max_candidates: 8192, max_exact_reads: 512, max_model_calls: 32, max_tokens: 4000000 },
      usage: { queries: 6, candidates: 8000, exact_reads: 12, model_calls: 4, tokens: 25000 },
      remaining: { queries: 10, unique_candidates: 192, exact_reads: 500, planner_model_calls: 28, planner_tokens: 3975000 },
      blocker_code: "expansion_policy_limit", blocker_boundary: "max_planner_tokens", blocker_operation_id: "work_page:page-1", blocker_summary: "Token ceiling reached",
    },
    repeated_blocker: { code: "expansion_policy_limit", boundary: "max_planner_tokens", consecutive_rounds: 2, action: "Review resource policy" },
  };
  const html = Budget.renderStatus(resources);
  assert.match(html, /唯一候选 8192/);
  assert.match(html, /候选 8000 \/ 8192/);
  assert.match(html, /规划 Token 25000 \/ 4000000（剩余 3975000）/);
  assert.match(html, /阻断层：expansion_policy_limit/);
  assert.match(html, /阶段 work_spec · 边界 max_planner_tokens · 操作 work_page:page-1/);
  assert.match(html, /全局剩余：模型调用 20 · 输入 Token 100000/);
  assert.match(html, /Grant R4/);
  assert.match(html, /连续 2 轮 Context Expansion 阻断/);
});
