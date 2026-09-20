/*
 * 本文件对外提供 Agent Loop Electron 回归页的确定性本地 API。
 * 输入为真实 Desktop 页面发出的结构化 Mission、Loop、Console、完整会话、事实、介入、压缩恢复与 workspace 请求；输出为
 * 可变的长期 Loop 快照、Context Portfolio 和审计投影。具体工作流为复用 Context 测试 API，再拦截
 * Loop 领域路由，模拟后继激活 eligibility、阈值、候选、Kernel commit、自动 resume/显式 Mission revision，记录来源恢复与三类用户介入，而不访问网络或数据库；示例：在 BrowserWindow preload 中加载本文件。
 */
"use strict";

require("./context-ui-test-preload.cjs");

const baseFetch = window.fetch;
const json = (value, status = 200) => new Response(JSON.stringify(value), {
  status,
  headers: { "Content-Type": "application/json" },
});
const loop = {
  snapshot: null,
  startBody: null,
  overrides: [],
  grantMutations: [],
  controls: [],
  interventions: [],
  directMessages: [],
  restores: [],
  compressionRestored: false,
  cursor: 0,
};

function runningSnapshot(body) {
  const mission = body.mission;
  return {
    loop_id: body.loop_id,
    workspace_id: body.workspace_id,
    initial_context_id: body.initial_context_id,
    program_id: "program-loop",
    status: "running",
    health: "observing",
    waiting_reason: null,
    holder_id: body.holder_id,
    goal_revision: 1,
    authority_revision: 1,
    current_round_id: "round-12",
    mission,
    active_mission_revision: 1,
    goal: {
      goal: mission.outcome,
      task_contract: [...mission.boundaries.in_scope, ...mission.boundaries.required_invariants, ...mission.boundaries.prohibited_actions].join("\n"),
      acceptance_criteria: mission.completion_checks.map(item => ({ criterion_id: item.check_id, text: item.claim, required: item.required })),
    },
    grant: { budgets: body.budgets, capabilities: body.capabilities, context_scope: body.context_scope, permission_scope: body.permission_scope, delegable_gates: body.delegable_gates, compression_policy: body.compression_policy, expires_at: null },
    usage: { rounds: 13, duration_seconds: 180, model_calls: 43, input_tokens: 14192, output_tokens: 2304, retries: 2, lanes: 4, contexts: 5, providers: 2 },
    memberships: [
      { context_id: "root", lane_id: "implementation", state: "active" },
      { context_id: "child", lane_id: "testing", state: "active" },
      { context_id: "merged", lane_id: "architecture", state: "active" },
      { context_id: "sibling", lane_id: "requirements", state: "active" },
    ],
  };
}

const portfolio = {
  lanes: [
    { lane_id: "implementation", purpose: "继续实现" },
    { lane_id: "testing", purpose: "测试与故障分析" },
    { lane_id: "architecture", purpose: "架构审查" },
    { lane_id: "requirements", purpose: "需求偏航检查" },
    { lane_id: "abandoned", purpose: "已放弃探索" },
  ],
  portfolios: [
    { generation: 11, candidates: [{ lane_id: "implementation", action: "keep", target_context_id: "root", candidate_context_revision_id: "root-r4", status: "published" }] },
    { generation: 12, candidates: [
      { lane_id: "implementation", action: "update", target_context_id: "root", candidate_context_revision_id: "root-r5", status: "published" },
      { lane_id: "testing", action: "create", target_context_id: "child", candidate_context_revision_id: "test-r1", status: "published" },
      { lane_id: "architecture", action: "create", target_context_id: "merged", candidate_context_revision_id: "arch-r1", status: "published" },
      { lane_id: "requirements", action: "create", target_context_id: "sibling", candidate_context_revision_id: "req-r1", status: "published" },
      { lane_id: "abandoned", action: "pause", target_context_id: "extra-1", candidate_context_revision_id: "old-r2", status: "published" },
    ] },
  ],
};

const evolution = {
  revisions: [
    { revision_id: "root-r5", context_id: "root", generation: 5 },
    { revision_id: "test-r1", context_id: "child", generation: 1 },
    { revision_id: "arch-r1", context_id: "merged", generation: 1 },
  ],
  edges: [
    { target_revision_id: "arch-r1", source_revision_id: "root-r5", position: 0 },
    { target_revision_id: "arch-r1", source_revision_id: "test-r1", position: 1 },
  ],
};

