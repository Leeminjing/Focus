/*
 * 本文件对外提供 Context Expansion 只读用量与固定预算授权展示的自动化断言。
 * 输入为有效 API 策略快照与阻断 session；输出为固定预算、真实阶段及逐项用量可见的测试结果。
 * 具体工作流为用公开 UI 模块核对只读预算和真实用量，不依赖真实 Vault 或模型。
 * 示例：`node --test desktop/src/renderer/features/loop/loop-expansion-budget.test.cjs`。
 */
const assert = require("node:assert/strict");
const test = require("node:test");
const Budget = require("./loop-expansion-budget.js");
const GrantView = require("./loop-grant-view.js");

test("grant controls keep fixed unlimited budgets read-only and report actual usage", () => {
  const html = GrantView.grantControls({ status: "running", usage: { rounds: 7, model_calls: 99, input_tokens: 1200, output_tokens: 80, retries: 2 },
    accounting: { consumption: { actual: { model_calls: 90 }, unreported_reservations: { model_calls: 9 } } },
    grant: { capabilities: ["read"], context_scope: ["c1"], budgets: {} } });
  assert.match(html, /预算：无上限（固定）/);
  assert.match(html, /模型调用：已结算 90，预留／待确认 9/);
  assert.match(html, /输入 Token：已结算 1200/);
  assert.match(html, /输出 Token：已结算 80/);
  assert.match(html, /轮次：已结算 7/);
  assert.match(html, /id="agentLoopGrantForm"/);
  assert.doesNotMatch(html, /agentLoopBudgetForm|type="number"|追加|更新预算/);
  assert.equal(Budget.readLoop, undefined);
  assert.equal(Budget.read, undefined);
  assert.equal(Budget.renderLoopInputs, undefined);
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

test("narrowing a grant revokes compression without expanding remaining scopes", () => {
  const previous = globalThis.FormData;
  globalThis.FormData = class { constructor(values) { return new Map(values); } };
  const form = [["capabilities", "continue_context, apply_context_compression"], ["contextScope", " c1, c2 "], ["permissionScope", "read"], ["delegableGates", "compression, access"], ["expiresAt", " "]];
  const grant = {compression_policy: {version: 1}};
  try {
    const revoked = GrantView.readNarrowGrant(form, grant);
    assert.deepEqual(revoked, {command:"narrow",capabilities:["continue_context"],context_scope:["c1","c2"],permission_scope:["read"],delegable_gates:["access"],compression_policy:null,expires_at:null});
    const kept = GrantView.readNarrowGrant([...form, ["autonomousCompression", "on"]], grant);
    assert.equal(kept.compression_policy, grant.compression_policy);
    assert.ok(kept.capabilities.includes("apply_context_compression"));
    assert.ok(kept.delegable_gates.includes("compression"));
  } finally { globalThis.FormData = previous; }
});
