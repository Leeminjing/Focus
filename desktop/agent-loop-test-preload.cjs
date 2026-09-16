/*
 * 本文件对外提供 Agent Loop Electron 回归页的确定性本地 API。
 * 输入为真实 Desktop 页面发出的 Loop、Portfolio、Evolution、audit 与 workspace-slot 请求；输出为
 * 可变的长期 Loop 快照和审计投影。具体工作流为复用 Context 测试 API，再拦截 Loop 领域路由，
 * 记录启动、控制和用户覆盖请求而不访问网络或数据库；示例：在 BrowserWindow preload 中加载本文件。
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
  cursor: 0,
};

function runningSnapshot(body) {
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
    goal: {
      goal: body.goal,
      task_contract: body.task_contract,
      acceptance_criteria: body.acceptance_criteria,
    },
    grant: { budgets: body.budgets, capabilities: body.capabilities, context_scope: body.context_scope, permission_scope: body.permission_scope, delegable_gates: body.delegable_gates, expires_at: null },
    usage: { rounds: 12, duration_seconds: 180, model_calls: 41, input_tokens: 8192, output_tokens: 2048, retries: 2, lanes: 4, contexts: 5, providers: 2 },
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
  directives: [
    { directive_id: "directive-12", round_id: "round-12", status: "launched", content: "暂停修改代码，只分析过去三轮失败的共同原因。" },
  ],
  runs: [
    { run_id: "run-testing", context_id: "child", status: "running" },
    { run_id: "run-architecture", context_id: "merged", status: "success" },
  ],
};

window.__agentLoopTest = loop;
window.fetch = async (input, options = {}) => {
  const url = new URL(String(input), "http://focus.test");
  const path = url.pathname;
  if (path === "/desktop/api/tasks/root") return json({ task_id: "root", messages: [], ui_state: {}, active_run: { run_id: "run-initial", status: "success" }, context: null });
  if (path === "/desktop/api/agent-loops/by-context/root") return json(loop.snapshot);
  if (path === "/desktop/api/agent-loops" && options.method === "POST") {
    loop.startBody = JSON.parse(options.body);
    loop.snapshot = runningSnapshot(loop.startBody);
    return json(loop.snapshot);
  }
  if (/^\/desktop\/api\/agent-loops\/[^/]+$/.test(path)) return json(loop.snapshot);
  if (/\/desktop\/api\/agent-loops\/[^/]+\/control$/.test(path) && options.method === "POST") {
    const command = JSON.parse(options.body).command;
    loop.controls.push(command);
    loop.snapshot = { ...loop.snapshot, status: command === "resume" ? "running" : command === "pause" ? "paused" : "stopped", health: command === "resume" ? "observing" : "idle" };
    return json(loop.snapshot);
  }
  if (/\/desktop\/api\/agent-loops\/[^/]+\/override$/.test(path) && options.method === "POST") {
    const body = JSON.parse(options.body);
    loop.overrides.push(body);
    loop.snapshot = { ...loop.snapshot, status: "running", goal_revision: loop.snapshot.goal_revision + 1, authority_revision: loop.snapshot.authority_revision + 1, goal: body, health: "observing", waiting_reason: null };
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
