/*
 * 本文件对外提供会话 Patrol 小兵 Electron 回归页的确定性本地 API。输入为桌面页面请求，
 * 输出为三个任务、三名真实 Patrol、零 Agent 待命会话、草稿和 UI 状态写入记录；工作流在沙箱 preload 内提供
 * 最小同源数据与 EventSource 替身，不访问网络或数据库。
 */
"use strict";

const tasks = [
  { task_id: "child", title: "Patrol 会话", workspace_id: "workspace", workspace_name: "测试工作区", workspace_path: "C:/workspace", thread_id: "thread-child", active_run: null },
  { task_id: "root", title: "其他会话", workspace_id: "workspace", workspace_name: "测试工作区", workspace_path: "C:/workspace", thread_id: "thread-root", active_run: null },
  { task_id: "empty", title: "待命 Patrol 会话", workspace_id: "workspace", workspace_name: "测试工作区", workspace_path: "C:/workspace", thread_id: "thread-empty", active_run: null },
];
const tree = [
  { context_id: "root", title: "其他会话", depth: 0, projection_status: "root", editable: false, parents: [] },
  { context_id: "child", title: "Patrol 会话", depth: 1, projection_status: "valid", editable: true, parents: [{ context_id: "root" }] },
  { context_id: "empty", title: "待命 Patrol 会话", depth: 1, projection_status: "valid", editable: true, parents: [{ context_id: "root" }] },
];
const agents = {
  child: [
    {
      agent_id: "patrol-running-0001",
      checkpoint_ns: "patrol:patrol-running-0001",
      permissions: ["read"],
      latest_run: { run_id: "run-running", task_id: "child", agent_id: "patrol-running-0001", kind: "patrol", status: "running" },
    },
    {
      agent_id: "patrol-pending-0002",
      checkpoint_ns: "patrol:patrol-pending-0002",
      permissions: ["read"],
      latest_run: { run_id: "run-pending", task_id: "child", agent_id: "patrol-pending-0002", kind: "patrol", status: "pending" },
    },
  ],
  root: [
    {
      agent_id: "patrol-success-0003",
      checkpoint_ns: "patrol:patrol-success-0003",
      permissions: ["read"],
      latest_run: { run_id: "run-success", task_id: "root", agent_id: "patrol-success-0003", kind: "patrol", status: "success" },
    },
  ],
  empty: [],
};
const uiStates = new Map();
const savedBodies = [];
const eventSources = [];
const draftOpenCalls = [];
const draftSaves = [];

function json(value, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}

window.focusDesktop = { runtime: () => ({ apiBase: "http://focus.test", session: "test-session" }) };
window.fetch = async (input, options = {}) => {
  const requestPath = new URL(String(input), "http://focus.test").pathname;
  if (requestPath === "/desktop/api/bootstrap") return json({ tasks, equipment: { models: [], tools: [], skills: [], permissions: [] }, plugins: [] });
  if (requestPath === "/desktop/api/tasks") return json(tasks);
  if (requestPath === "/desktop/api/workspaces/workspace/contexts/tree") return json(tree);
  const openDraftMatch = requestPath.match(/^\/desktop\/api\/tasks\/([^/]+)\/drafts\/open$/);
  if (openDraftMatch && options.method === "POST") {
    const taskId = openDraftMatch[1];
    draftOpenCalls.push(taskId);
    return json({
      draft_id: `draft-${taskId}`,
      task_id: taskId,
      source_checkpoint_id: null,
      system_prompt: "",
      history_messages: [],
      final_human_message: "",
      equipment: { model_name: null, permissions: ["read"], tools: [], skills: [] },
      token_estimate: 0,
    });
  }
  const saveDraftMatch = requestPath.match(/^\/desktop\/api\/drafts\/([^/]+)$/);
  if (saveDraftMatch && options.method === "PUT") {
    const body = JSON.parse(options.body || "{}");
    draftSaves.push({ draft_id: saveDraftMatch[1], body });
    return json({ draft_id: saveDraftMatch[1], task_id: saveDraftMatch[1].replace(/^draft-/, ""), token_estimate: 0, ...body });
  }
  const agentsMatch = requestPath.match(/^\/desktop\/api\/tasks\/([^/]+)\/agents$/);
  if (agentsMatch) return json(agents[agentsMatch[1]] || []);
  const taskMatch = requestPath.match(/^\/desktop\/api\/tasks\/([^/]+)$/);
  if (taskMatch) {
    const taskId = taskMatch[1];
    return json({
      task_id: taskId,
      messages: [{ role: "human", content: "请检查当前工作区。" }, { role: "ai", content: "正在处理。" }],
      ui_state: uiStates.get(taskId) || {},
      active_run: null,
      context: taskId === "child" ? { context_id: taskId, projection_status: "valid", editable: true, parents: [{ context_id: "root" }] } : null,
    });
  }
  const uiStateMatch = requestPath.match(/^\/desktop\/api\/tasks\/([^/]+)\/ui-state$/);
  if (uiStateMatch && options.method === "PUT") {
    const body = JSON.parse(options.body || "{}");
    uiStates.set(uiStateMatch[1], body);
    savedBodies.push({ path: requestPath, body });
    return json(body);
  }
  if (/\/desktop\/api\/tasks\/[^/]+\/materials$/.test(requestPath)) return json([]);
  if (/\/desktop\/api\/tasks\/[^/]+\/skills$/.test(requestPath)) return json({ skills: [] });
  const historyMatch = requestPath.match(/^\/desktop\/api\/agents\/([^/]+)\/history$/);
  if (historyMatch) return json([
    { role: "human", content: `交给 ${historyMatch[1]} 的任务` },
    { role: "ai", content: "Patrol 已处理。" },
  ]);
  return json({ detail: `Unhandled test route: ${requestPath}` }, 404);
};

class TestEventSource {
  constructor(url) {
    this.url = url;
    this.listeners = new Map();
    eventSources.push(this);
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  close() {}

  emit(type, payload) {
    for (const listener of this.listeners.get(type) || []) listener({ data: JSON.stringify(payload) });
  }
}

window.EventSource = TestEventSource;
window.__patrolAvatarTest = { agents, savedBodies, eventSources, draftOpenCalls, draftSaves };
