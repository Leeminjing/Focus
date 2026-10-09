/* 本文件对外提供真实 Patrol/普通会话页面验收的离线 HTTP 与 Live fixture。
 * 输入为 index.html 的实际 API 请求和系统目录选择调用；输出为精确选中路径、文件夹绑定、稳定输入回执和独立事件。
 * 工作流为沿用普通会话 fixture，可取消/拒绝/暂停目录选择和绑定；旧目录列表请求显式失败，发送回执和观测仍独立提交。
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
  folder: null, folderError: false, folderCalls: 0, holdFolder: false, releaseFolder: null,
  workspaceBinds: [], bindError: false, holdBind: false, releaseBind: null, historyListRequests: 0,
  lineage: { roots: {}, nodes: [], edges: [], complete: true },
  emit(loopId, entity_type, entity_id, entity_revision, payload) {
    const event = { event_id: `event-${++sequence}`, loop_id: loopId, sequence, kind: `${entity_type}.updated`, entity_type, entity_id, entity_revision, payload, occurred_at: new Date().toISOString(), schema_version: 1 };
    streams.get(loopId)?.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`));
  },
};
window.focusDesktop.selectWorkspace = async () => {
  window.patrolFixture.folderCalls++;
  if (window.patrolFixture.folderError) throw new Error("文件夹选择失败");
  const chosen = window.patrolFixture.folder;
  if (window.patrolFixture.holdFolder) await new Promise(resolve => { window.patrolFixture.releaseFolder = resolve; });
  return chosen;
};
window.fetch = async (input, options = {}) => {
  const url = new URL(String(input), "http://focus.test");
  const path = url.pathname;
  const prefix = "/desktop/api/agent-loops";
  if (path === `${prefix}/workspace`) {
    window.patrolFixture.historyListRequests++;
    return new Response("工作区必须从系统文件夹选择器选择", { status: 500 });
  }
  if (path === "/desktop/api/workspaces" && options.method === "POST") {
    const body = JSON.parse(options.body);
    window.patrolFixture.workspaceBinds.push(body);
    if (window.patrolFixture.holdBind) await new Promise(resolve => { window.patrolFixture.releaseBind = resolve; });
    if (window.patrolFixture.bindError) return new Response("工作区绑定失败", { status: 500 });
    const name = body.path.split(/[\\/]/).filter(Boolean).at(-1);
    return json({ workspace_id: name, display_name: name, path: body.path });
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
