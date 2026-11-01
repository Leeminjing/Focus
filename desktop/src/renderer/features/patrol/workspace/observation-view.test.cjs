/* 本文件对外提供 Observation 版本、因果链及逐轮权限展示的定向测试。
 * 输入为冻结查询页与同 Context 不同 Round 的真实授权形状；输出为精确版本链接、原文转义、独立权限和清理状态断言。
 * 工作流直接渲染白名单读面，不调用 Provider。示例：node --test desktop/src/renderer/features/patrol/workspace/observation-view.test.cjs。
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const View = require("./observation-view.js");

test("compact observation prioritizes source conclusions, committed plans and current blockers", () => {
  const summary = { observation_id: "o1", round_id: "r1", round_number: 6, generation: 3,
    decision_state: "committed", causal_chain_count: 8, sources: { total: 15 }, lineage: { node_count: 12 },
    conclusions: { observed: ["导入测试失败：缺少配置。"], decision: "先补齐配置，再验证导入。" } };
  const projection = { round: { state: { observation_id: "o1" } },
    patrol_session: { state: { observation_id: "o1", wait_reason: "等待提供测试环境。" } },
    wait_requests: { current: { state: { round_id: "r1", status: "open", prompt: "请确认配置文件位置。" } },
      old: { state: { round_id: "old", status: "open", prompt: "旧轮次内容" } },
      answered: { state: { round_id: "r1", status: "answered", prompt: "已解除问题" } } } };
  const html = View.card(summary, { compact: true, projection });
  assert.match(html, /看到了.*导入测试失败.*接下来.*先补齐配置.*当前阻塞.*等待提供测试环境.*请确认配置文件位置/s);
  assert.doesNotMatch(html, /Round|Version|Revision|OBSERVATION|冻结 15|8 条|旧轮次内容|已解除问题/);
  assert.match(View.render({ ...summary, previous_progress: {} }), /8 条输入因果关系/);
});

test("compact observation never invents decisions, evidence or a clear blocker state", () => {
  const summary = { observation_id: "o1", round_id: "r1", decision_state: "current", evidence_visible: false };
  assert.match(View.card(summary, { compact: true }), /当前权限不包含观察正文.*尚未提交后续安排.*尚未取得当前阻塞状态/s);
  const projection = { round: { state: { observation_id: "o1" } }, wait_requests: {},
    patrol_session: { state: { observation_id: "other", wait_reason: "另一个版本的等待" } } };
  const html = View.card({ ...summary, evidence_visible: true }, { compact: true, projection });
  assert.match(html, /尚无可展示的结论.*当前未报告阻塞/s);
  assert.doesNotMatch(html, /另一个版本的等待/);
  assert.match(View.card({ ...summary, decision_state: "rejected" }, { compact: true }), /候选安排未获通过/);
  assert.match(View.card({ ...summary, availability: "legacy" }, { compact: true }), /未保留完整观察内容/);
});

test("compact conclusions and live blockers are escaped and refresh independently", () => {
  const summary = { observation_id: "o1", round_id: "r1", decision_state: "committed",
    conclusions: { observed: ["<script>观察</script>"], decision: "<b>安排</b>" } };
  const projection = { round: { state: { observation_id: "o1" } }, wait_requests: {},
    patrol_session: { state: { observation_id: "o1", wait_reason: "<img>等待" } } };
  const html = View.card(summary, { compact: true, projection });
  assert.match(html, /&lt;script&gt;观察/); assert.match(html, /&lt;b&gt;安排/); assert.match(html, /&lt;img&gt;等待/);
  assert.doesNotMatch(html, /<script>|<img>|<b>/);
  projection.patrol_session.state.wait_reason = null;
  assert.match(View.card(summary, { compact: true, projection }), /当前未报告阻塞/);
  assert.match(View.card(summary, { compact: true, projection, connection: "unavailable" }), /连接中断，阻塞状态待同步/);
});

test("Patrol history renders selected-version phases, waiting workers, failure and empty state", () => {
  const summary = { generation: 1, observation_id: "old-observation", previous_progress: {}, sources: {}, lineage: {} };
  assert.match(View.render(summary), /data-patrol-observation-section="patrol_history"/);
  const html = View.render(summary, { patrol_history: { total: 2, has_more: false,
    patrol_session: { session_id: "old-session", observation_id: "old-observation", phase: "superseded", status: "superseded" },
    items: [{ history_kind: "phase", from_phase: "collecting_curators", to_phase: "superseded", summary: "版本失效",
      occurred_at: "2026-10-18", wait_reason: "等候 <Curator>", wait_targets: [{ entity_type: "worker_request", entity_id: "old-worker" }] },
    { history_kind: "curator", worker_request_id: "old-worker", state: "failed", failure: "<script>超时</script>" }] } });
  assert.match(html, /Patrol 阶段历史 · V1/);
  assert.match(html, /收集 Curator → 已失效/);
  assert.match(html, /等待目标：worker_request · old-worker/);
  assert.match(html, /失败原因：&lt;script&gt;超时/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /old-observation/);
  assert.match(html, /刷新此版本的审计记录/);
  const empty = View.render(summary, { patrol_history: { patrol_session: null, items: [], total: 0 } });
  assert.match(empty, /此 Observation 版本尚无 Patrol 会话记录/);
  assert.doesNotMatch(empty, /old-worker|old-session/);
});

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
