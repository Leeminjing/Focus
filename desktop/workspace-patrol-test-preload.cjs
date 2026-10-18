/* 本文件对外提供真实 Patrol/普通会话页面验收的离线 HTTP 与 Live fixture。
 * 输入为 index.html 的实际 API 请求和系统目录选择调用；输出为精确选中路径、文件夹绑定、稳定输入回执和独立事件。
 * 工作流为沿用普通会话 fixture，可取消/拒绝/暂停目录选择和绑定；旧目录列表请求显式失败，发送回执和观测独立提交，冻结覆盖不完整但页已读完，记录装备调用的实际 draft-open 路由；可注入图读取失败、删除冲突及精确版本会话，均仅限隔离测试。
 * 示例：BrowserWindow({ webPreferences: { preload: __filename, contextIsolation: false } })。
  * fileFeedbackFetch 是文件版本/策略页面测试的可选 HTTP fixture 扩展入口。
 * 历史读取按 before 身份倒序分页，historyPageSize/holdHistory/failHistory 可验证连续滚动、失败与迟到响应；seedHistory 仅向隔离 fixture 写入记录。
*/
require("./context-ui-test-preload.cjs");
const base = window.fetch;
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const loops = new Map();
const records = new Map();
const streams = new Map();
let sequence = 0;
window.patrolFixture = {
  requests: [], releaseBootstrap: null,
  pageError: null, pagesEmpty: false, reloads: 0, mainSubmissions: [], waitAnswers: [], grantChanges: [],
  observationError: false,
  historyPageSize: 50, historyRequests: [], historyReleases: [], holdHistory: false, failHistory: false,
  seedHistory(id, rows) { records.set(id, rows); loops.set(id, true); localStorage.setItem(`patrol-fixture:${id}`, JSON.stringify(rows)); },
  submissions: [], releases: [], hold: false, failReply: false, draftOpens: [],
  folder: null, folderError: false, folderCalls: 0, holdFolder: false, releaseFolder: null,
  workspaceBinds: [], bindError: false, holdBind: false, releaseBind: null, historyListRequests: 0,
  connected: loopId => streams.has(loopId),
  get sequence() { return sequence; },
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
if (process.env.FOCUS_PATROL_TEST_NATIVE === "1") {
  window.patrolFixture.robotPanelOpens = [];
  window.patrolFixture.robotReady = false;
  window.focusDesktop.patrol = {
    enabled: true,
    openPanel: async target => { window.patrolFixture.robotPanelOpens.push(target); },
    onWorkbench: listener => { window.patrolFixture.openWorkbench = listener; },
    ready: async () => { window.patrolFixture.robotReady = true; },
  };
}
window.fetch = async (input, options = {}) => {
  const fileFeedback = await window.fileFeedbackFetch?.(input, options);
  if (fileFeedback) return fileFeedback;
  const url = new URL(String(input), "http://focus.test");
  const path = url.pathname;
  window.patrolFixture.requests.push(path);
  if (path === "/desktop/api/materials/limits") return json({general_file_bytes:1024*1024,original_image_bytes:512*1024,run_material_count:4});
  if (path === "/desktop/api/settings/models") return json({setup_required:false});
  if (path === "/desktop/api/bootstrap" && process.env.FOCUS_TEST_HOLD_BOOTSTRAP === "1" && !sessionStorage.getItem("focus-test-bootstrap-held")) {
    sessionStorage.setItem("focus-test-bootstrap-held", "1");
    await new Promise(resolve => { window.patrolFixture.releaseBootstrap = resolve; });
  }
  const prefix = "/desktop/api/agent-loops";
  if (path === `${prefix}/budget-presets`) return json(require("./workspace-patrol-budget-fixture.cjs"));
  if (path === '/desktop/api/contexts/batch-delete' && window.patrolFixture.mapDeletes) {
    const body = JSON.parse(options.body);
    window.patrolFixture.mapDeletes.push(body);
    if (window.patrolFixture.deleteError) return new Response('删除被拒绝', {status:409});
    const removed = new Set(body.context_ids);
    const snapshot = window.patrolFixture.lineage;
    snapshot.nodes = snapshot.nodes.filter(node => !removed.has(node.context_id));
    snapshot.edges = snapshot.edges.filter(edge => !removed.has(edge.source_context_id) && !removed.has(edge.target_context_id));
    for (const id of removed) delete snapshot.roots[id];
    return json({deleted_context_ids:body.context_ids});
  }
  const openDraft = path.match(/^\/desktop\/api\/tasks\/([^/]+)\/drafts\/open$/);
  if (openDraft && options.method === "POST") {
    const taskId = openDraft[1];
    window.patrolFixture.draftOpens.push(taskId);
    return json({draft_id:`draft-${taskId}`,task_id:taskId,source_checkpoint_id:null,system_prompt:"",history_messages:[],final_human_message:"",equipment:{model_name:null,permissions:["read"],tools:[],skills:[]},token_estimate:0});
  }
  if (path === "/desktop/api/plugins" || path === "/desktop/api/plugins/traces" || path === "/desktop/api/plugins/reload" || path === "/desktop/api/memory") {
    if (window.patrolFixture.pageError) return new Response(window.patrolFixture.pageError, { status: 503 });
    if (path.endsWith("/reload")) window.patrolFixture.reloads++;
    if (path.includes("/plugins")) return json(path.endsWith("/traces") ? { traces: [] } : { plugins: window.patrolFixture.pagesEmpty ? [] : [
      { name: "文件系统", version: "1.0", status: "active", injected: ["workspace.files"], requires: [] },
      { name: "浏览器", version: "1.0", status: "unavailable", injected: [], missing: ["browser-host"] },
      { name: "重复工具", version: "1.0", status: "rejected", injected: [], reason: "接口冲突" },
    ], interfaces: {} });
    return json({ memories: window.patrolFixture.pagesEmpty ? [] : [
      { memory_id: "memory-1", title: "交付范围：优先本地体验", content: "本次交付只包含本地会话与配置保存。", source_kind: "manual", updated_at: "2026-10-10" },
      { memory_id: "memory-2", title: "保持旁支讨论独立", content: "确认后的结论再进入相关实现。", source_kind: "partial_messages", updated_at: "2026-10-10" },
    ] });
  }
  if (path === "/desktop/api/assembly/task") return json({ task_id: "assembly", title: "自由会话", harness_mode: "assembly", active_run: null });
  if (path === "/desktop/api/sessions/archived") return json([]);
  if (path.endsWith("/console") && path.startsWith(prefix)) return json(window.patrolFixture.mapConsole || { loop_id: path.split("/").at(-2), nodes: [{context_id:"root", revision_id:"r1", title:"产品想法与范围", status:"active"}], edges: [] });
  if (/\/agent-loops\/loop-[^/]+$/.test(path)) return json({loop_id:path.split("/").at(-1), status:"running", grant:{capabilities:["read"],context_scope:["root"],permission_scope:["read"],delegable_gates:[],budgets:{max_rounds:7}}});
  if (path.endsWith("/grant") && path.startsWith(prefix)) { window.patrolFixture.grantChanges.push({loopId:path.split("/").at(-2),body:JSON.parse(options.body)}); return json({status:"waiting_user"}); }
  if (path.endsWith("/responses") && path.startsWith(prefix)) { window.patrolFixture.waitAnswers.push(JSON.parse(options.body)); return json({status:"accepted"}); }
  if (path === "/desktop/api/tasks/assembly") return json({ task_id: "assembly", harness_mode: "assembly", messages: [], ui_state: {}, active_run: null });
  if (/\/desktop\/api\/tasks\/[^/]+\/main\/runs$/.test(path)) {
    window.patrolFixture.mainSubmissions.push(JSON.parse(options.body));
    return json({ intent_id: "accepted-direct-message", intent_kind: "direct_message", status: "accepted", loop_id: "task-loop" });
  }
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
    if (!options.method || options.method === "GET") {
      const before = url.searchParams.get("before"), requestedIds = url.searchParams.getAll("intent_ids");
      const rows = [...(records.get(id) || [])].reverse().filter(row => !requestedIds.length || requestedIds.includes(row.intent_id));
      const start = before ? rows.findIndex(row => row.intent_id === before) + 1 : 0;
      const items = rows.slice(start, start + (requestedIds.length || window.patrolFixture.historyPageSize));
      const has_more = start + items.length < rows.length;
      window.patrolFixture.historyRequests.push({ id, before });
      if (window.patrolFixture.holdHistory) await new Promise(resolve => window.patrolFixture.historyReleases.push(resolve));
      if (window.patrolFixture.failHistory) { window.patrolFixture.failHistory = false; return new Response("历史读取中断", { status: 503 }); }
      return json({ items, next_before: has_more ? items.at(-1).intent_id : null, has_more });
    }
    const body = JSON.parse(options.body);
    window.patrolFixture.submissions.push(body);
    loops.set(id, true);
    const rows = records.get(id) || [];
    let receipt = rows.find(row => row.submission_id === body.submission_id);
    if (!receipt) { receipt = { ...body, intent_id: body.submission_id, loop_id: `loop-${id}`, status: "accepted", created_at: new Date().toISOString() }; rows.push(receipt); records.set(id, rows); }
    localStorage.setItem(`patrol-fixture:${id}`, JSON.stringify(rows));
    if (window.patrolFixture.failReply) { window.patrolFixture.failReply = false; throw new Error("回执连接中断"); }
    if (window.patrolFixture.hold) await new Promise(resolve => window.patrolFixture.releases.push(resolve));
    return json(receipt);
  }
  const live = path.match(/\/agent-loops\/(loop-[^/]+)\/live$/);
  if (live) return json({ loop_id: live[1], last_sequence: sequence,
    loop: { entity_id: live[1], revision: 1, updated_sequence: Math.max(1, sequence), state: { status: "running", interaction_mode: "workspace_patrol", waiting_reason: null } },
    task_progress: { entity_id: live[1], revision: 1, updated_sequence: Math.max(1, sequence), state: { generation: 0, document: { items: [] } } } });
  const stream = path.match(/\/agent-loops\/(loop-[^/]+)\/live\/stream$/);
  if (stream) return new Response(new ReadableStream({ start(controller) { streams.set(stream[1], controller); options.signal?.addEventListener("abort", () => { streams.delete(stream[1]); controller.close(); }, { once: true }); } }), { headers: { "Content-Type": "text/event-stream" } });
  if (path.endsWith("/lineage") && path.startsWith(prefix)) {
    const snapshot = structuredClone(window.patrolFixture.lineage);
    if (window.patrolFixture.holdLineage) {
      window.patrolFixture.holdLineage = false;
      await new Promise(resolve => { window.patrolFixture.releaseLineage = resolve; });
    }
    return window.patrolFixture.lineageError ? new Response("关系读取失败", {status:503}) : json(snapshot);
  }
  if (window.patrolFixture.graphConversations) {
    const context = path.match(/\/contexts\/([^/]+)\/revisions$/);
    if (context) return json(window.patrolFixture.lineage.nodes.filter(node => node.context_id === context[1]).map(node => ({...node,current:window.patrolFixture.lineage.roots[node.context_id] === node.revision_id})));
    const revision = path.match(/\/context-revisions\/([^/]+)$/);
    if (revision) return json({revision:{sources:window.patrolFixture.lineage.edges.filter(edge => edge.target_revision_id === revision[1]).map(edge => ({source:{context_id:edge.source_context_id,revision_id:edge.source_revision_id}}))}});
  }
  const graphContext = path.match(/\/agent-loops\/[^/]+\/contexts\/([^/]+)\/conversation$/);
  if (graphContext && window.patrolFixture.graphConversations) {
    const id = graphContext[1], revision = url.searchParams.get("revision_id") || window.patrolFixture.lineage.roots[id];
    if (!window.patrolFixture.lineage.roots[id]) return new Response("Context 不属于当前 Loop", {status:404});
    const node = window.patrolFixture.lineage.nodes.find(node => node.revision_id === revision);
    return json({ revision:{revision_id:revision,generation:node?.generation}, messages:[{index:0,message:{role:"ai",content:"隔离版本检查 "+revision}}],total:1,has_more:false });
  }
  const observation = path.match(/\/agent-loops\/[^/]+\/observations\/([^/]+)$/);
  if (observation) {
    if (window.patrolFixture.observationError) return new Response("冻结读取暂不可用", { status: 503 });
    const section = url.searchParams.get("section");
    const historical = observation[1] === "observation-old";
    const summary = { observation_id: observation[1], generation: historical ? 1 : 2, round_number: observation[1].endsWith("2") ? 2 : 1, observation_hash: "frozen-hash", frozen_at: "2026-10-09T00:00:00Z", availability: "available", evidence_visible: false, previous_progress: { progress_id: "old-progress", item_count: 1, history_complete: false }, sources: { total: 1, complete: false }, lineage: { node_count: 1, complete: true }, goal_revision: 1, authority_revision: 1 };
    if (section === "versions") return json({ ...summary, items: [{ observation_id: "observation-old", generation: 1, frozen_at: summary.frozen_at }], total: 1, has_more: false });
    if (section === "patrol_history") {
      const next = url.searchParams.has("cursor");
      if (next && window.patrolFixture.patrolHistoryChanged) {
        window.patrolFixture.patrolHistoryChanged = false;
        return json({ detail: "Patrol 审计记录或权限已变化，请重新读取此版本" }, 409);
      }
      const item = historical
        ? { history_kind: "curator", state: "failed", worker_request_id: "historical-worker", failure: "历史 Curator 超时" }
        : next ? { history_kind: "curator", state: "analyzing", worker_request_id: "selected-worker", result_summary: "selected-curator-record" }
        : { history_kind: "phase", from_phase: "dispatching_curators", to_phase: "collecting_curators", summary: "收集当前版本 Curator", occurred_at: summary.frozen_at,
          wait_reason: "等待 selected-worker", wait_targets: [{ entity_type: "worker_request", entity_id: "selected-worker" }] };
      return json({ ...summary, patrol_session: { session_id: historical ? "historical-session" : "selected-session", observation_id: observation[1], phase: historical ? "failed" : "collecting_curators", status: historical ? "failed" : "active" },
        items: [item], total: historical ? 1 : 2, has_more: !historical && !next, next_cursor: !historical && !next ? "patrol-next" : null });
    }
    return json(section ? { ...summary, section, items: [{ source_id: "frozen-source", version: "original" }], total: 1, has_more: false } : summary);
  }
  if (path.endsWith("/task-progress")) return json({ current: { progress_id: "current-progress", generation: 2, document: { items: [] } }, selected: { progress_id: url.searchParams.get("progress_id") || "current-progress", generation: 2, document: { items: [] } }, history: [{ progress_id: "current-progress", generation: 2 }] });
  if (path === "/desktop/api/contexts/root/revisions") return json([{revision_id:"r1",generation:1,origin_kind:"bootstrap"},{revision_id:"r2",generation:2,origin_kind:"curation",current:true}]);
  if (/\/context-revisions\/r[12]$/.test(path)) return json({revision:{sources:[{source:{context_id:"source",revision_id:"source-r1"}}]}});
  if (path.endsWith("/contexts/root/conversation") && path.startsWith(prefix)) return json({ revision:{revision_id:url.searchParams.get("revision_id") || "r2"}, messages: [{ index: 0, message: { role: "ai", content: "Context 执行结果\n\n## 可读结果\n\n- 已完成\n\n| 文件 | 状态 |\n| --- | --- |\n| a.js | 完成 |\n\n[文件](file:///C:/work/a.js)\n\n[参考](https://example.com/context)\n\n```js\n\t  x < 2 && y\n```\n", tool_calls: [{name:"read_file",args:{path:"a.js"}}] } }], total: 1, has_more: false });
  if (path.endsWith("/control") && path.startsWith(prefix)) {
    if (window.patrolFixture.holdControl) await new Promise(resolve => { window.patrolFixture.releaseControl = resolve; });
    if (window.patrolFixture.controlError) return new Response("工作控制读取失败", { status: 503 });
    return json({ status: JSON.parse(options.body).command });
  }
  return base(input, options);
};
