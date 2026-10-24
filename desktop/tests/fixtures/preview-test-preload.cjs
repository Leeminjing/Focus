/* 本文件对外提供运行预览宿主验收的隔离 HTTP/SSE fixture。
 * 输入为生产页面的 Run GET/EventSource、Loop Live 与既有业务请求；输出为可控制角色、游标、Run 身份、断线与迟到帧的测试事件。
 * 工作流为复用原 Patrol fixture，仅扩展运行快照与传输控制；网络替身只存在于本测试 preload，不进入生产入口或真实后端验收。
 * 示例：previewFixture.seed('workspace', 8); previewFixture.runState(runId, 'running'); previewFixture.event(runId, 'tokens', {content:'真实协议形状的测试正文',message_id:'m1'})。
 */
require("./workspace-patrol-test-preload.cjs");
const baseFetch = window.fetch;
const json = value => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
const fixture = window.previewFixture = { workspaces: {}, runs: {}, sources: [], frames: {}, sequence: {}, gets: [], held: {}, hold: new Set(), denied: new Set(), version: 1 };
fixture.seed = (workspaceId, count, status = "pending") => {
  const contexts = {}, runs = {}, lineage = { roots: {}, nodes: [], edges: [], complete: true };
  for (let i = 0; i < count; i++) {
    const id = `${workspaceId}-ctx-${i}`, runId = `${workspaceId}-run-${i}`, revisionId = `${workspaceId}-revision-${i}`;
    const context = { context_id: id, title: i ? `工作线 ${i}` : "你好", status: "active", current_revision_id: revisionId };
    contexts[id] = { entity_id: id, revision: 1, updated_sequence: 1, state: context };
    fixture.runs[runId] = { run_id: runId, task_id: id, loop_id: `loop-${workspaceId}`, execution_thread_id: `thread-${id}`, thread_id: `thread-${id}`, message_id: `input-${runId}`, workspace_anchor: { workspace_id: workspaceId }, status, kind: "main" };
    runs[runId] = { entity_id: runId, revision: 1, updated_sequence: 1, state: { context_id: id, run_id: runId, status } };
    lineage.roots[id] = revisionId;
    lineage.nodes.push({ context_id: id, revision_id: revisionId, generation: 1 });
    if (i) lineage.edges.push({ source_context_id: `${workspaceId}-ctx-0`, source_revision_id: `${workspaceId}-revision-0`, target_context_id: id, target_revision_id: revisionId });
  }
  fixture.workspaces[workspaceId] = { contexts, runs, lineage };
};
fixture.runState = (runId, status) => {
  const run = fixture.runs[runId]; run.status = status;
  const workspace = fixture.workspaces[run.workspace_anchor.workspace_id], previous = workspace.runs[runId];
  const state = { ...previous?.state, context_id: run.task_id, run_id: runId, status };
  const revision = (previous?.revision || 0) + 1;
  workspace.runs[runId] = { entity_id: runId, revision, updated_sequence: ++fixture.version, state };
  window.patrolFixture.emit(run.loop_id, "run", runId, revision, state);
};
fixture.replaceRun = (previousId, newId, status = "running") => {
  fixture.runState(previousId, "success");
  fixture.runs[newId] = { ...fixture.runs[previousId], run_id: newId, message_id: `input-${newId}`, status };
  fixture.runState(newId, status);
};
fixture.count = runId => fixture.sources.filter(source => !source.closed && (!runId || source.runId === runId)).length;
fixture.event = (runId, type, data, options = {}) => {
  const run = fixture.runs[runId];
  const id = String(options.id ?? (fixture.sequence[runId] = (fixture.sequence[runId] || 0) + 1));
  const envelope = type === "end" ? { run_id: runId, ...data } : { workspace_id: run.workspace_anchor.workspace_id, thread_id: run.execution_thread_id, run_id: runId, agent_id: `main:${run.task_id}`, data };
  const event = {data:JSON.stringify(envelope),lastEventId:id};
  (fixture.frames[runId] ||= []).push({type,event});
  if (fixture.frames[runId].length > 512) fixture.frames[runId].shift();
  for (const source of fixture.sources.filter(item => item.runId === runId && (!item.closed || options.late))) source.dispatch(type, event);
};
fixture.disconnect = runId => fixture.sources.filter(source => source.runId === runId && !source.closed).forEach(source => source.dispatch("error", {}));
window.EventSource = class {
  constructor(url) { this.url = url; this.runId = new URL(url).pathname.split("/").at(-2); this.listeners = new Map(); this.closed = false; fixture.sources.push(this); queueMicrotask(() => { this.dispatch("open", {}); for (const frame of fixture.frames[this.runId] || []) if (!this.closed) this.dispatch(frame.type,frame.event); }); }
  addEventListener(type, callback) { if (!this.listeners.has(type)) this.listeners.set(type, []); this.listeners.get(type).push(callback); }
  dispatch(type, event) { for (const callback of this.listeners.get(type) || []) callback(event); }
  close() { this.closed = true; }
};
fixture.seed("workspace", 8);
fixture.seed("other", 2, "running");
window.fetch = async (input, options = {}) => {
  const url = new URL(String(input), "http://focus.test"), pathname = url.pathname;
  const runMatch = pathname.match(/^\/desktop\/api\/runs\/([^/]+)$/);
  if (runMatch) {
    fixture.gets.push(runMatch[1]);
    if (fixture.hold.has(runMatch[1])) await new Promise(resolve => { fixture.held[runMatch[1]] = resolve; });
    if (fixture.denied.has(runMatch[1])) return new Response("无权读取当前 Run", { status: 403 });
    return fixture.runs[runMatch[1]] ? json(fixture.runs[runMatch[1]]) : new Response("Run 不存在", { status: 404 });
  }
  const workspaceMatch = pathname.match(/\/agent-loops\/workspace\/([^/]+)$/);
  if (workspaceMatch && fixture.workspaces[workspaceMatch[1]]) return json({ workspace_id: workspaceMatch[1], loop_id: `loop-${workspaceMatch[1]}` });
  const treeMatch = pathname.match(/^\/desktop\/api\/workspaces\/([^/]+)\/contexts\/tree$/);
  if (treeMatch && fixture.workspaces[treeMatch[1]]) return json(Object.values(fixture.workspaces[treeMatch[1]].contexts).map(entity => ({ ...entity.state, depth: 0, projection_status: "root", parents: [] })));
  const loopMatch = pathname.match(/\/agent-loops\/loop-([^/]+)\/(lineage|console|live)$/);
  if (loopMatch && fixture.workspaces[loopMatch[1]]) {
    const workspace = fixture.workspaces[loopMatch[1]];
    if (loopMatch[2] === "lineage") { window.patrolFixture.lineage = workspace.lineage; window.patrolFixture.graphConversations = true; return json(workspace.lineage); }
    if (loopMatch[2] === "console") return json({ loop_id: `loop-${loopMatch[1]}`, nodes: Object.values(workspace.contexts).map(entity => entity.state), edges: [] });
    const response = await baseFetch(input, options);
    return json({ ...await response.json(), contexts: workspace.contexts, runs: workspace.runs });
  }
  const taskMatch = pathname.match(/^\/desktop\/api\/tasks\/([^/]+)$/);
  const allContexts = Object.values(fixture.workspaces).flatMap(workspace => Object.values(workspace.contexts));
  if (taskMatch && allContexts.some(entity => entity.entity_id === taskMatch[1])) {
    const active = Object.values(fixture.runs).find(run => run.task_id === taskMatch[1] && run.status === "running");
    return json({ task_id: taskMatch[1], messages: [], ui_state: {}, active_run: active || null, context: null });
  }
  if (pathname === "/desktop/api/bootstrap") {
    const result = await (await baseFetch(input, options)).json();
    for (const [workspaceId, workspace] of Object.entries(fixture.workspaces)) for (const entity of Object.values(workspace.contexts)) result.tasks.push({ task_id: entity.entity_id, title: entity.state.title, workspace_id: workspaceId, workspace_name: workspaceId, workspace_path: `C:/${workspaceId}`, thread_id: `thread-${entity.entity_id}`, active_run: null });
    return json(result);
  }
  return baseFetch(input, options);
};
