/* 本文件对外提供 Observation 版本、因果链及逐轮权限展示的定向测试。
 * 输入为冻结查询页与同 Context 不同 Round 的真实授权形状；输出为精确版本链接、原文转义、独立权限和清理状态断言。
 * 工作流直接渲染白名单读面，不调用 Provider。示例：node --test desktop/observation-view.test.cjs。
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const View = require("./observation-view.js");

test("inspection keeps original input links, boundaries and paging separate from current version", () => {
  const html = View.render({ round_number: 6, generation: 3, observation_id: "o3", frozen_at: "2026-10-11",
    decision_state: "rejected", previous_progress: {}, sources: { total: 0 }, lineage: { node_count: 0 },
    boundary: { server_sequence: 99, live_sequence: 80, round_count: 2, projection_cursor: 70, projection_failures: 1 },
  }, { causal_chain: { total: 3, has_more: true, items: [{ generation: 1, observation_id: "o1", accepted_sequence: 81,
    input: { content: "<script>原始方向</script>" } }] } });
  assert.match(html, /data-patrol-observation-version="o1"/);
  assert.match(html, /候选被拒绝 · 尚未提交/);
  assert.match(html, /服务器受理前边界 99/);
  assert.match(html, /客户端 Live 位置 80/);
  assert.match(html, /已读取 1 \/ 3 项/);
  assert.match(html, /&lt;script&gt;原始方向/);
  assert.doesNotMatch(html, /<script>/);
});

test("same Context retains R5 writable and R6 read-only authorization through cancellation cleanup", () => {
  const entity = (entity_id, state) => ({ entity_id, state });
  const projection = { rounds: { r5: entity("r5", { number: 5, status: "executing" }), r6: entity("r6", { number: 6, status: "executing" }) },
    runs: { a: entity("a", { round_id: "r5", context_id: "same", status: "running", settlement_status: "pending", round_authorization: { access_mode: "workspace-write", source: "patrol" }, permission_state: { valid: true } }),
      b: entity("b", { round_id: "r6", context_id: "same", status: "interrupted", settlement_status: "pending", round_authorization: { access_mode: "read-only", source: "patrol" }, permission_state: { valid: true } }) } };
  const html = View.rounds(projection);
  assert.match(html, /Round 5/);
  assert.match(html, /工作区可写 · Patrol 本轮授权 · 有效/);
  assert.match(html, /Round 6/);
  assert.match(html, /只读执行 · Patrol 本轮授权 · 已失效/);
  assert.match(html, /等待执行清理与结算/);
});