const audit = {
  pending_decisions: [
    { pending_decision_id: "compression-pending-1", kind: "compression", delegable: true, status: "resolved", payload: { context_id: "root", context_revision_id: "root-r5", checkpoint_id: "checkpoint-before" } },
  ],
  decisions: [
    { decision_id: "compression-decision-1", round_id: "round-12", rationale: "继续当前实现身份，但将已解决调试历史压缩为可恢复摘要。", status: "committed" },
  ],
  compression_candidates: [
    { candidate_id: "compression-candidate-1", pending_decision_id: "compression-pending-1", context_id: "root", status: "accepted", before_tokens: 6000, after_tokens: 900, estimated_reduction: 5100, source_ranges: [{ source_ids: ["debug-1", "debug-2", "debug-3"] }], protection_evidence: [{ message_id: "root-human", reason: "current_direct_user_message", overlap: false }] },
  ],
  compression_resolutions: [
    { resolution_id: "compression-resolution-1", candidate_id: "compression-candidate-1", decision_id: "compression-decision-1", run_id: "compression-resume-1", status: "applied", result_checkpoint_id: "checkpoint-after", result_context_revision_id: "root-r6", actual_reduction: 5100 },
  ],
  directives: [
    { directive_id: "directive-12", round_id: "round-12", status: "launched", content: "暂停修改代码，只分析过去三轮失败的共同原因。" },
  ],
  runs: [
    { run_id: "run-testing", context_id: "child", status: "running" },
    { run_id: "run-architecture", context_id: "merged", status: "success" },
  ],
};

const consoleManifest = {
  loop_id: "loop-test",
  loop_revision: 1,
  status: "running",
  health: "observing",
  current_round_id: "round-12",
  initial_context_id: "root",
  nodes: [
    { context_id: "root", title: "实现", topic: "继续实现", purpose: "完成核心功能", role: "primary", status: "active", lane_id: "implementation", revision: { revision_id: "root-r5", generation: 5, projection_status: "valid" }, latest_run: { run_id: "run-root", status: "success" }, counts: { runs: 5, delegated_messages: 2 } },
    { context_id: "child", title: "测试", topic: "测试与故障分析", purpose: "定位失败并验证修复", role: "derived", status: "active", lane_id: "testing", revision: { revision_id: "test-r1", generation: 1, projection_status: "valid" }, latest_run: { run_id: "run-testing", status: "running" }, counts: { runs: 3, delegated_messages: 1 } },
    { context_id: "merged", title: "架构", topic: "架构审查", purpose: "反方审查设计风险", role: "derived", status: "active", lane_id: "architecture", revision: { revision_id: "arch-r1", generation: 1, projection_status: "valid" }, latest_run: { run_id: "run-architecture", status: "success" }, counts: { runs: 1, delegated_messages: 1 } },
    { context_id: "sibling", title: "需求", topic: "需求偏航检查", purpose: "对照 Task Contract", role: "derived", status: "active", lane_id: "requirements", revision: { revision_id: "req-r1", generation: 1, projection_status: "valid" }, latest_run: null, counts: { runs: 0, delegated_messages: 0 } },
    { context_id: "extra-1", title: "探索", topic: "已放弃探索", purpose: "保留但不再采用", role: "side", status: "paused", lane_id: "abandoned", revision: { revision_id: "old-r2", generation: 2, projection_status: "valid" }, latest_run: null, counts: { runs: 1, delegated_messages: 1 } },
  ],
  edges: [
    { source_context_id: "root", source_revision_id: "root-r5", target_context_id: "child", target_revision_id: "test-r1", position: 0 },
    { source_context_id: "root", source_revision_id: "root-r5", target_context_id: "merged", target_revision_id: "arch-r1", position: 0 },
    { source_context_id: "child", source_revision_id: "test-r1", target_context_id: "merged", target_revision_id: "arch-r1", position: 1 },
    { source_context_id: "root", source_revision_id: "root-r5", target_context_id: "sibling", target_revision_id: "req-r1", position: 0 },
  ],
  user_intents: [],
};

