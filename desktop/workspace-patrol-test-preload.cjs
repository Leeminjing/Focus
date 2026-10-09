/* 本文件对外提供真实 Patrol/普通会话页面验收的离线 HTTP 与 Live fixture。
 * 输入为 index.html 的实际 API 请求；输出为可变工作区列表、文件夹绑定、稳定输入回执和独立事件。
 * 工作流为沿用普通会话 fixture，仅接管工作区路由；可暂停列表响应或拒绝读取/文件夹选择，发送持久后可延迟响应，Progress/Lineage/Fact 各自提交。
 * 示例：BrowserWindow({ webPreferences: { preload: __filename, contextIsolation: false } })。
 */
require("./context-ui-test-preload.cjs");
const base = window.fetch;
const json = body => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
const loops = new Map();
const records = new Map();
const streams = new Map();
let sequence = 0;
window.patrolFixture = {
  submissions: [], releases: [], hold: false,
  workspaces: ["information", "outcome", "boundary", "completion_check"].map(type => ({ workspace_id: type, display_name: type, path: `C:/test/${type}` })),
  workspaceError: false, holdWorkspaces: false, releaseWorkspaces: null,
  folder: null, folderError: false, workspaceBinds: [],
  lineage: { roots: {}, nodes: [], edges: [], complete: true },
  emit(loopId, entity_type, entity_id, entity_revision, payload) {
    const event = { event_id: `event-${++sequence}`, loop_id: loopId, sequence, kind: `${entity_type}.updated`, entity_type, entity_id, entity_revision, payload, occurred_at: new Date().toISOString(), schema_version: 1 };
    streams.get(loopId)?.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`));
  },
};
window.focusDesktop.selectWorkspace = async () => {
  if (window.patrolFixture.folderError) throw new Error("文件夹选择失败");
  return window.patrolFixture.folder;
};
window.fetch = async (input, options = {}) => {
  const url = new URL(String(input), "http://focus.test");
  const path = url.pathname;
  const prefix = "/desktop/api/agent-loops";
  if (path === `${prefix}/workspace`) {
    const rows = [...window.patrolFixture.workspaces];
    if (window.patrolFixture.holdWorkspaces) await new Promise(resolve => { window.patrolFixture.releaseWorkspaces = resolve; });
    return window.patrolFixture.workspaceError ? new Response("工作区列表读取失败", { status: 500 }) : json(rows);
  }
  if (path === "/desktop/api/workspaces" && options.method === "POST") {
    const body = JSON.parse(options.body);
    window.patrolFixture.workspaceBinds.push(body);
    return json({ workspace_id: "bound-workspace", display_name: "绑定测试", path: body.path });
  }
  const workspace = path.match(/\/workspace\/([^/]+)(\/inputs)?$/);
  if (workspace) {
    const id = workspace[1];
    if (!records.has(id)) {
      const stored = JSON.parse(localStorage.getItem(`patrol-fixture:${id}`) || "[]");
      records.set(id, stored);
      if (stored.length) loops.set(id, true);
    }
    if (!workspace[2]) return json(loops.has(id) ? { loop_id: `loop-${id}`, workspace_id: id } : null);
    if (!options.method || options.method === "GET") return json({ items: [...(records.get(id) || [])].reverse(), next_before: null, has_more: false });
    const body = JSON.parse(options.body);
    window.patrolFixture.submissions.push(body);
    loops.set(id, true);
    const rows = records.get(id) || [];
    let receipt = rows.find(row => row.submission_id === body.submission_id);
    if (!receipt) { receipt = { ...body, intent_id: body.submission_id, loop_id: `loop-${id}`, status: "accepted", created_at: new Date().toISOString() }; rows.push(receipt); records.set(id, rows); }
    localStorage.setItem(`patrol-fixture:${id}`, JSON.stringify(rows));
    if (window.patrolFixture.hold) await new Promise(resolve => window.patrolFixture.releases.push(resolve));
    return json(receipt);
  }
  const live = path.match(/\/agent-loops\/(loop-[^/]+)\/live$/);
  if (live) return json({ loop_id: live[1], last_sequence: sequence,
    loop: { entity_id: live[1], revision: 1, updated_sequence: Math.max(1, sequence), state: { status: "running", interaction_mode: "workspace_patrol", waiting_reason: null } },
    task_progress: { entity_id: live[1], revision: 1, updated_sequence: Math.max(1, sequence), state: { generation: 0, document: { items: [] } } } });
  const stream = path.match(/\/agent-loops\/(loop-[^/]+)\/live\/stream$/);
  if (stream) return new Response(new ReadableStream({ start(controller) { streams.set(stream[1], controller); options.signal?.addEventListener("abort", () => { streams.delete(stream[1]); controller.close(); }, { once: true }); } }), { headers: { "Content-Type": "text/event-stream" } });
  if (path.endsWith("/lineage") && path.startsWith(prefix)) return json(window.patrolFixture.lineage);
  if (path.endsWith("/contexts/root/conversation") && path.startsWith(prefix)) return json({ messages: [{ index: 0, message: { role: "ai", content: "Context 执行结果" } }], total: 1, has_more: false });
  if (path.endsWith("/control") && path.startsWith(prefix)) return json({ status: JSON.parse(options.body).command });
  return base(input, options);
};