function conversation(contextId) {
  const messages = contextId === "child" ? [
    { index: 0, message: { id: "test-human", role: "human", content: "只定位三个失败测试的共同原因。" }, provenance: { source_kind: "delegated_patrol", actor_id: "patrol:loop-test" } },
    { index: 1, message: { id: "test-tool", role: "tool", name: "pytest", tool_call_id: "call-test", content: "12 passed, 2 failed, 1 skipped" }, provenance: null },
  ] : loop.compressionRestored ? [
    { index: 0, message: { id: "root-human", role: "human", content: "暂停修改代码，只分析过去三轮失败的共同原因。" }, provenance: { source_kind: "delegated_patrol", actor_id: "patrol:loop-test" } },
    { index: 1, message: { id: "debug-1", role: "tool", content: "first failed trace" }, provenance: null },
    { index: 2, message: { id: "debug-2", role: "assistant", content: "second debugging attempt" }, provenance: null },
    { index: 3, message: { id: "debug-3", role: "tool", content: "third failed trace" }, provenance: null },
  ] : [
    { index: 0, message: { id: "root-human", role: "human", content: "暂停修改代码，只分析过去三轮失败的共同原因。" }, provenance: { source_kind: "delegated_patrol", actor_id: "patrol:loop-test" } },
    { index: 1, message: { id: "compression-block-1", role: "human", content: "三轮调试均未改变同一个 session uploader 初始化失败。", compression: { source: [{ id: "debug-1", role: "tool", content: "first failed trace" }, { id: "debug-2", role: "assistant", content: "second debugging attempt" }, { id: "debug-3", role: "tool", content: "third failed trace" }] } }, provenance: null },
    { index: 2, message: { id: "root-ai", role: "assistant", content: "压缩恢复后已继续下一轮并记录证据。" }, provenance: null },
  ];
  return { context_id: contextId, revision: { revision_id: `${contextId}-revision`, generation: 1 }, projection_status: "valid", total: messages.length, range: { start: 0, end: messages.length }, next_before: null, has_more: false, messages };
}

const factRows = [
  { fact_id: "fact-run", context_id: "root", kind: "run", status: "success", title: "Agent Run success", summary: "完成实现阶段", metrics: {}, evidence: { run_id: "run-root" }, occurred_at: "2026-09-16T01:00:00Z" },
  { fact_id: "fact-test", context_id: "child", kind: "test", status: "failed", title: "测试结果", summary: "12 passed · 2 failed · 1 skipped", metrics: { passed: 12, failed: 2, skipped: 1, count_status: "exact" }, evidence: { message_id: "test-tool", tool_name: "pytest" }, occurred_at: "2026-09-16T01:01:00Z" },
];

function liveSnapshot() {
  const envelope = (entityId, state, revision = 1) => ({ entity_id: entityId, revision, updated_sequence: 1, state });
  const contexts = Object.fromEntries(consoleManifest.nodes.map(node => [node.context_id, envelope(node.context_id, {
    title: node.title,
    role: node.role,
    status: node.status,
    lane_id: node.lane_id,
    current_revision_id: node.revision?.revision_id,
  })]));
  const runs = Object.fromEntries(audit.runs.map(run => [run.run_id, envelope(run.run_id, {
    ...run,
    tool_name: run.status === "running" ? "pytest" : "analysis",
    workspace_changes: run.status === "running" ? 2 : 0,
    input_tokens: run.status === "running" ? 4200 : 1800,
    output_tokens: run.status === "running" ? 620 : 240,
  })]));
  const directives = Object.fromEntries(audit.directives.map(item => [item.directive_id, envelope(item.directive_id, {
    ...item,
    target_context_id: "child",
    correlation_id: item.directive_id,
  })]));
  const facts = Object.fromEntries(factRows.map(item => [item.fact_id, envelope(item.fact_id, item)]));
  const mission = loop.snapshot.mission;
  return {
    loop_id: loop.snapshot.loop_id,
    last_sequence: 1,
    loop: envelope(loop.snapshot.loop_id, loop.snapshot, loop.snapshot.authority_revision || 1),
    mission: envelope(`mission-${loop.snapshot.goal_revision || 1}`, mission),
    patrol_session: envelope("patrol-session-12", { state: "observing", phase: "collecting", safe_summary: "正在观察多个 Context 并汇总证据" }),
    round: envelope("round-12", { round_id: "round-12", number: 13, state: "running" }),
    contexts,
    runs,
    curators: {
      "curator-testing": envelope("curator-testing", { lane_id: "testing", state: "analyzing", safe_summary: "正在检查测试证据" }),
      "curator-architecture": envelope("curator-architecture", { lane_id: "architecture", state: "reading", safe_summary: "正在比较 Context 设计" }),
    },
    directives,
    facts,
    portfolio: envelope("portfolio-12", { generation: 12, status: "published" }),
    activity_timeline: [{ event_id: "live-event-1", sequence: 1, kind: "patrol.directive.delivered", entity_type: "directive", entity_id: "directive-12", summary: "Patrol 指令已送达 Testing Context", occurred_at: "2026-09-16T01:01:00Z", correlation_id: "directive-12", causation_id: null, detail: { context_id: "child", directive_id: "directive-12", status: "launched" } }],
    unknown_kinds: [],
    diagnostics: { journal_last_sequence: 1, projector_last_sequence: 1, lag: 0, rebuilt: false, updated_at: "2026-09-16T01:01:00Z" },
  };
}

window.__agentLoopTest = loop;
window.fetch = async (input, options = {}) => {
  const url = new URL(String(input), "http://focus.test");
  const path = url.pathname;
  if (path === "/desktop/api/tasks/root") return json({ task_id: "root", messages: [], ui_state: {}, active_run: { run_id: "run-initial", status: "success", origin: "direct_user", loop_id: loop.snapshot?.loop_id || null }, latest_direct_user_run: { run_id: "run-initial", status: "success", origin: "direct_user", loop_id: loop.snapshot?.loop_id || null }, context: null });
  if (/^\/desktop\/api\/tasks\/[^/]+$/.test(path)) return json({ task_id: decodeURIComponent(path.split("/").at(-1)), messages: [], ui_state: { _main_run_equipment: { permissions: ["read", "write"], skills: [], access_mode: "workspace" } }, context: null });
  if (path === "/desktop/api/agent-loops/activation-eligibility/by-context/root") return json({ context_id: "root", eligible: !loop.snapshot, candidate_run_id: "run-initial", candidate_status: "success", predecessor_loop_id: loop.snapshot?.loop_id || null, reason: loop.snapshot ? "newest_run_already_bound" : null, consistency_token: "a".repeat(64) });
  if (path === "/desktop/api/agent-loops/by-context/root") return json(["running", "pausing", "paused", "waiting_user", "completing"].includes(loop.snapshot?.status) ? loop.snapshot : null);
  if (path === "/desktop/api/agent-loops" && options.method === "POST") {
    loop.startBody = JSON.parse(options.body);
    loop.snapshot = runningSnapshot(loop.startBody);
    return json(loop.snapshot);
  }
  if (/^\/desktop\/api\/agent-loops\/[^/]+$/.test(path)) return json(loop.snapshot);
  if (/\/desktop\/api\/agent-loops\/[^/]+\/live$/.test(path)) return json(liveSnapshot());
  if (/\/desktop\/api\/agent-loops\/[^/]+\/live\/stream$/.test(path)) {
    const body = new ReadableStream({
      start(controller) {
        options.signal?.addEventListener("abort", () => controller.error(new DOMException("Aborted", "AbortError")), { once: true });
      },
    });
    return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
  }
  if (/\/desktop\/api\/agent-loops\/[^/]+\/console$/.test(path)) return json({ ...consoleManifest, loop_id: loop.snapshot?.loop_id || consoleManifest.loop_id, status: loop.snapshot?.status || "running", health: loop.snapshot?.health || "observing" });
  if (/\/desktop\/api\/agent-loops\/[^/]+\/contexts\/[^/]+\/conversation$/.test(path)) return json(conversation(decodeURIComponent(path.split("/").at(-2))));
  if (/\/desktop\/api\/agent-loops\/[^/]+\/facts$/.test(path)) {
    const contextId = url.searchParams.get("context_id");
    const facts = contextId ? factRows.filter(item => item.context_id === contextId) : factRows;
    return json({ total: facts.length, range: { start: 0, end: facts.length }, next_before: null, has_more: false, facts });
  }
  if (/\/desktop\/api\/agent-loops\/[^/]+\/interventions$/.test(path) && options.method === "POST") {
    const body = JSON.parse(options.body);
    loop.interventions.push(body);
    return json({ intent_id: `intent-${loop.interventions.length}`, scope: body.mode === "patrol_context_intent" ? "context" : "portfolio", status: "pending" });
  }
  if (/\/desktop\/api\/tasks\/[^/]+\/main\/runs$/.test(path) && options.method === "POST") {
    const body = JSON.parse(options.body);
    loop.directMessages.push({ context_id: decodeURIComponent(path.split("/").at(-3)), ...body });
    return json({ run_id: `direct-${loop.directMessages.length}`, status: "pending" });
  }
  if (path === "/desktop/api/compression/quick-apply" && options.method === "POST") {
    const body = JSON.parse(options.body);
    loop.restores.push(body);
    loop.compressionRestored = Boolean(body.ranges?.some(item => item.restore));
    return json({ messages: conversation(body.task_id).messages.map(item => item.message) });
  }
  if (/\/desktop\/api\/agent-loops\/[^/]+\/control$/.test(path) && options.method === "POST") {
    const command = JSON.parse(options.body).command;
    loop.controls.push(command);
    loop.snapshot = { ...loop.snapshot, status: command === "resume" ? "running" : command === "pause" ? "paused" : "stopped", health: command === "resume" ? "observing" : "idle" };
    return json(loop.snapshot);
  }
  if (/\/desktop\/api\/agent-loops\/[^/]+\/(?:override|missions)$/.test(path) && options.method === "POST") {
    const body = JSON.parse(options.body);
    loop.overrides.push(body);
    const mission = body.mission || body;
    loop.snapshot = { ...loop.snapshot, status: "running", goal_revision: loop.snapshot.goal_revision + 1, authority_revision: loop.snapshot.authority_revision + 1, mission, goal: { goal: mission.outcome, task_contract: "", acceptance_criteria: mission.completion_checks || [] }, health: "observing", waiting_reason: null };
    return json(loop.snapshot);
  }
  if (/\/desktop\/api\/agent-loops\/[^/]+\/grant$/.test(path) && options.method === "POST") {
    const body = JSON.parse(options.body);
    loop.grantMutations.push(body);
    if (body.command === "revoke") {
      loop.snapshot = { ...loop.snapshot, status: "waiting_user", health: "idle", authority_revision: loop.snapshot.authority_revision + 1, grant: null, waiting_reason: "用户已撤销 Patrol delegation" };
    } else if (body.command === "adjust_budgets") {
      loop.snapshot = { ...loop.snapshot, authority_revision: loop.snapshot.authority_revision + 1, grant: { ...loop.snapshot.grant, budgets: body.budgets } };
    } else {
      loop.snapshot = { ...loop.snapshot, authority_revision: loop.snapshot.authority_revision + 1, grant: { ...loop.snapshot.grant, ...body } };
    }
    return json(loop.snapshot);
  }
  if (/\/desktop\/api\/agent-loops\/[^/]+\/events$/.test(path)) return json([]);
  if (/\/desktop\/api\/agent-loops\/[^/]+\/events\/stream$/.test(path)) {
    const encoder = new TextEncoder();
    const event = { event_id: "loop-event-1", cursor: 1, type: "RoundObserved", payload: { round_id: "round-12" } };
    return new Response(new ReadableStream({ start(controller) { controller.enqueue(encoder.encode(`id: loop-event-1\nevent: RoundObserved\ndata: ${JSON.stringify(event)}\n\n`)); controller.close(); } }), { status: 200, headers: { "Content-Type": "text/event-stream" } });
  }
  if (path === "/desktop/api/curation-programs/program-loop") return json(portfolio);
  if (path === "/desktop/api/workspaces/workspace/context-evolution") return json(evolution);
  if (path === "/desktop/api/workspaces/workspace/context-tree") return json([]);
  if (/\/desktop\/api\/agent-loops\/[^/]+\/audit$/.test(path)) return json(audit);
  if (path === "/desktop/api/workspaces/workspace/slots") return json([{ slot_id: "slot-main", kind: "authoritative", owner_lane_id: "implementation", state: "active" }, { slot_id: "slot-test", kind: "isolated", owner_lane_id: "testing", state: "active", adoption: { status: "pending" } }]);
  if (/\/desktop\/api\/context-revisions\//.test(path)) return json({ ref: { context_id: "merged", revision_id: "arch-r1", generation: 1 }, projection_status: "valid", authored_messages: [{ role: "human", content: "审查架构" }] });
  return baseFetch(input, options);
};
