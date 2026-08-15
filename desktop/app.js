"use strict";

// markdown-it 单例(渲染器无状态):完成态助手消息的 md 渲染引擎。
// html:false 转义原始 HTML,危险协议链接由默认 validateLink 拒绝;vendor 文件先行加载。
const mdRenderer = window.markdownit({ html: false, linkify: true });

const runtime = window.focusDesktop?.runtime?.() || {
  apiBase: location.protocol === "file:" ? "http://127.0.0.1:8765" : location.origin,
  session: "focus-dev-session",
};

const state = {
  view: "focus",
  tasks: [],
  activeTaskId: null,
  details: new Map(),
  drafts: new Map(),
  materials: new Map(),
  agents: new Map(),
  skillCatalogs: new Map(),
  contextTrees: new Map(),
  contextDraft: null,
  pickerActive: { main: 0, draft: 0 },
  equipment: { models: [], tools: [], skills: [], permissions: [] },
  soldierArmed: false,
  openMaterial: null,
  openDraftSection: "history",
  agentDialog: { agentId: null, messages: [], busy: false },
  saveTimer: null,
  statusTimer: null,
  deploying: false,
  mainInterrupting: false,
  streams: new Map(),
  streamBuffers: new Map(),
  streamFrames: new Map(),
  commitment: {
    taskId: null,
    stage: 0,
    review: null,
    recovery: null,
    recoveryRestored: false,
    busy: false,
    tracePanel: null,
    traceStartedAt: null,
    traceTimer: null,
    traceStreams: new Map(),
    traceItems: new Map(),
    messageIds: new Set(),
    terminalStatus: null,
    terminalError: null,
    handoffStarted: false,
  },
};

const app = document.querySelector("#app");
const statusNode = document.querySelector("#globalStatus");
const dialog = document.querySelector("#taskDialog");
const agentDialog = document.querySelector("#agentDialog");
const skillPicker = window.FocusSkillPicker;
const contextEditor = window.FocusContextEditor;
let contextUiSequence = 0;
let contextPointerDrag = null;
let contextUndoTimer = null;

function nextContextUiKey() {
  contextUiSequence += 1;
  return `context-ui-${contextUiSequence}`;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-Focus-Session", runtime.session);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(`${runtime.apiBase}${path}`, { ...options, headers });
  if (!response.ok) {
    let detail;
    try { detail = (await response.json()).detail; } catch { detail = await response.text(); }
    const error = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    error.status = response.status;
    error.detail = detail;
    throw error;
  }
  if (response.status === 204) return null;
  return response.json();
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function renderAssistantContent(value) {
  // markdown-it 渲染完成态助手消息(html:false 转义 LLM 输出中的原始 HTML;
  // 危险协议链接被默认 validateLink 拒绝)。vendor 文件由 index.html 先行加载。
  return `<div class="message-rich">${mdRenderer.render(String(value))}</div>`;
}

function setStatus(text, isError = false) {
  clearTimeout(state.statusTimer);
  statusNode.textContent = text;
  statusNode.classList.toggle("danger", isError);
  if (text && !isError && text.includes("已保存")) {
    state.statusTimer = setTimeout(() => {
      if (statusNode.textContent === text) statusNode.textContent = "";
    }, 1400);
  }
}

function activeTask() { return state.tasks.find(task => task.task_id === state.activeTaskId); }

async function bootstrap() {
  setStatus("正在连接…");
  try {
    const data = await api("/desktop/api/bootstrap");
    state.tasks = data.tasks;
    state.equipment = data.equipment;
    await hydrateContextTrees();
    state.activeTaskId ||= state.tasks[0]?.task_id || null;
    setStatus("");
    await hydrateActive();
    render();
  } catch (error) {
    setStatus(error.message, true);
    app.innerHTML = `<section class="empty-state"><h1>桌面服务未就绪</h1><p>${escapeHtml(error.message)}</p><button class="primary" data-action="reload">重试</button></section>`;
  }
}

async function hydrateActive() {
  if (!state.activeTaskId) return;
  const [detail, materials, agents, catalog] = await Promise.all([
    api(`/desktop/api/tasks/${state.activeTaskId}`),
    api(`/desktop/api/tasks/${state.activeTaskId}/materials`),
    api(`/desktop/api/tasks/${state.activeTaskId}/agents`),
    api(`/desktop/api/tasks/${state.activeTaskId}/skills`),
  ]);
  state.details.set(state.activeTaskId, detail);
  reconcileCommitmentRecovery(detail);
  state.materials.set(state.activeTaskId, materials);
  state.agents.set(state.activeTaskId, agents);
  state.skillCatalogs.set(state.activeTaskId, catalog.skills);
  if (detail.active_run?.status === "pending" || detail.active_run?.status === "running") {
    listenToRun(detail.active_run);
  }
}

function render() {
  document.body.dataset.view = state.view;
  document.querySelector('[data-action="show-map"]').hidden = !state.tasks.length || state.view !== "focus";
  if (!state.tasks.length) {
    app.replaceChildren(document.querySelector("#emptyTemplate").content.cloneNode(true));
    return;
  }
  if (state.view === "focus") renderFocus();
  else if (state.view === "map") renderMap();
  else if (state.view === "draft") renderDraft();
  else renderContextEditor();
}

function normalizeSkillNames(value) {
  return Array.isArray(value) ? [...new Set(value.filter(name => typeof name === "string"))] : [];
}

function selectedSkills(kind) {
  if (kind === "draft") {
    return normalizeSkillNames(state.drafts.get(state.activeTaskId)?.equipment?.skills);
  }
  return normalizeSkillNames(state.details.get(state.activeTaskId)?.ui_state?.skills);
}

function renderSkillPicker(kind, textarea) {
  const selected = selectedSkills(kind);
  const listId = `${kind}SkillList`;
  const tags = selected.map(name => `<span class="skill-tag">${escapeHtml(name)}<button type="button" data-action="remove-skill" data-picker-kind="${kind}" data-skill-name="${escapeHtml(name)}" aria-label="Remove ${escapeHtml(name)}">×</button></span>`).join("");
  return `<div class="skill-picker-shell ${kind === "draft" ? "draft-skill-picker" : ""}" data-skill-picker="${kind}">
    <div class="skill-tags" aria-label="Selected skills">${tags}</div>
    ${textarea.replace(">", ` data-skill-input="${kind}" aria-controls="${listId}" aria-expanded="false">`)}
    <div class="skill-menu" id="${listId}" role="listbox" aria-label="Skills" hidden></div>
  </div>`;
}

function pickerMatches(input) {
  const query = skillPicker.queryFromInput(input.value);
  if (query === null) return null;
  const kind = input.dataset.skillInput;
  return skillPicker.filterSkills(
    state.skillCatalogs.get(state.activeTaskId) || [], query, selectedSkills(kind)
  );
}

function commitCommandVisible(query) {
  return !query || "commit".startsWith(query.toLowerCase()) || query.toLowerCase().startsWith("commit");
}

function updateSkillMenu(input, reset = false) {
  const kind = input.dataset.skillInput;
  const menu = document.querySelector(`#${kind}SkillList`);
  const matches = pickerMatches(input);
  if (!menu || matches === null) {
    if (menu) menu.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    return [];
  }
  if (reset) state.pickerActive[kind] = 0;
  const query = skillPicker.queryFromInput(input.value) || "";
  const commitCount = commitCommandVisible(query) ? 1 : 0;
  const total = commitCount + matches.length;
  state.pickerActive[kind] = total
    ? Math.min(state.pickerActive[kind], total - 1)
    : -1;
  const commitItem = commitCount
    ? `<button type="button" id="${kind}SkillOption0" class="skill-option commit-option ${state.pickerActive[kind] === 0 ? "is-active" : ""}" role="option" aria-selected="${state.pickerActive[kind] === 0}" data-action="select-commit" data-picker-kind="${kind}"><span class="skill-option-name">/commit</span><span class="skill-option-description">进入九阶段承诺流程</span></button>`
    : "";
  const options = matches.map((skill, index) => {
    const optionIndex = index + commitCount;
    return `<button type="button" id="${kind}SkillOption${optionIndex}" class="skill-option ${optionIndex === state.pickerActive[kind] ? "is-active" : ""}" role="option" aria-selected="${optionIndex === state.pickerActive[kind]}" data-action="select-skill" data-picker-kind="${kind}" data-skill-name="${escapeHtml(skill.name)}"><span class="skill-option-name">${escapeHtml(skill.name)}</span><span class="skill-option-description">${escapeHtml(skill.description)}</span></button>`;
  }).join("");
  menu.innerHTML = (commitItem + options) || `<div class="skill-empty">No matching skills</div>`;
  menu.hidden = false;
  input.setAttribute("aria-expanded", "true");
  if (state.pickerActive[kind] >= 0) {
    input.setAttribute("aria-activedescendant", `${kind}SkillOption${state.pickerActive[kind]}`);
    menu.querySelector(".is-active")?.scrollIntoView({ block: "nearest" });
  } else {
    input.removeAttribute("aria-activedescendant");
  }
  return matches;
}

function selectCommitCommand(kind) {
  const input = document.querySelector(`[data-skill-input="${kind}"]`);
  if (!input) return;
  input.value = "/commit ";
  input.focus();
  const menu = document.querySelector(`#${kind}SkillList`);
  if (menu) menu.hidden = true;
  input.setAttribute("aria-expanded", "false");
  input.removeAttribute("aria-activedescendant");
  if (kind === "draft") scheduleDraftSave();
}

function setPickerSelection(kind, names, clearQuery = false) {
  if (kind === "draft") {
    const draft = syncDraftFromDom();
    draft.equipment.skills = names;
    if (clearQuery) draft.final_human_message = "";
    renderDraft();
    scheduleDraftSave();
  } else {
    const detail = state.details.get(state.activeTaskId);
    detail.ui_state ||= {};
    detail.ui_state.input = clearQuery ? "" : (document.querySelector("#mainInput")?.value || "");
    detail.ui_state.skills = names;
    detail.ui_state.scrollTop = document.querySelector("#conversation")?.scrollTop || 0;
    renderFocus();
    persistFocusState();
  }
  requestAnimationFrame(() => document.querySelector(`[data-skill-input="${kind}"]`)?.focus());
}

function selectSkill(kind, name) {
  setPickerSelection(kind, skillPicker.addSelection(selectedSkills(kind), name), true);
}

function removeSkill(kind, name) {
  setPickerSelection(kind, skillPicker.removeSelection(selectedSkills(kind), name));
}

function renderInterruptButton(detail) {
  // 主 Agent 有 pending/running 的活动运行时才渲染中断按钮（active_run 由后端只查 main 运行）
  const run = detail?.active_run;
  if (!run || !["pending", "running"].includes(run.status)) return "";
  return `<button class="text-button danger" data-action="interrupt-main-run" data-run-id="${run.run_id}"${state.mainInterrupting ? " disabled" : ""}>中断</button>`;
}

function renderContextRail(task) {
  const tasks = contextEditor.contextFamilyTasks(state.tasks, state.contextTrees, task.task_id);
  const tree = state.contextTrees.get(task.workspace_id) || [];
  const nodes = new Map(tree.map(node => [node.context_id, node]));
  const cards = tasks.map(item => {
    const node = nodes.get(item.task_id);
    const depth = Number(node?.depth || 0);
    const projectionStatus = node?.projection_status || "root";
    const blocked = !["root", "valid", "repaired", "approved"].includes(projectionStatus);
    const cacheRate = Number.isFinite(node?.cache_hit_rate)
      ? `${Math.round(node.cache_hit_rate * 100)}%`
      : "—";
    const otherParents = (node?.parents || []).slice(1).map(parent =>
      state.tasks.find(candidate => candidate.task_id === parent.context_id)?.title || parent.context_id
    ).join("、");
    return `<div class="context-rail-item${node?.editable ? " is-editable" : ""}" style="--context-depth:${depth}">
      <button type="button" class="context-rail-card${item.task_id === task.task_id ? " is-current" : ""}${blocked ? " is-blocked" : ""}" data-action="context-rail-card" data-task-id="${escapeHtml(item.task_id)}" aria-current="${item.task_id === task.task_id ? "true" : "false"}">
        <span class="context-rail-title">${escapeHtml(item.title)}</span>
        <span class="context-rail-meta">${depth ? "派生 Context" : "根 Context"} · ${escapeHtml(item.task_id.slice(0, 8))}${blocked ? ` · ${escapeHtml(projectionStatus)}` : ""} · 缓存 ${cacheRate}</span>
        ${otherParents ? `<span class="context-rail-parents">另含：${escapeHtml(otherParents)}</span>` : ""}
      </button>
      ${node?.editable ? `<button type="button" class="context-rail-edit" data-action="edit-context-definition" data-context-id="${escapeHtml(item.task_id)}">编辑</button>` : ""}
    </div>`;
  }).join("");
  return `<aside class="context-rail" aria-label="Context 树">
    <header class="context-rail-heading"><strong>Contexts</strong><span>${tasks.length}</span></header>
    <nav class="context-rail-list" aria-label="当前聊天派生的 Context">
      ${cards}
      <button type="button" class="context-rail-add" data-action="derive-context">新增 Context</button>
    </nav>
  </aside>`;
}

function renderFocus() {
  const task = activeTask();
  const detail = state.details.get(task.task_id) || { messages: [] };
  const projectionStatus = detail.context?.projection_status || "root";
  const projectionBlocked = !["root", "valid", "repaired", "approved"].includes(projectionStatus);
  const contextBlock = projectionBlocked
    ? `<section class="context-block-banner"><strong>该 Context 尚未获得安全执行投影</strong><span>${escapeHtml(projectionStatus)}</span><button class="primary" data-action="resume-context-decision">查看并决断</button></section>`
    : "";
  const materials = state.materials.get(task.task_id) || [];
  const previousConversation = app.dataset.taskId === task.task_id ? document.querySelector("#conversation") : null;
  const previousRail = document.querySelector(".context-rail-list");
  const previousRailScrollTop = previousRail?.scrollTop;
  const wasPinned = previousConversation && previousConversation.scrollHeight - previousConversation.scrollTop - previousConversation.clientHeight < 80;
  const previousScrollTop = previousConversation?.scrollTop;
  app.innerHTML = `
    <section class="focus-shell">
      <section class="focus-view" data-task-id="${task.task_id}">
        ${contextBlock}
        <div class="commitment-progress" id="commitmentProgress" hidden>
          <div class="progress-heading"><strong>任务合同</strong><span id="progressLabel"></span></div>
          <ol id="progressSteps"></ol>
        </div>
        <div class="conversation" id="conversation">
          ${renderConversation(detail, task)}
        </div>
        <div class="focus-bottom">
          <div class="composer">
            ${renderSkillPicker("main", `<textarea id="mainInput" aria-label="任务输入" placeholder="继续输入任务…">${escapeHtml(detail.ui_state?.input || "")}</textarea>`)}
            <div class="composer-actions"><label class="attach-button">添加文件<input id="fileInput" type="file" hidden></label>${renderInterruptButton(detail)}<button class="send-button" data-action="send-main">发送</button></div>
          </div>
          <section class="materials ${materials.length ? "" : "is-empty"}">${materials.length ? materials.map(renderMaterial).join("") : `<div class="materials-empty">暂无材料</div>`}</section>
          <div class="agents-strip">${renderAgentStrip(task.task_id)}</div>
        </div>
      </section>
      ${renderContextRail(task)}
    </section>`;
  app.dataset.taskId = task.task_id;
  const conversation = document.querySelector("#conversation");
  mountCommitmentRecovery(detail);
  restoreCommitmentPanels(conversation);
  const commitmentBlocked = activeTaskHasCommitmentLock() || projectionBlocked;
  const mainInput = document.querySelector("#mainInput");
  const sendButton = document.querySelector('[data-action="send-main"]');
  if (mainInput) mainInput.disabled = commitmentBlocked;
  if (sendButton) sendButton.disabled = commitmentBlocked;
  conversation.scrollTop = previousConversation
    ? (wasPinned ? conversation.scrollHeight : previousScrollTop)
    : (detail.ui_state?.scrollTop ?? conversation.scrollHeight);
  const rail = document.querySelector(".context-rail-list");
  if (previousRailScrollTop != null) rail.scrollTop = previousRailScrollTop;
  if (!previousConversation) requestAnimationFrame(() => rail.querySelector('[aria-current="true"]')?.scrollIntoView({ block: "nearest" }));
}

function renderMessage(message) {
  const baseRole = { human: "你", user: "你", ai: "助手", assistant: "助手", system: "System", tool: "Tool" }[message.role] || message.role;
  const role = message.role === "tool" && message.name ? `${baseRole} · ${message.name}` : baseRole;
  const kind = { human: "human", user: "human", ai: "ai", assistant: "ai", system: "system", tool: "tool" }[message.role] || "system";
  const content = typeof message.content === "string" ? message.content : JSON.stringify(message.content, null, 2);
  const calls = Array.isArray(message.tool_calls) ? message.tool_calls : [];
  const counts = new Map();
  calls.forEach(call => {
    const name = String(call?.name || "tool");
    counts.set(name, (counts.get(name) || 0) + 1);
  });
  const callSummary = [...counts.entries()]
    .map(([name, count]) => `<code>${escapeHtml(name)}${count > 1 ? ` ×${count}` : ""}</code>`)
    .join("、");
  const toolProgress = callSummary
    ? `<div class="tool-call-progress">正在调用：${callSummary}</div>`
    : "";
  const renderedContent = kind === "ai" ? renderAssistantContent(content) : escapeHtml(content);
  const rendered = `${renderedContent}${toolProgress}`;
  return `<article class="message ${kind}"><span class="message-role">${escapeHtml(role)}</span><div class="message-content">${rendered}</div></article>`;
}

function renderConversation(detail, task) {
  const messages = detail.messages?.length
    ? detail.messages.map(renderMessage).join("")
    : `<div class="message"><span class="message-role">Focus</span><div class="message-content">${escapeHtml(task.workspace_path)}<br><span class="muted">这是该工作区与线程的 Page 1。输入任务即可开始。</span></div></div>`;
  const streaming = [...state.streamBuffers.entries()]
    .filter(([, buffer]) => buffer.taskId === task.task_id && buffer.text)
    .map(([runId, buffer]) => `<article class="message ai streaming" data-stream-run="${runId}"><span class="message-role">助手</span><div class="message-content">${escapeHtml(buffer.text.replace(/\n{2,}/g, "\n"))}</div></article>`)
    .join("");
  return messages + streaming;
}

function replaceConversation(task, messages) {
  const detail = state.details.get(task.task_id) || {};
  detail.messages = messages;
  state.details.set(task.task_id, detail);
  if (state.view !== "focus" || state.activeTaskId !== task.task_id) return;
  const conversation = document.querySelector("#conversation");
  if (!conversation) return;
  const pinned = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 80;
  conversation.innerHTML = renderConversation(detail, task);
  restoreCommitmentPanels(conversation);
  if (pinned) conversation.scrollTop = conversation.scrollHeight;
}

function appendToken(envelope) {
  const task = state.tasks.find(item => item.thread_id === envelope.thread_id && item.workspace_id === envelope.workspace_id);
  const content = envelope.data?.content;
  const messageId = envelope.data?.message_id;
  // 防御：承诺子图冒泡消息（commitment-stage-*）只进轨迹面板，不进 lead 对话流
  if (typeof messageId === "string" && messageId.startsWith("commitment-stage-")) return;
  if (!task || !content || !envelope.agent_id.startsWith("main:")) return;
  beginLeadExecution(task.task_id);
  const buffer = state.streamBuffers.get(envelope.run_id) || { taskId: task.task_id, text: "" };
  buffer.text += content;
  state.streamBuffers.set(envelope.run_id, buffer);
  if (state.streamFrames.has(envelope.run_id)) return;
  state.streamFrames.set(envelope.run_id, requestAnimationFrame(() => {
    state.streamFrames.delete(envelope.run_id);
    if (state.view !== "focus" || state.activeTaskId !== task.task_id) return;
    const conversation = document.querySelector("#conversation");
    if (!conversation) return;
    const pinned = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 80;
    let article = conversation.querySelector(`[data-stream-run="${envelope.run_id}"]`);
    if (!article) {
      article = document.createElement("article");
      article.className = "message ai streaming";
      article.dataset.streamRun = envelope.run_id;
      article.innerHTML = '<span class="message-role">助手</span><div class="message-content"></div>';
      conversation.append(article);
    }
    article.querySelector(".message-content").textContent = buffer.text.replace(/\n{2,}/g, "\n");
    if (pinned) conversation.scrollTop = conversation.scrollHeight;
  }));
}

function clearStreamBuffer(runId) {
  const frame = state.streamFrames.get(runId);
  if (frame) cancelAnimationFrame(frame);
  state.streamFrames.delete(runId);
  state.streamBuffers.delete(runId);
}

function renderMaterial(material) {
  const open = state.openMaterial === material.material_id;
  return `<article class="material-row" data-material-id="${material.material_id}">
    <button class="material-summary" data-action="toggle-material">
      <span class="material-name">${escapeHtml(material.relative_path)}</span>
      <span class="material-meta">${material.reading_mode === "full" ? "完整阅读" : "粗略阅读"} · ${material.instruction_mode === "strict" ? "严格遵守" : "仅供参考"} · ${material.retention === "irreplaceable" ? "不可遗失" : "可移除"}</span>
      ${material.needs_confirmation ? `<span class="danger tiny">检测到外部删除，文件已恢复</span>` : ""}
      <span class="material-toggle">${open ? "收起" : "规则"}</span>
    </button>
    ${open ? `<div class="material-editor">
      <label>阅读方式<select data-field="reading_mode"><option value="full" ${material.reading_mode === "full" ? "selected" : ""}>完整阅读</option><option value="rough" ${material.reading_mode === "rough" ? "selected" : ""}>粗略阅读</option></select></label>
      <label>约束<select data-field="instruction_mode"><option value="reference" ${material.instruction_mode === "reference" ? "selected" : ""}>仅供参考</option><option value="strict" ${material.instruction_mode === "strict" ? "selected" : ""}>严格遵守</option></select></label>
      <label>文件规则<select data-field="retention"><option value="removable" ${material.retention === "removable" ? "selected" : ""}>可移除</option><option value="irreplaceable" ${material.retention === "irreplaceable" ? "selected" : ""}>不可遗失</option></select></label>
      <span class="material-actions"><button class="text-button" data-action="save-material">保存</button>
      ${material.retention === "irreplaceable" ? `<button class="text-button" data-action="clear-material">置空</button><button class="text-button" data-action="load-versions">版本</button>` : `<button class="text-button danger" data-action="delete-material">删除</button>`}</span>
      <ul class="version-list" data-versions></ul>
    </div>` : ""}
  </article>`;
}

function renderAgentStrip(taskId) {
  const agents = state.agents.get(taskId) || [];
  const warning = agents.some(agent => agent.permissions?.some(permission => permission === "write" || permission === "host_command"))
    ? `<span class="write-warning">共享宿主机写入：并发冲突采用最后写入者结果</span>` : "";
  return warning + agents.map(agent => `<button class="agent-chip" data-action="agent-details" data-agent-id="${agent.agent_id}">小兵 ${agent.agent_id.slice(0, 5)} · ${agent.latest_run?.status || "ready"}</button>`).join("");
}

function agentFromState(agentId) {
  return (state.agents.get(state.activeTaskId) || []).find(item => item.agent_id === agentId);
}

async function openAgentDetails(agentId) {
  state.agentDialog = { agentId, messages: [], busy: false };
  renderAgentDialog();
  agentDialog.showModal();
  await refreshAgentDetails();
}

async function refreshAgentDetails() {
  if (!state.agentDialog.agentId || state.agentDialog.busy) return;
  state.agentDialog.busy = true;
  renderAgentDialog();
  try {
    const [history, agents] = await Promise.all([
      api(`/desktop/api/agents/${state.agentDialog.agentId}/history`),
      api(`/desktop/api/tasks/${state.activeTaskId}/agents`),
    ]);
    state.agents.set(state.activeTaskId, agents);
    state.agentDialog.messages = history;
  } catch (error) { setStatus(error.message, true); }
  finally { state.agentDialog.busy = false; renderAgentDialog(); }
}

function renderAgentDialog() {
  const agent = agentFromState(state.agentDialog.agentId);
  document.querySelector("#agentDialogMeta").textContent = agent
    ? `小兵 ${agent.agent_id} · ${agent.latest_run?.status || "ready"} · ${agent.checkpoint_ns}`
    : "";
  const history = document.querySelector("#agentHistory");
  if (state.agentDialog.busy) { history.innerHTML = `<p class="muted">加载中…</p>`; return; }
  history.innerHTML = state.agentDialog.messages.length
    ? state.agentDialog.messages.map(renderMessage).join("")
    : `<p class="muted">暂无消息记录（该小兵尚未产生已提交的 checkpoint）</p>`;
}

async function retryAgentDetails() {
  try {
    const run = await api(`/desktop/api/agents/${state.agentDialog.agentId}/retry`, { method: "POST" });
    listenToRun(run);
    setStatus("已发起小兵重试");
    await refreshAgentDetails();
  } catch (error) { setStatus(error.message, true); }
}

async function continueAgentDetails() {
  const input = document.querySelector("#agentContinueInput");
  const message = input.value.trim();
  if (!message) return;
  try {
    const run = await api(`/desktop/api/agents/${state.agentDialog.agentId}/continue`, { method: "POST", body: JSON.stringify({ message }) });
    listenToRun(run);
    input.value = "";
    setStatus("已发起小兵继续对话");
    await refreshAgentDetails();
  } catch (error) { setStatus(error.message, true); }
}

async function cancelAgentDetails() {
  const agent = agentFromState(state.agentDialog.agentId);
  if (!agent?.latest_run) return setStatus("该小兵尚无运行可取消", true);
  try {
    await api(`/desktop/api/runs/${agent.latest_run.run_id}/cancel`, { method: "POST" });
    setStatus("已请求取消运行");
    await refreshAgentDetails();
  } catch (error) { setStatus(error.message, true); }
}

async function interruptMainRun() {
  const detail = state.details.get(state.activeTaskId);
  const run = detail?.active_run;
  if (!run || !["pending", "running"].includes(run.status)) return setStatus("当前没有可中断的主 Agent 运行", true);
  if (state.mainInterrupting) return; // busy 防连点
  state.mainInterrupting = true;
  const button = document.querySelector('[data-action="interrupt-main-run"]');
  if (button) button.disabled = true;
  try {
    const payload = await api(`/desktop/api/runs/${run.run_id}/cancel`, { method: "POST" });
    // cancel 受理即置 interrupted（DB 立即同步）；仅当竞态窗口内 run 自己先到
    // success/error 等终态（非 interrupted）时才提示"已结束"
    if (payload && payload.status === "interrupted") {
      setStatus("已请求中断主 Agent，正在停止…");
    } else if (payload && !["pending", "running"].includes(payload.status)) {
      setStatus("运行已结束，无需中断", true);
    } else {
      setStatus("已请求中断主 Agent，正在停止…");
    }
  } catch (error) {
    setStatus(error.status === 404 ? "运行不存在（可能已结束）" : error.message, true);
  } finally {
    state.mainInterrupting = false;
  }
}

async function debugAgentMenu(agentId) {
  // 保留的高级操作通道：原 prompt 交互在 Electron 中被禁用，调用即抛错，此处显式提示
  let choice;
  try { choice = prompt("输入操作：history / retry / continue / cancel", "history"); }
  catch { return setStatus("调试通道依赖 window.prompt，Electron 不支持，请使用详情面板操作", true); }
  if (choice === "history") alert(JSON.stringify(await api(`/desktop/api/agents/${agentId}/history`), null, 2));
  if (choice === "retry") listenToRun(await api(`/desktop/api/agents/${agentId}/retry`, { method: "POST" }));
  if (choice === "continue") { const message = prompt("继续对话内容"); if (message) listenToRun(await api(`/desktop/api/agents/${agentId}/continue`, { method: "POST", body: JSON.stringify({ message }) })); }
  if (choice === "cancel") { const agent = agentFromState(agentId); if (agent?.latest_run) await api(`/desktop/api/runs/${agent.latest_run.run_id}/cancel`, { method: "POST" }); }
}

function taskCards(draftMode = false) {
  const draft = state.drafts.get(state.activeTaskId);
  return contextEditor.orderTasksByTree(state.tasks, state.contextTrees).map(task => {
    const selected = draftMode && draft?.task_id === task.task_id;
    const status = task.active_run?.status;
    const tree = state.contextTrees.get(task.workspace_id) || [];
    const context = tree.find(item => item.context_id === task.task_id);
    const projectionStatus = context?.projection_status || "root";
    const parents = (context?.parents || []).slice(1).map(parent => state.tasks.find(item => item.task_id === parent.context_id)?.title || parent.context_id).join("、");
    const contextMeta = parents ? `<span class="context-parent-tags">另含：${escapeHtml(parents)}</span>` : "";
    const contextIdentity = context
      ? `<span class="context-identity">${context.depth ? "派生 Context" : "根 Context"} · ${escapeHtml(task.task_id.slice(0, 8))}${!["root", "valid", "repaired", "approved"].includes(projectionStatus) ? ` · ${escapeHtml(projectionStatus)}` : ""}</span>`
      : `<span class="context-identity">兼容任务 · ${escapeHtml(task.task_id.slice(0, 8))}</span>`;
    return `<button class="task-card ${draftMode && !selected ? "dimmed" : ""}" style="--context-depth:${context?.depth || 0}" data-task-id="${task.task_id}" data-action="task-card">
      <span class="task-title">${escapeHtml(task.title)}</span>
      ${contextIdentity}
      <span class="task-path">${escapeHtml(task.workspace_name)} · ${escapeHtml(task.thread_id)}</span>
      <span class="task-status ${status === "running" ? "running" : ""}">${status === "running" ? "主 Agent 运行中" : escapeHtml(task.workspace_path)}</span>
      ${contextMeta}
    </button>`;
  }).join("");
}

function renderMap() {
  app.innerHTML = `<section class="map-view">
    <div class="map-toolbar"><button class="soldier-source" draggable="true" aria-pressed="${state.soldierArmed}" data-action="arm-soldier">小兵 · 拖向任务</button></div>
    <div class="task-grid">${taskCards()}</div>
  </section>`;
}

async function hydrateContextTrees() {
  const workspaceIds = [...new Set(state.tasks.map(task => task.workspace_id))];
  const trees = await Promise.all(workspaceIds.map(async workspaceId => [
    workspaceId,
    await api(`/desktop/api/workspaces/${workspaceId}/contexts/tree`),
  ]));
  trees.forEach(([workspaceId, tree]) => state.contextTrees.set(workspaceId, tree));
}

async function openContextEditor(contextId) {
  const task = state.tasks.find(item => item.task_id === contextId);
  if (!task) return;
  setStatus("读取 Context checkpoint…");
  try {
    const snapshot = await api(`/desktop/api/contexts/${contextId}/snapshot`);
    if (!snapshot.checkpoint_id) throw new Error("该 Context 尚无可派生的已提交 checkpoint");
    state.contextDraft = {
      title: `${task.title} · 派生`,
      workspace_id: task.workspace_id,
      sources: [{ context_id: contextId, checkpoint_id: snapshot.checkpoint_id }],
      sourceSnapshots: [{ context_id: contextId, checkpoint_id: snapshot.checkpoint_id, messages: contextEditor.cloneMessages(snapshot.messages) }],
      activeSourceId: contextId,
      messages: [],
      uiKeys: [],
      expandedKey: null,
      undo: null,
      context: null,
    };
    state.view = "context";
    setStatus("");
    render();
  } catch (error) { setStatus(error.message, true); }
}

async function reopenContextDecision(contextId) {
  try {
    const detail = state.details.get(contextId) || await api(`/desktop/api/tasks/${contextId}`);
    const task = state.tasks.find(item => item.task_id === contextId);
    if (!detail.context || !task) throw new Error("Context 决断数据不存在");
    const sources = contextEditor.cloneMessages(detail.context.sources || []);
    const sourceSnapshots = await Promise.all(sources.map(async source => {
      const checkpoint = encodeURIComponent(source.checkpoint_id);
      const snapshot = await api(`/desktop/api/contexts/${source.context_id}/snapshot?checkpoint_id=${checkpoint}`);
      return { ...source, messages: contextEditor.cloneMessages(snapshot.messages) };
    }));
    const messages = contextEditor.cloneMessages(detail.context.authored_messages || []);
    state.contextDraft = {
      title: task.title,
      workspace_id: task.workspace_id,
      sources,
      sourceSnapshots,
      activeSourceId: sources[0]?.context_id || null,
      messages,
      uiKeys: contextEditor.createUiKeys(messages, nextContextUiKey),
      expandedKey: null,
      undo: null,
      context: detail.context,
    };
    state.view = "context";
    render();
  } catch (error) { setStatus(error.message, true); }
}

function syncContextDraft() {
  const draft = state.contextDraft;
  if (!draft) return null;
  const panel = document.querySelector(".context-editor-view");
  if (!panel) return draft;
  draft.title = panel.querySelector("[data-context-title]").value.trim();
  draft.messages = contextEditor.readMessages(panel, draft.messages);
  return draft;
}

function renderContextDecision(context) {
  if (!context) return "";
  const repairs = (context.repair_manifest || []).filter(item => item.kind !== "regex_flag");
  const repairList = repairs.length
    ? `<details open><summary>Focus 无损补齐了 ${repairs.length} 处协议结构</summary><pre>${escapeHtml(JSON.stringify(repairs, null, 2))}</pre></details>`
    : "";
  if (context.projection_status === "repaired" && !context.editable) {
    return `<section class="context-decision repaired"><h2>执行投影已安全生成</h2>${repairList}<button class="primary" data-action="open-derived-context">进入新 Context</button></section>`;
  }
  if (!["approval_required", "rejected", "initialization_failed"].includes(context.projection_status)) return repairList;
  const issues = (context.issues || []).map(issue => `<article class="context-issue">
    <strong>${escapeHtml(issue.reason)}</strong>
    <div class="context-diff"><div><span>原始片段</span><pre>${escapeHtml(JSON.stringify(issue.original, null, 2))}</pre></div><div><span>拟议投影</span><pre>${escapeHtml(JSON.stringify(issue.proposed, null, 2))}</pre></div></div>
  </article>`).join("");
  const actions = context.projection_status === "approval_required"
    ? `<button class="primary" data-action="accept-context-projection">接受本次降级</button><button class="text-button" data-action="edit-context-projection">返回编辑</button><button class="text-button danger" data-action="cancel-context-projection">取消发送</button>`
    : `<button class="text-button" data-action="edit-context-projection">返回编辑</button><button class="text-button danger" data-action="exit-context-editor">关闭</button>`;
  return `<section class="context-decision blocked"><h2>需要你的决断</h2><p>Focus 不会静默采用以下降级。</p>${repairList}${issues}<div class="dialog-actions">${actions}</div></section>`;
}

function contextDraftLocked(draft = state.contextDraft) {
  return Boolean(draft?.context && !draft.context.editable);
}

function ensureContextUiKeys(draft) {
  draft.uiKeys ||= [];
  while (draft.uiKeys.length < draft.messages.length) draft.uiKeys.push(nextContextUiKey());
  if (draft.uiKeys.length > draft.messages.length) draft.uiKeys.length = draft.messages.length;
  if (draft.expandedKey && !draft.uiKeys.includes(draft.expandedKey)) draft.expandedKey = null;
}

function contextSourcePanelMarkup(draft) {
  const created = Boolean(draft.context);
  const locked = contextDraftLocked(draft);
  const active = draft.sourceSnapshots?.find(source => source.context_id === draft.activeSourceId) || draft.sourceSnapshots?.[0];
  const tabs = draft.sources.map((source, index) => {
    const task = state.tasks.find(item => item.task_id === source.context_id);
    return `<span class="context-source-tab-wrap"><button type="button" class="context-source-tab${source.context_id === active?.context_id ? " is-active" : ""}" data-action="context-source-select" data-context-id="${escapeHtml(source.context_id)}">${escapeHtml(task?.title || source.context_id)}</button>${!created && draft.sources.length > 1 ? `<button type="button" class="context-source-remove" data-action="remove-context-source" data-source-index="${index}" aria-label="移除此来源">×</button>` : ""}</span>`;
  }).join("");
  const sourceIds = new Set(draft.sources.map(source => source.context_id));
  const tree = state.contextTrees.get(draft.workspace_id) || [];
  const candidates = tree.filter(item => !sourceIds.has(item.context_id)).map(item => {
    const task = state.tasks.find(candidate => candidate.task_id === item.context_id);
    return `<button class="context-source-candidate" style="--context-depth:${item.depth}" data-action="add-context-source" data-context-id="${escapeHtml(item.context_id)}">${escapeHtml(task?.title || item.title)}</button>`;
  }).join("") || '<span class="muted tiny">没有其他可加入的 Context</span>';
  return `<header class="context-source-heading"><div><span class="review-kicker">SOURCE</span><h2>已有消息</h2></div><span class="muted tiny">拖到右侧</span></header>
    <div class="context-source-tabs" role="tablist" aria-label="来源 Context">${tabs}</div>
    <div class="context-source-message-list">${contextEditor.renderSourceMessages(active?.messages || [], active?.context_id || "", locked)}</div>
    <details class="context-source-add"${created ? " hidden" : ""}><summary>加入其他来源</summary><div class="context-source-tree">${candidates}</div></details>`;
}

function renderContextSourcePanel() {
  const panel = document.querySelector(".context-source-panel");
  if (panel && state.contextDraft) panel.innerHTML = contextSourcePanelMarkup(state.contextDraft);
}

function contextMessageElement(message, index, uiKey, expanded = false) {
  const template = document.createElement("template");
  template.innerHTML = contextEditor.renderMessages([message], [uiKey], expanded ? uiKey : null, contextDraftLocked()).trim();
  const element = template.content.firstElementChild;
  element.dataset.contextMessageIndex = String(index);
  element.querySelector("[data-context-message-json]")?.setAttribute("data-context-message-index", String(index));
  return element;
}

function contextListPositions() {
  return new Map([...document.querySelectorAll(".context-message-editor")].map(row => [row.dataset.contextUiKey, row.getBoundingClientRect().top]));
}

function animateContextReflow(previous) {
  if (!previous || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
  document.querySelectorAll(".context-message-editor").forEach(row => {
    const before = previous.get(row.dataset.contextUiKey);
    if (before == null) return;
    const delta = before - row.getBoundingClientRect().top;
    if (Math.abs(delta) > 1) row.animate([{ transform: `translateY(${delta}px)` }, { transform: "translateY(0)" }], { duration: 190, easing: "cubic-bezier(.2,.8,.2,1)" });
  });
}

function updateContextMessageIndices() {
  const draft = state.contextDraft;
  const list = document.querySelector(".context-message-list");
  if (!draft || !list) return;
  const rows = [...list.querySelectorAll(".context-message-editor")];
  rows.forEach((row, index) => {
    row.dataset.contextMessageIndex = String(index);
    const role = draft.messages[index]?.role || "未指定角色";
    row.querySelector("header strong").textContent = `${String(index + 1).padStart(2, "0")} · ${role}`;
    const input = row.querySelector("[data-context-message-json]");
    if (input) {
      input.dataset.contextMessageIndex = String(index);
      input.setAttribute("aria-label", `消息 ${index + 1} JSON`);
    }
  });
  const count = document.querySelector(".context-compose-heading > div > span");
  if (count) count.textContent = `${draft.messages.length} 条消息`;
  if (!rows.length && !list.querySelector(".context-empty-drop")) list.innerHTML = contextEditor.renderMessages([], []);
}

function renderContextEditor(scrollTop = null) {
  const draft = state.contextDraft;
  if (!draft) { state.view = "map"; return renderMap(); }
  ensureContextUiKeys(draft);
  const created = Boolean(draft.context);
  const locked = contextDraftLocked(draft);
  app.innerHTML = `<section class="context-editor-view">
    <aside class="context-source-panel">${contextSourcePanelMarkup(draft)}</aside>
    <section class="context-definition-panel">
      <header><div><span class="review-kicker">NEW CONTEXT</span><h1>自由组装</h1></div><button class="text-button" data-action="exit-context-editor">关闭</button></header>
      <label>Context 标题<input data-context-title value="${escapeHtml(draft.title)}" ${created ? "disabled" : ""}></label>
      <div class="context-compose-heading"><div><strong>新 Context</strong><span>${draft.messages.length} 条消息</span></div><div class="history-actions"><button class="text-button" data-action="context-message-add" ${locked ? "disabled" : ""}>新增消息</button><button class="text-button danger" data-action="context-message-clear" ${locked ? "disabled" : ""}>清空</button></div></div>
      <div class="context-message-list" data-context-drop-zone>${contextEditor.renderMessages(draft.messages, draft.uiKeys, draft.expandedKey, locked)}</div>
      ${renderContextDecision(draft.context)}
      <div class="context-undo-toast" hidden><span>已删除消息</span><button type="button" class="text-button" data-action="context-message-undo">撤销</button></div>
      ${locked ? "" : `<footer><span class="muted tiny">只有右侧内容会成为新 Context；来源始终保持不变。</span><button class="primary" data-action="submit-context">${draft.context ? "重新编译" : "创建 Context"}</button></footer>`}
    </section>
  </section>`;
  if (Number.isFinite(scrollTop)) document.querySelector(".context-definition-panel").scrollTop = scrollTop;
}

function insertContextMessage(message, index, options = {}) {
  const draft = syncContextDraft();
  ensureContextUiKeys(draft);
  const list = document.querySelector(".context-message-list");
  const previous = contextListPositions();
  const safeIndex = Math.max(0, Math.min(Number(index), draft.messages.length));
  const key = options.key || nextContextUiKey();
  draft.messages.splice(safeIndex, 0, contextEditor.cloneMessages([message])[0]);
  draft.uiKeys.splice(safeIndex, 0, key);
  if (options.expand) draft.expandedKey = key;
  list.querySelector(".context-empty-drop")?.remove();
  const rows = [...list.querySelectorAll(".context-message-editor")];
  list.insertBefore(contextMessageElement(draft.messages[safeIndex], safeIndex, key, Boolean(options.expand)), rows[safeIndex] || null);
  updateContextMessageIndices();
  animateContextReflow(previous);
  if (options.focus) requestAnimationFrame(() => list.querySelector(`[data-context-ui-key="${key}"] [data-context-message-json]`)?.focus());
  return key;
}

function moveContextMessage(from, to, focus = false) {
  const draft = syncContextDraft();
  if (from < 0 || from >= draft.messages.length || to < 0 || to >= draft.messages.length || from === to) return;
  const previous = contextListPositions();
  const key = draft.uiKeys[from];
  contextEditor.move(draft.messages, from, to);
  contextEditor.move(draft.uiKeys, from, to);
  const rows = new Map([...document.querySelectorAll(".context-message-editor")].map(row => [row.dataset.contextUiKey, row]));
  const list = document.querySelector(".context-message-list");
  draft.uiKeys.forEach(uiKey => list.append(rows.get(uiKey)));
  updateContextMessageIndices();
  animateContextReflow(previous);
  if (focus) document.querySelector(`[data-context-ui-key="${key}"] [data-context-pointer-handle]`)?.focus();
}

function toggleContextMessage(uiKey) {
  const draft = syncContextDraft();
  const previousExpanded = draft.expandedKey;
  draft.expandedKey = previousExpanded === uiKey ? null : uiKey;
  const replace = key => {
    if (!key) return;
    const index = draft.uiKeys.indexOf(key);
    const row = document.querySelector(`[data-context-ui-key="${key}"]`);
    if (row && index >= 0) row.replaceWith(contextMessageElement(draft.messages[index], index, key, draft.expandedKey === key));
  };
  replace(previousExpanded);
  if (uiKey !== previousExpanded) replace(uiKey);
  if (draft.expandedKey) requestAnimationFrame(() => document.querySelector(`[data-context-ui-key="${draft.expandedKey}"] [data-context-message-json]`)?.focus());
}

function showContextUndo() {
  clearTimeout(contextUndoTimer);
  const toast = document.querySelector(".context-undo-toast");
  if (toast) toast.hidden = false;
  contextUndoTimer = setTimeout(() => {
    if (state.contextDraft) state.contextDraft.undo = null;
    const current = document.querySelector(".context-undo-toast");
    if (current) current.hidden = true;
  }, 4500);
}

async function deleteContextMessage(index) {
  const draft = syncContextDraft();
  const row = document.querySelector(`.context-message-editor[data-context-message-index="${index}"]`);
  if (!row) return;
  const panel = document.querySelector(".context-definition-panel");
  const scrollTop = panel?.scrollTop || 0;
  const previous = contextListPositions();
  const [message] = draft.messages.splice(index, 1);
  const [key] = draft.uiKeys.splice(index, 1);
  if (draft.expandedKey === key) draft.expandedKey = null;
  draft.undo = { message, key, index };
  if (!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
    const animation = row.animate([{ opacity: 1, transform: "scale(1)", maxHeight: `${row.offsetHeight}px` }, { opacity: 0, transform: "scale(.98)", maxHeight: "0px" }], { duration: 150, easing: "ease-in", fill: "forwards" });
    await Promise.race([animation.finished.catch(() => {}), new Promise(resolve => setTimeout(resolve, 180))]);
  }
  row.remove();
  updateContextMessageIndices();
  if (panel) panel.scrollTop = Math.min(scrollTop, Math.max(0, panel.scrollHeight - panel.clientHeight));
  animateContextReflow(previous);
  showContextUndo();
}

function undoContextMessageDelete() {
  const draft = state.contextDraft;
  if (!draft?.undo) return;
  const undo = draft.undo;
  draft.undo = null;
  clearTimeout(contextUndoTimer);
  document.querySelector(".context-undo-toast")?.setAttribute("hidden", "");
  insertContextMessage(undo.message, undo.index, { key: undo.key });
}

function clearContextMessages() {
  const draft = syncContextDraft();
  draft.messages = [];
  draft.uiKeys = [];
  draft.expandedKey = null;
  document.querySelector(".context-message-list").innerHTML = contextEditor.renderMessages([], []);
  document.querySelector(".context-compose-heading span").textContent = "0 条消息";
}

async function addContextSource(contextId) {
  const draft = syncContextDraft();
  if (!draft || draft.sources.some(source => source.context_id === contextId)) return;
  try {
    const snapshot = await api(`/desktop/api/contexts/${contextId}/snapshot`);
    if (!snapshot.checkpoint_id) throw new Error("来源 Context 尚无已提交 checkpoint");
    draft.sources.push({ context_id: contextId, checkpoint_id: snapshot.checkpoint_id });
    draft.sourceSnapshots.push({ context_id: contextId, checkpoint_id: snapshot.checkpoint_id, messages: contextEditor.cloneMessages(snapshot.messages) });
    draft.activeSourceId = contextId;
    renderContextSourcePanel();
  } catch (error) { setStatus(error.message, true); }
}

async function submitContext() {
  let draft;
  try { draft = syncContextDraft(); }
  catch (error) { return setStatus(error.message, true); }
  if (!draft.title) return setStatus("Context 标题不能为空", true);
  try {
    const context = draft.context
      ? await api(`/desktop/api/contexts/${draft.context.context_id}/definition`, { method: "PUT", body: JSON.stringify({ messages: draft.messages }) })
      : await api("/desktop/api/contexts/derive", { method: "POST", body: JSON.stringify({ title: draft.title, sources: draft.sources, messages: draft.messages }) });
    draft.context = context;
    await refreshTasks();
    await hydrateContextTrees();
    if (context.projection_status === "valid") return openDerivedContext();
    renderContextEditor();
  } catch (error) { setStatus(error.message, true); }
}

async function decideContextProjection(decision) {
  const context = state.contextDraft?.context;
  if (!context) return;
  try {
    const updated = await api(`/desktop/api/contexts/${context.context_id}/projection/decision`, {
      method: "POST",
      body: JSON.stringify({ decision, definition_hash: context.definition_hash, projection_hash: context.projection_hash }),
    });
    state.contextDraft.context = updated;
    if (decision === "accept") return openDerivedContext();
    renderContextEditor();
  } catch (error) { setStatus(error.message, true); }
}

async function openDerivedContext() {
  const contextId = state.contextDraft?.context?.context_id;
  if (!contextId) return;
  state.activeTaskId = contextId;
  state.contextDraft = null;
  state.view = "focus";
  await hydrateActive();
  render();
}

function renderDraft() {
  const draft = state.drafts.get(state.activeTaskId);
  const task = state.tasks.find(item => item.task_id === draft?.task_id);
  if (!draft || !task) { state.view = "map"; renderMap(); return; }
  app.innerHTML = `<section class="draft-view">
    <aside class="draft-map"><div class="task-grid">${taskCards(true)}</div></aside>
    <section class="draft-panel">
      <header class="draft-heading"><h1>${escapeHtml(task.title)} · 小兵草稿</h1><span class="run-indicator">主 Agent 可继续运行</span><span class="muted tiny">复制自 checkpoint ${escapeHtml(draft.source_checkpoint_id || "空历史")}</span></header>
      <div class="draft-sections">
        ${draftSection("system", "1. System Prompt", `<textarea data-draft-field="system_prompt">${escapeHtml(draft.system_prompt)}</textarea>`)}
        ${draftSection("history", "2. 上下文历史", renderHistory(draft))}
        ${draftSection("final", "3. 最后一条 HumanMessage", renderSkillPicker("draft", `<textarea data-draft-field="final_human_message" placeholder="给小兵的任务…">${escapeHtml(draft.final_human_message)}</textarea>`))}
        ${draftSection("equipment", "4. 模型、工具、技能与权限", renderEquipment(draft))}
      </div>
      <footer class="draft-footer"><button class="text-button" data-action="exit-draft">退出并保存</button><span class="token-count" id="tokenCount">估算 ${draft.token_estimate} tokens</span><button class="primary" data-action="deploy">投放</button></footer>
    </section>
  </section>`;
  updateTokenState();
}

function draftSection(id, title, body) {
  return `<details class="draft-section" data-section="${id}" ${state.openDraftSection === id ? "open" : ""}><summary>${title}<span>${state.openDraftSection === id ? "收起" : "展开"}</span></summary><div class="section-body">${body}</div></details>`;
}

function renderHistory(draft) {
  return `<div class="history-actions"><button class="text-button" data-action="add-message">增加消息</button><button class="text-button danger" data-action="clear-history">清空历史</button></div><div id="historyList">${draft.history_messages.map((message, index) => renderMessageEditor(message, index)).join("")}</div>`;
}

function renderMessageEditor(message, index) {
  const locked = message.locked || message.role === "tool" || message.tool_calls?.length;
  const content = typeof message.content === "string" ? message.content : JSON.stringify(message.content, null, 2);
  return `<div class="message-editor" draggable="true" data-index="${index}">
    <button class="drag-handle" type="button" aria-label="拖动排序">${String(index + 1).padStart(2, "0")}</button>
    <select data-message-field="role" ${locked ? "disabled" : ""}><option value="human" ${["human", "user"].includes(message.role) ? "selected" : ""}>Human</option><option value="ai" ${["ai", "assistant"].includes(message.role) ? "selected" : ""}>AI</option><option value="system" ${message.role === "system" ? "selected" : ""}>System</option><option value="tool" ${message.role === "tool" ? "selected" : ""}>Tool</option></select>
    <textarea data-message-field="content">${escapeHtml(content)}</textarea>
    <button class="delete-message" type="button" data-action="delete-message" aria-label="删除消息">删除</button>
    ${locked ? `<span class="locked-note">工具调用结构已锁定；删除会同时处理关联消息。</span>` : ""}
  </div>`;
}

function renderEquipment(draft) {
  const equipment = draft.equipment || {};
  const permissions = equipment.permissions || ["read"];
  return `<div class="equipment-grid">
    <label>模型<select data-equipment="model_name">${state.equipment.models.map(model => `<option value="${model.name}" ${model.name === equipment.model_name ? "selected" : ""}>${escapeHtml(model.display_name)}</option>`).join("")}</select></label>
    <div><span class="tiny muted">权限</span><div class="check-line">${state.equipment.permissions.map(permission => `<label><input type="checkbox" data-permission="${permission}" ${permissions.includes(permission) ? "checked" : ""}>${permission}</label>`).join("")}</div></div>
    <p class="tiny danger">无沙箱：写入或命令权限会直接影响真实宿主机。命令权限可绕过文件工具规则。</p>
  </div>`;
}

async function openDraft(taskId) {
  setStatus("复制 checkpoint…");
  try {
    const [draft, catalog] = await Promise.all([
      api(`/desktop/api/tasks/${taskId}/drafts/open`, { method: "POST" }),
      api(`/desktop/api/tasks/${taskId}/skills`),
    ]);
    state.activeTaskId = taskId;
    state.drafts.set(taskId, draft);
    state.skillCatalogs.set(taskId, catalog.skills);
    state.view = "draft";
    state.soldierArmed = false;
    setStatus("");
    render();
  } catch (error) { setStatus(error.message, true); }
}

function syncDraftFromDom() {
  const draft = state.drafts.get(state.activeTaskId);
  if (!draft) return null;
  document.querySelectorAll(".message-editor").forEach(row => {
    const message = draft.history_messages[Number(row.dataset.index)];
    const role = row.querySelector('[data-message-field="role"]');
    if (role && !role.disabled) message.role = role.value;
    message.content = row.querySelector('[data-message-field="content"]').value;
  });
  document.querySelectorAll("[data-draft-field]").forEach(input => { draft[input.dataset.draftField] = input.value; });
  const model = document.querySelector('[data-equipment="model_name"]');
  if (model) draft.equipment.model_name = model.value;
  draft.equipment.permissions = [...document.querySelectorAll("[data-permission]:checked")].map(input => input.dataset.permission);
  draft.equipment.skills = normalizeSkillNames(draft.equipment.skills);
  return draft;
}

function scheduleDraftSave() {
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(saveDraft, 350);
}

async function saveDraft() {
  const draft = syncDraftFromDom();
  if (!draft) return;
  try {
    const saved = await api(`/desktop/api/drafts/${draft.draft_id}`, { method: "PUT", body: JSON.stringify({
      system_prompt: draft.system_prompt,
      history_messages: draft.history_messages,
      final_human_message: draft.final_human_message,
      equipment: draft.equipment,
    }) });
    state.drafts.set(state.activeTaskId, saved);
    const counter = document.querySelector("#tokenCount");
    if (counter) { counter.textContent = `估算 ${saved.token_estimate} tokens`; updateTokenState(); }
    setStatus("草稿已保存");
  } catch (error) { setStatus(error.message, true); }
}

function updateTokenState() {
  const draft = state.drafts.get(state.activeTaskId);
  const model = state.equipment.models.find(item => item.name === draft?.equipment?.model_name);
  const over = model?.context_window && draft.token_estimate > model.context_window;
  document.querySelector("#tokenCount")?.classList.toggle("over", Boolean(over));
  const deploy = document.querySelector('[data-action="deploy"]');
  if (deploy) deploy.disabled = Boolean(over) || !draft.final_human_message?.trim();
}

async function deployDraft() {
  if (state.deploying) return;
  state.deploying = true;
  document.querySelector('[data-action="deploy"]')?.setAttribute("disabled", "");
  await saveDraft();
  const draft = state.drafts.get(state.activeTaskId);
  if (!draft.final_human_message.trim()) {
    state.deploying = false;
    updateTokenState();
    return setStatus("最后一条 HumanMessage 不能为空", true);
  }
  try {
    draft.deployment_id ||= crypto.randomUUID();
    const run = await api(`/desktop/api/drafts/${draft.draft_id}/deploy`, { method: "POST", body: JSON.stringify({ deployment_id: draft.deployment_id }) });
    listenToRun(run);
    state.view = "map";
    state.drafts.delete(state.activeTaskId);
    await refreshTasks();
    render();
  } catch (error) { setStatus(error.message, true); }
  finally { state.deploying = false; updateTokenState(); }
}

async function sendMain() {
  adoptCommitmentContext();
  if (activeTaskHasCommitmentLock()) {
    return setStatus("存在尚未处理的承诺流程，请先处理审批面板", true);
  }
  setStatus("");
  const input = document.querySelector("#mainInput");
  const message = input.value.trim();
  if (!message) return;
  const detail = state.details.get(state.activeTaskId);
  try {
    const run = await api(`/desktop/api/tasks/${state.activeTaskId}/main/runs`, {
      method: "POST",
      body: JSON.stringify({ message, skills: selectedSkills("main") }),
    });
    const contextNode = (state.contextTrees.get(activeTask().workspace_id) || [])
      .find(item => item.context_id === state.activeTaskId);
    if (contextNode) contextNode.editable = false;
    if (detail.context) detail.context.editable = false;
    // 运行已发起：立即暴露中断入口（否则运行中 active_run 仍为旧值，按钮不渲染）
    detail.active_run = run;
    detail.messages = [...(detail.messages || []), { role: "human", content: message }];
    detail.ui_state = { ...(detail.ui_state || {}), input: "", skills: [] };
    renderFocus();
    persistFocusState();
    listenToRun(run);
  } catch (error) { setStatus(error.message, true); }
}

const COMMITMENT_STAGE_NAMES = {
  1: "明确目标", 2: "要求与兼容性", 3: "优先级", 4: "必要输入",
  5: "技术版本", 6: "官方知识", 7: "合同落盘", 8: "产出合同", 9: "交接准备",
};
const TRACE_ACTORS = { supervisor: "Supervisor", worker: "Worker", evaluator: "Evaluator" };

function listenToRun(run) {
  if (state.streams.has(run.run_id)) return;
  let runError = null;
  const source = new EventSource(`${runtime.apiBase}/desktop/api/runs/${run.run_id}/stream?session=${encodeURIComponent(runtime.session)}`);
  state.streams.set(run.run_id, source);
  source.addEventListener("tokens", event => appendToken(JSON.parse(event.data)));
  source.addEventListener("events", event => {
    const envelope = JSON.parse(event.data);
    const payload = envelope.data;
    if (payload && payload.type === "commitment_messages") {
      if (commitmentBelongsToTask(envelope)) appendCommitmentMessages(payload);
      return;
    }
    const messages = payload?.messages;
    const task = state.tasks.find(item => item.thread_id === envelope.thread_id && item.workspace_id === envelope.workspace_id);
    if (task && messages && envelope.agent_id.startsWith("main:")) {
      beginLeadExecution(task.task_id);
      clearStreamBuffer(run.run_id);
      replaceConversation(task, messages);
    }
  });
  source.addEventListener("interrupt", event => {
    const envelope = JSON.parse(event.data);
    const value = envelope.data?.value;
    if (!value || value.type !== "commitment_review") return;
    if (!commitmentBelongsToTask(envelope)) return;
    showReview(value);
  });
  source.addEventListener("error", event => {
    if (!event.data) return;
    const error = JSON.parse(event.data).data?.error || "运行失败";
    runError = error;
    setStatus(error, true);
  });
  source.addEventListener("end", async event => {
    const terminal = event.data ? JSON.parse(event.data) : { status: "error", error: "运行流异常结束" };
    if (!terminal.error && runError) terminal.error = runError;
    // 主动中断判定：主 Agent run 终态 interrupted 且无错误、且非承诺审批（审批面板已接管界面状态）
    const wasMainInterrupted = run.kind === "main" && terminal.status === "interrupted" && !terminal.error;
    source.close(); state.streams.delete(run.run_id); clearStreamBuffer(run.run_id);
    await refreshTasks();
    await hydrateContextTrees();
    if (state.activeTaskId) await hydrateActive();
    const task = run.task_id
      ? state.tasks.find(item => item.task_id === run.task_id)
      : state.tasks.find(item => item.thread_id === run.thread_id);
    if (task) settleCommitmentRun(task.task_id, terminal);
    render();
    if (wasMainInterrupted && !state.commitment.review && !state.commitment.recovery) {
      setStatus("主 Agent 已中断，可继续对话");
    }
  });
}

// === 承诺进度条 ===

function setProgress(stage, visible = true) {
  const container = document.querySelector("#commitmentProgress");
  if (!container) return;
  container.hidden = !visible;
  if (!visible) return;
  const list = container.querySelector("#progressSteps");
  list.replaceChildren();
  for (let number = 1; number <= 9; number += 1) {
    const item = document.createElement("li");
    item.className = number < stage ? "done" : number === stage ? "active" : "";
    item.title = `${number}. ${COMMITMENT_STAGE_NAMES[number] || ""}`;
    list.append(item);
  }
  container.querySelector("#progressLabel").textContent = COMMITMENT_STAGE_NAMES[stage] || "正在准备任务合同";
}

// === 执行轨迹面板 ===

function conversationNode() {
  return document.querySelector("#conversation");
}

function commitmentBelongsToTask(envelope) {
  const task = state.tasks.find(item => item.thread_id === envelope.thread_id && item.workspace_id === envelope.workspace_id);
  if (!task) return false;
  if (state.commitment.taskId === task.task_id && state.commitment.terminalStatus) {
    resetCommitment();
  }
  if (state.commitment.taskId === null) state.commitment.taskId = task.task_id;
  return state.commitment.taskId === task.task_id;
}

function resetCommitment() {
  clearInterval(state.commitment.traceTimer);
  state.commitment.traceTimer = null;
  state.commitment.tracePanel?.remove();
  state.commitment.reviewPanel?.remove();
  state.commitment.tracePanel = null;
  state.commitment.reviewPanel = null;
  state.commitment.traceStartedAt = null;
  state.commitment.traceStreams = new Map();
  state.commitment.traceItems = new Map();
  state.commitment.messageIds = new Set();
  state.commitment.terminalStatus = null;
  state.commitment.terminalError = null;
  state.commitment.handoffStarted = false;
  state.commitment.taskId = null;
  state.commitment.stage = 0;
  state.commitment.review = null;
  state.commitment.recovery = null;
  state.commitment.recoveryRestored = false;
}

function beginLeadExecution(taskId) {
  if (state.commitment.taskId !== taskId
      || state.commitment.stage < 9
      || state.commitment.review
      || state.commitment.recovery
      || state.commitment.terminalStatus
      || state.commitment.handoffStarted) return;
  state.commitment.handoffStarted = true;
  finishTracePanel("承诺已完成");
  if (state.commitment.tracePanel) state.commitment.tracePanel.open = false;
  setProgress(state.commitment.stage, false);
  setStatus("Lead Agent 正在执行…");
  const conversation = conversationNode();
  if (conversation && state.commitment.tracePanel?.isConnected) {
    conversation.prepend(state.commitment.tracePanel);
  }
}

function adoptCommitmentContext() {
  // 承诺 UI 上下文绑定到当前任务；后端负责拒绝越过待确认 checkpoint 的普通输入。
  if (state.commitment.taskId && state.commitment.taskId !== state.activeTaskId) {
    resetCommitment();
  }
}

function ensureTracePanel() {
  if (state.commitment.tracePanel?.isConnected) return;
  const fragment = document.querySelector("#traceTemplate").content.cloneNode(true);
  state.commitment.tracePanel = fragment.querySelector(".trace-panel");
  state.commitment.traceStartedAt = Date.now();
  state.commitment.traceStreams = new Map();
  clearInterval(state.commitment.traceTimer);
  state.commitment.traceTimer = setInterval(updateTraceElapsed, 1000);
  const conversation = conversationNode();
  // 面板绑定任务：仅在渲染该任务时插入 DOM；切走时保留内存节点，切回由 restore 补插
  if (conversation && state.commitment.taskId === state.activeTaskId) {
    conversation.append(state.commitment.tracePanel);
  }
  updateTraceElapsed();
}

function updateTraceElapsed() {
  const panel = state.commitment.tracePanel;
  if (!panel?.isConnected || !state.commitment.traceStartedAt) return;
  const seconds = Math.max(0, Math.floor((Date.now() - state.commitment.traceStartedAt) / 1000));
  panel.querySelector(".trace-elapsed").textContent = `已思考 ${seconds} 秒`;
}

function updateTraceCount() {
  const panel = state.commitment.tracePanel;
  if (!panel?.isConnected) return;
  panel.querySelector(".trace-count").textContent = `${panel.querySelectorAll(".trace-item").length} 项`;
}

function isNearBottom(element, threshold = 80) {
  return element && element.scrollHeight - element.scrollTop - element.clientHeight <= threshold;
}

function contentText(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.map(item => typeof item === "string" ? item : item?.text || "").filter(Boolean).join("\n");
}

function prettyDraft(value) {
  if (typeof value === "string") return value;
  return JSON.stringify(value ?? {}, null, 2);
}

function advanceCommitmentStage(stage) {
  const next = Number(stage || 0);
  if (next > state.commitment.stage) state.commitment.stage = next;
  return state.commitment.stage;
}

function settleCommitmentRun(taskId, terminal) {
  if (state.commitment.taskId !== taskId) return;
  if (state.commitment.review || state.commitment.recovery) return;
  const status = String(terminal?.status || "error");
  if (["pending", "running", "interrupted"].includes(status)) return;
  const error = terminal?.error || state.commitment.terminalError || null;
  state.commitment.terminalStatus = status;
  state.commitment.terminalError = error;
  finishTracePanel(status === "success" ? "已完成" : "运行失败");
  setProgress(state.commitment.stage, false);
  if (status !== "success") setStatus(error || "运行失败", true);
  else setStatus("");
}

function revealTraceText(element, text) {
  const value = String(text || "");
  if (!value || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    element.textContent = value;
    return;
  }
  let index = 0;
  const size = Math.max(1, Math.ceil(value.length / 100));
  const step = () => {
    if (!element.isConnected || index >= value.length) return;
    index = Math.min(value.length, index + size);
    element.textContent = value.slice(0, index);
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function appendCommitmentTrace(trace) {
  const followMessages = isNearBottom(conversationNode());
  ensureTracePanel();
  const panel = state.commitment.tracePanel;
  const item = document.createElement("li");
  item.className = `trace-item trace-${trace.actor} trace-${trace.status}`;
  item.dataset.actor = trace.actor;
  item.dataset.stage = String(trace.stage);
  if (trace.status === "running") item.classList.add("is-active");
  const meta = document.createElement("div");
  meta.className = "trace-meta";
  const actor = document.createElement("span");
  actor.className = "trace-actor";
  actor.textContent = TRACE_ACTORS[trace.actor] || trace.actor;
  const position = document.createElement("span");
  position.textContent = `第 ${trace.stage} 步${trace.attempt ? ` · 第 ${trace.attempt} 轮` : ""}`;
  meta.append(actor, position);
  const title = document.createElement("strong");
  title.textContent = trace.title;
  item.append(meta, title);
  if (trace.detail) {
    const detail = document.createElement("p");
    revealTraceText(detail, trace.detail);
    item.append(detail);
  }
  if (trace.payload?.reasoning_summary) {
    const reasoning = document.createElement("p");
    reasoning.className = "trace-reasoning";
    reasoning.textContent = trace.payload.reasoning_summary;
    item.append(reasoning);
  }
  if (trace.payload !== undefined) {
    const payload = document.createElement("details");
    payload.className = "trace-payload";
    payload.innerHTML = "<summary>查看输入与输出</summary><pre></pre>";
    payload.querySelector("pre").textContent = prettyDraft(trace.payload);
    item.append(payload);
  }
  panel.querySelector(".trace-list").append(item);
  if (trace.status !== "running") completeTraceActivity(trace);
  updateTraceCount();
  advanceCommitmentStage(trace.stage);
  setProgress(state.commitment.stage, !state.commitment.terminalStatus);
  if (followMessages) scrollConversation();
  return item;
}

function appendTraceOutputDelta(trace, followMessages) {
  ensureTracePanel();
  const panel = state.commitment.tracePanel;
  const streamId = trace.payload?.stream_id || `${trace.actor}-${trace.stage}`;
  const key = `${trace.actor}:${trace.stage}:${streamId}`;
  let item = state.commitment.traceStreams.get(key);
  if (!item?.isConnected) {
    item = document.createElement("li");
    item.className = `trace-item trace-${trace.actor} trace-stream-item is-active`;
    item.dataset.actor = trace.actor;
    item.dataset.stage = String(trace.stage);
    item.innerHTML = `
      <div class="trace-meta">
        <span class="trace-actor"></span>
        <span>第 ${trace.stage} 步 · 公开输出流</span>
      </div>
      <strong></strong>
      <pre class="trace-stream"><span></span><i aria-hidden="true"></i></pre>`;
    item.querySelector(".trace-actor").textContent = TRACE_ACTORS[trace.actor] || trace.actor;
    item.querySelector("strong").textContent = trace.title;
    panel.querySelector(".trace-list").append(item);
    state.commitment.traceStreams.set(key, item);
    updateTraceCount();
  }
  const stream = item.querySelector(".trace-stream");
  const followStream = isNearBottom(stream, 24);
  stream.querySelector("span").textContent += String(trace.payload?.delta || "");
  if (followStream) {
    requestAnimationFrame(() => { stream.scrollTop = stream.scrollHeight; });
  }
  if (followMessages) scrollConversation();
}

function completeTraceActivity(trace) {
  const panel = state.commitment.tracePanel;
  if (!panel?.isConnected) return;
  panel.querySelectorAll(`.trace-item.is-active[data-actor="${trace.actor}"][data-stage="${trace.stage}"]`)
    .forEach(item => item.classList.remove("is-active"));
  for (const [key, item] of state.commitment.traceStreams) {
    if (item.dataset.actor === trace.actor && item.dataset.stage === String(trace.stage)) {
      item.querySelector("i")?.remove();
      state.commitment.traceStreams.delete(key);
    }
  }
}

function finishTracePanel(label = "已完成") {
  clearInterval(state.commitment.traceTimer);
  state.commitment.traceTimer = null;
  const panel = state.commitment.tracePanel;
  if (!panel?.isConnected) return;
  updateTraceElapsed();
  panel.classList.remove("is-live");
  panel.classList.add("is-finished");
  panel.querySelector(".trace-state").textContent = label;
  panel.querySelectorAll(".trace-item.is-active").forEach(item => item.classList.remove("is-active"));
  panel.querySelectorAll(".trace-stream i").forEach(cursor => cursor.remove());
}

function appendCommitmentMessages(batch) {
  const actor = batch.actor || "supervisor";
  const stage = Number(batch.stage || 0);
  const attempt = Number(batch.attempt || 0) || undefined;
  batch.messages.forEach(message => {
    const type = String(message.type || message.role || "").toLowerCase();
    const text = contentText(message.content);
    const key = message.id || `${actor}:${stage}:${attempt || 0}:${type}:${text}`;
    const signature = JSON.stringify(message);
    if (type.includes("human")) {
      if (actor === "supervisor" || state.commitment.messageIds.has(key)) return;
      state.commitment.messageIds.add(key);
      appendCommitmentTrace({
        actor, stage, attempt,
        title: `${TRACE_ACTORS[actor] || actor} 收到输入`,
        status: "running",
        detail: "已接收当前任务、Supervisor 消息历史与验收条件。",
        payload: message,
      });
      return;
    }
    if (actor !== "supervisor" && (type.includes("chunk") || !message.id)) {
      appendTraceOutputDelta({
        actor, stage, attempt,
        title: `${TRACE_ACTORS[actor] || actor} 正在生成`,
        payload: { stream_id: `${actor}-${stage}-${attempt || 0}`, delta: text },
      }, isNearBottom(conversationNode()));
      return;
    }
    if (state.commitment.messageIds.has(key)) {
      const existing = state.commitment.traceItems.get(key);
      if (actor !== "supervisor" || !type.includes("tool")
          || !existing || existing.signature === signature) return;
      existing.item.remove();
      state.commitment.messageIds.delete(key);
      state.commitment.traceItems.delete(key);
    }
    state.commitment.messageIds.add(key);
    let payload = message;
    let status = "running";
    let title = `${TRACE_ACTORS[actor] || actor} 消息`;
    if (type.includes("tool")) {
      try { payload = JSON.parse(text); } catch { payload = message; }
      status = ["approved", "revised"].includes(payload?.status) ? "completed" : "failed";
      title = "delegate_with_review 最终返回";
    } else if (actor === "supervisor") {
      title = "Supervisor 委派阶段任务";
    }
    const messageStage = Number(payload?.stage || message.tool_calls?.[0]?.args?.stage || stage);
    const item = appendCommitmentTrace({
      actor, stage: messageStage, attempt, title, status,
      detail: type.includes("tool") ? "" : text,
      payload,
    });
    if (actor === "supervisor" && type.includes("tool")) {
      state.commitment.traceItems.set(key, { item, signature });
    }
  });
}

// === 审批面板 ===

function showReview(payload) {
  state.commitment.review = payload;
  state.commitment.recovery = {
    ...(state.commitment.recovery || {}),
    status: "resumable",
    stage: Number(payload.stage || 0),
    review: payload,
  };
  state.commitment.stage = Number(payload.stage || state.commitment.stage);
  finishTracePanel("等待确认");
  if (state.commitment.tracePanel?.isConnected) state.commitment.tracePanel.open = true;
  setStatus("等待确认", false);
  setProgress(state.commitment.stage, true);
  removeReviewPanel();

  const fragment = document.querySelector("#reviewTemplate").content.cloneNode(true);
  const panel = fragment.querySelector(".review-panel");
  panel.dataset.recoveryStatus = "resumable";
  panel.dataset.stage = String(payload.stage || 0);
  panel.querySelector(".review-kicker").textContent = `第 ${payload.stage} 步`;
  panel.querySelector("h3").textContent = COMMITMENT_STAGE_NAMES[payload.stage] || "人工确认";
  panel.querySelector(".review-draft").textContent = prettyDraft(payload.draft);
  panel.querySelector(".review-error").textContent = payload.error || "";
  const allowed = payload.allowed_decisions || ["approve", "revise"];
  panel.querySelector(".approve-button").hidden = !allowed.includes("approve");
  if (payload.revise_label || !allowed.includes("approve")) {
    panel.querySelector(".revise-toggle").textContent = payload.revise_label || (payload.stage === 2 ? "解决矛盾" : "提出修订");
  }
  const contract = payload.draft?.contract_markdown;
  if (payload.stage === 7 && typeof contract === "string") {
    panel.querySelector(".review-draft").hidden = true;
    const editor = panel.querySelector(".review-contract-editor");
    editor.hidden = false;
    editor.value = contract;
    editor.dataset.original = contract.trim();
    panel.querySelector(".approve-button").textContent = "确认并写入";
    panel.querySelector(".revise-toggle").textContent = "反馈重写";
  }
  bindReview(panel);
  state.commitment.reviewPanel = panel;
  const conversation = conversationNode();
  if (conversation && state.commitment.taskId === state.activeTaskId) conversation.append(panel);
  scrollConversation();
}

function removeReviewPanel() {
  state.commitment.reviewPanel?.remove();
  state.commitment.reviewPanel = null;
}

function bindReview(panel) {
  const form = panel.querySelector(".revision-form");
  const input = panel.querySelector(".revision-input");
  const contractEditor = panel.querySelector(".review-contract-editor");
  const approveButton = panel.querySelector(".approve-button");
  const stage = Number(panel.dataset.stage || 0);
  let mode = "feedback";

  const updateContractAction = () => {
    if (stage !== 7) return;
    approveButton.textContent =
      contractEditor.value.trim() === contractEditor.dataset.original
        ? "确认并写入"
        : "提交编辑并审核";
  };

  contractEditor.addEventListener("input", updateContractAction);
  approveButton.addEventListener("click", () => {
    if (stage === 7) {
      const contract = contractEditor.value.trim();
      if (!contract) {
        panel.querySelector(".review-error").textContent = "任务合同不能为空";
        return;
      }
      if (contract !== contractEditor.dataset.original) {
        disableReview(panel);
        resumeRun({ decision: "revise", replacement: { contract_markdown: contract } });
        return;
      }
    }
    disableReview(panel);
    resumeRun({ decision: "approve" });
  });

  panel.querySelector(".revise-toggle").addEventListener("click", () => {
    panel.querySelector(".review-actions").hidden = true;
    form.hidden = false;
    input.focus();
  });

  panel.querySelector(".cancel-revision").addEventListener("click", () => {
    form.hidden = true;
    panel.querySelector(".review-actions").hidden = false;
  });

  panel.querySelectorAll(".segmented button").forEach(button => {
    button.addEventListener("click", () => {
      mode = button.dataset.mode;
      panel.querySelectorAll(".segmented button").forEach(item => item.classList.toggle("active", item === button));
      input.value = "";
      input.placeholder = mode === "feedback" ? "输入需要调整的内容" : "输入完整 JSON 替换草稿";
    });
  });

  form.addEventListener("submit", event => {
    event.preventDefault();
    const value = input.value.trim();
    if (!value) return;
    const payload = { decision: "revise" };
    if (mode === "feedback") {
      payload.feedback = value;
    } else {
      try { payload.replacement = JSON.parse(value); }
      catch {
        panel.querySelector(".review-error").textContent = "替换草稿必须是有效 JSON";
        return;
      }
    }
    disableReview(panel);
    resumeRun(payload);
  });
}

function disableReview(panel) {
  panel.querySelectorAll("button, textarea").forEach(control => { control.disabled = true; });
  panel.classList.add("review-submitted");
  const error = panel.querySelector(".review-error");
  if (error && !error.textContent) error.textContent = "已提交，审核中…";
}

async function resumeRun(payload) {
  if (state.commitment.busy) return;
  // resume 必须发往审批面板绑定的任务（state.commitment.taskId），
  // 不能依赖 activeTask()——activeTask 可能因重启/切换任务与面板不一致
  const task = state.commitment.taskId
    ? state.tasks.find(item => item.task_id === state.commitment.taskId)
    : activeTask();
  if (!task) return setStatus("当前没有活动任务", true);
  state.commitment.busy = true;
  try {
    const run = await api(`/desktop/api/threads/${task.thread_id}/runs/resume`, {
      method: "POST",
      body: JSON.stringify({ resume: payload }),
    });
    listenToRun(run);
    state.commitment.review = null;
    state.commitment.recovery = null;
    state.commitment.recoveryRestored = false;
    removeReviewPanel();
  } catch (error) { setStatus(error.message, true); }
  finally { state.commitment.busy = false; }
}

function activeTaskHasCommitmentLock() {
  return state.commitment.taskId === state.activeTaskId
    && Boolean(state.commitment.review || state.commitment.recovery);
}

function reconcileCommitmentRecovery(detail) {
  const recovery = detail?.commitment_recovery || null;
  if (!recovery) {
    if (state.commitment.taskId && state.commitment.taskId !== state.activeTaskId) {
      setStatus("");
    }
    if (state.commitment.taskId === state.activeTaskId && state.commitment.recoveryRestored) {
      resetCommitment();
    }
    return;
  }
  if (state.commitment.taskId && state.commitment.taskId !== state.activeTaskId) {
    resetCommitment();
  }
  state.commitment.taskId = state.activeTaskId;
  state.commitment.stage = Number(recovery.stage || 0);
  state.commitment.recovery = recovery;
  state.commitment.review = detail.pending_commitment_review || null;
  state.commitment.recoveryRestored = true;
}

function mountCommitmentRecovery(detail) {
  const recovery = detail?.commitment_recovery;
  if (!recovery || state.commitment.taskId !== state.activeTaskId) return;
  if (recovery.status === "resumable" && detail.pending_commitment_review) {
    if (!state.commitment.reviewPanel?.isConnected
        || state.commitment.reviewPanel.dataset.recoveryStatus !== "resumable") {
      showReview(detail.pending_commitment_review);
      state.commitment.recoveryRestored = true;
    }
    return;
  }
  if (recovery.status === "processing") {
    if (state.commitment.reviewPanel?.dataset.recoveryStatus !== "processing") {
      removeReviewPanel();
      const fragment = document.querySelector("#reviewTemplate").content.cloneNode(true);
      const panel = fragment.querySelector(".review-panel");
      panel.dataset.stage = String(recovery.stage || 0);
      panel.dataset.recoveryStatus = "processing";
      panel.querySelector(".review-kicker").textContent = `第 ${recovery.stage} 步`;
      panel.querySelector("h3").textContent = "人工决定正在处理";
      panel.querySelector(".review-badge").textContent = "处理中";
      panel.querySelector(".review-draft").textContent = prettyDraft(recovery.review?.draft);
      panel.querySelector(".review-error").textContent = "正在从 checkpoint 继续执行，请等待新的审批或完成结果。";
      panel.querySelector(".review-actions").remove();
      panel.querySelector(".revision-form").remove();
      panel.querySelector(".review-contract-editor").remove();
      state.commitment.reviewPanel = panel;
      conversationNode()?.append(panel);
      setProgress(state.commitment.stage, true);
      setStatus("承诺审批处理中");
      scrollConversation();
    }
    return;
  }
  if (recovery.status !== "orphaned"
      || state.commitment.reviewPanel?.dataset.recoveryStatus === "orphaned") return;
  removeReviewPanel();
  state.commitment.review = null;
  const fragment = document.querySelector("#reviewTemplate").content.cloneNode(true);
  const panel = fragment.querySelector(".review-panel");
  panel.dataset.stage = String(recovery.stage || 0);
  panel.dataset.recoveryStatus = "orphaned";
  panel.querySelector(".review-kicker").textContent = `第 ${recovery.stage} 步`;
  panel.querySelector("h3").textContent = "旧承诺流程无法继续";
  panel.querySelector(".review-badge").textContent = "需要重开";
  panel.querySelector(".review-draft").textContent = prettyDraft(recovery.review?.draft);
  panel.querySelector(".review-error").textContent =
    "父图已经越过原来的人工中断，旧决定不能安全恢复。放弃后只会清理承诺子图，现有对话和材料都会保留。";
  panel.querySelector(".revision-form").remove();
  panel.querySelector(".review-contract-editor").remove();
  panel.querySelector(".review-actions").innerHTML =
    '<button class="primary" type="button" data-action="abandon-commitment">放弃旧流程并重开</button>';
  state.commitment.reviewPanel = panel;
  conversationNode()?.append(panel);
  setProgress(state.commitment.stage, true);
  setStatus("旧承诺流程需要显式重开", true);
  scrollConversation();
}

async function abandonCommitment() {
  if (state.commitment.busy) return;
  const task = state.commitment.taskId
    ? state.tasks.find(item => item.task_id === state.commitment.taskId)
    : activeTask();
  if (!task) return setStatus("当前没有活动任务", true);
  const restartMessage = state.commitment.recovery?.restart_message || "/commit ";
  state.commitment.busy = true;
  try {
    await api(`/desktop/api/threads/${task.thread_id}/commitment/abandon`, {
      method: "POST",
    });
    resetCommitment();
    await hydrateActive();
    render();
    const input = document.querySelector("#mainInput");
    if (input) {
      input.value = restartMessage;
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
    setStatus("旧承诺流程已放弃；请确认任务内容后重新发送");
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    state.commitment.busy = false;
  }
}

function restoreCommitmentPanels(conversation) {
  // 承诺面板/轨迹绑定任务：仅恢复属于当前渲染任务的流程
  if (!state.commitment.taskId || state.commitment.taskId !== state.activeTaskId) {
    setProgress(state.commitment.stage, false);
    return;
  }
  const panel = state.commitment.tracePanel;
  if (panel && state.commitment.handoffStarted) {
    conversation.prepend(panel);
  } else if (panel && !panel.isConnected) {
    conversation.insertBefore(panel, conversation.lastElementChild?.nextSibling || null);
  }
  // render() may create the review before restoring a detached trace panel.
  // Always append the review last so the actionable panel stays below the trace.
  if (state.commitment.reviewPanel) conversation.append(state.commitment.reviewPanel);
  if (state.commitment.tracePanel?.isConnected) {
    setProgress(
      state.commitment.stage,
      !state.commitment.terminalStatus && !state.commitment.handoffStarted,
    );
  }
}

function scrollConversation() {
  const conversation = conversationNode();
  if (!conversation) return;
  requestAnimationFrame(() => { conversation.scrollTop = conversation.scrollHeight; });
}

async function refreshTasks() { state.tasks = await api("/desktop/api/tasks"); }

async function createTask(event) {
  event.preventDefault();
  const path = document.querySelector("#workspacePath").value.trim();
  const title = document.querySelector("#threadTitle").value.trim();
  if (!path || !title) return;
  try {
    const workspace = await api("/desktop/api/workspaces", { method: "POST", body: JSON.stringify({ path }) });
    const task = await api(`/desktop/api/workspaces/${workspace.workspace_id}/threads`, { method: "POST", body: JSON.stringify({ title }) });
    dialog.close(); state.tasks.push(task); state.activeTaskId = task.task_id; state.view = "focus";
    await hydrateActive(); render();
  } catch (error) { setStatus(error.message, true); }
}

async function pickWorkspace() {
  try {
    const path = window.focusDesktop?.selectWorkspace ? await window.focusDesktop.selectWorkspace() : (await api("/desktop/api/workspaces/select", { method: "POST" })).path;
    if (path) document.querySelector("#workspacePath").value = path;
  } catch (error) { setStatus(error.message, true); }
}

async function handleMaterialAction(button) {
  const row = button.closest("[data-material-id]");
  const materialId = row.dataset.materialId;
  const material = (state.materials.get(state.activeTaskId) || []).find(item => item.material_id === materialId);
  if (button.dataset.action === "toggle-material") { state.openMaterial = state.openMaterial === materialId ? null : materialId; return renderFocus(); }
  if (button.dataset.action === "save-material") {
    const body = Object.fromEntries([...row.querySelectorAll("select[data-field]")].map(select => [select.dataset.field, select.value]));
    try {
      const saved = await api(`/desktop/api/materials/${materialId}`, { method: "PUT", body: JSON.stringify(body) });
      Object.assign(material, saved); renderFocus();
    } catch (error) {
      if (error.status === 409 && error.detail?.code === "git_init_required" && confirm("该文件夹还不是 Git 仓库。是否执行 git init 并启用不可遗失保护？")) {
        const saved = await api(`/desktop/api/materials/${materialId}`, { method: "PUT", body: JSON.stringify({ ...body, confirm_git_init: true }) });
        Object.assign(material, saved); renderFocus();
      } else setStatus(error.message, true);
    }
  }
  if (button.dataset.action === "clear-material" && confirm("保留文件路径并将内容置空？此操作会保存新版本。")) {
    Object.assign(material, await api(`/desktop/api/materials/${materialId}/clear`, { method: "POST" })); renderFocus();
  }
  if (button.dataset.action === "delete-material" && confirm("删除这个可移除文件？")) {
    await api(`/desktop/api/materials/${materialId}`, { method: "DELETE" });
    state.materials.set(state.activeTaskId, state.materials.get(state.activeTaskId).filter(item => item.material_id !== materialId)); renderFocus();
  }
  if (button.dataset.action === "load-versions") {
    const versions = await api(`/desktop/api/materials/${materialId}/versions`);
    row.querySelector("[data-versions]").innerHTML = versions.map(version => `<li><span>${escapeHtml(version.source)} · ${escapeHtml(version.created_at || "")}</span><button class="text-button" data-action="restore-version" data-version-id="${version.version_id}">恢复</button></li>`).join("") || "<li>尚无版本</li>";
  }
  if (button.dataset.action === "restore-version") {
    Object.assign(material, await api(`/desktop/api/materials/${materialId}/restore`, { method: "POST", body: JSON.stringify({ version_id: button.dataset.versionId }) })); renderFocus();
  }
}

async function switchTask(taskId) {
  if (state.view === "focus") await persistFocusState();
  state.activeTaskId = taskId;
  state.view = "focus";
  await hydrateActive();
  const projectionStatus = state.details.get(taskId)?.context?.projection_status || "root";
  if (!["root", "valid", "repaired", "approved"].includes(projectionStatus)) return reopenContextDecision(taskId);
  render();
}

document.addEventListener("click", async event => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  if (action === "reload") return bootstrap();
  if (action === "new-task") return dialog.showModal();
  if (action === "pick-workspace") return pickWorkspace();
  if (action === "select-skill") return selectSkill(button.dataset.pickerKind, button.dataset.skillName);
  if (action === "select-commit") return selectCommitCommand(button.dataset.pickerKind);
  if (action === "remove-skill") return removeSkill(button.dataset.pickerKind, button.dataset.skillName);
  if (action === "show-map") { persistFocusState(); state.view = "map"; return render(); }
  if (action === "focus-home" && state.activeTaskId) { state.view = "focus"; await hydrateActive(); return render(); }
  if (action === "derive-context") return openContextEditor(state.activeTaskId);
  if (action === "edit-context-definition") return reopenContextDecision(button.dataset.contextId);
  if (action === "context-rail-card") return switchTask(button.dataset.taskId);
  if (action === "resume-context-decision") return reopenContextDecision(state.activeTaskId);
  if (action === "exit-context-editor") { state.contextDraft = null; state.view = "map"; return render(); }
  if (action === "add-context-source") return addContextSource(button.dataset.contextId);
  if (action === "context-source-select") {
    state.contextDraft.activeSourceId = button.dataset.contextId;
    return renderContextSourcePanel();
  }
  if (action === "remove-context-source") {
    try {
      const draft = syncContextDraft();
      const [removed] = draft.sources.splice(Number(button.dataset.sourceIndex), 1);
      draft.sourceSnapshots = draft.sourceSnapshots.filter(source => source.context_id !== removed.context_id);
      if (draft.activeSourceId === removed.context_id) draft.activeSourceId = draft.sources[0]?.context_id || null;
      return renderContextSourcePanel();
    } catch (error) { return setStatus(error.message, true); }
  }
  if (action === "context-source-copy") {
    try {
      const row = button.closest("[data-context-source-index]");
      const source = state.contextDraft.sourceSnapshots.find(item => item.context_id === row.dataset.sourceContextId);
      return insertContextMessage(source.messages[Number(row.dataset.contextSourceIndex)], state.contextDraft.messages.length);
    } catch (error) { return setStatus(error.message, true); }
  }
  if (action === "context-message-add") {
    try { return insertContextMessage({ role: "human", content: "" }, state.contextDraft.messages.length, { expand: true, focus: true }); }
    catch (error) { return setStatus(error.message, true); }
  }
  if (action === "context-message-clear") {
    try { return clearContextMessages(); }
    catch (error) { return setStatus(error.message, true); }
  }
  if (action === "context-message-toggle") return toggleContextMessage(button.closest("[data-context-ui-key]").dataset.contextUiKey);
  if (action === "context-message-undo") return undoContextMessageDelete();
  if (["context-message-copy", "context-message-delete"].includes(action)) {
    try {
      const index = Number(button.closest("[data-context-message-index]").dataset.contextMessageIndex);
      if (action === "context-message-copy") return insertContextMessage(state.contextDraft.messages[index], index + 1);
      return deleteContextMessage(index);
    } catch (error) { return setStatus(error.message, true); }
  }
  if (action === "submit-context") return submitContext();
  if (action === "accept-context-projection") return decideContextProjection("accept");
  if (action === "cancel-context-projection") return decideContextProjection("reject");
  if (action === "edit-context-projection") return renderContextEditor();
  if (action === "open-derived-context") return openDerivedContext();
  if (action === "arm-soldier") { state.soldierArmed = !state.soldierArmed; return renderMap(); }
  if (action === "task-card") {
    const taskId = button.dataset.taskId;
    if (state.view === "draft") return;
    if (state.soldierArmed) return openDraft(taskId);
    return switchTask(taskId);
  }
  if (action === "send-main") return sendMain();
  if (action === "abandon-commitment") return abandonCommitment();
  if (action === "exit-draft") { await saveDraft(); state.view = "map"; return render(); }
  if (action === "deploy") return deployDraft();
  if (action === "add-message") { syncDraftFromDom().history_messages.push({ role: "human", content: "" }); renderDraft(); scheduleDraftSave(); return; }
  if (action === "clear-history") { syncDraftFromDom().history_messages = []; renderDraft(); scheduleDraftSave(); return; }
  if (action === "delete-message") {
    const draft = syncDraftFromDom(); const index = Number(button.closest(".message-editor").dataset.index); const message = draft.history_messages[index];
    const callIds = new Set((message.tool_calls || []).map(call => call.id));
    if (message.role === "tool") callIds.add(message.tool_call_id);
    draft.history_messages = draft.history_messages.filter((item, itemIndex) => itemIndex !== index && !callIds.has(item.tool_call_id) && !(item.tool_calls || []).some(call => callIds.has(call.id)));
    renderDraft(); scheduleDraftSave(); return;
  }
  if (button.closest("[data-material-id]")) return handleMaterialAction(button);
  if (action === "agent-menu" || action === "debug-agent-menu") return debugAgentMenu(button.dataset.agentId);
  if (action === "agent-details") return openAgentDetails(button.dataset.agentId);
  if (action === "close-agent-details") return agentDialog.close();
  if (action === "refresh-agent-details") return refreshAgentDetails();
  if (action === "retry-agent-details") return retryAgentDetails();
  if (action === "cancel-agent-details") return cancelAgentDetails();
  if (action === "interrupt-main-run") return interruptMainRun();
});

document.addEventListener("input", event => {
  if (event.target.matches("[data-skill-input]")) updateSkillMenu(event.target, true);
  if (event.target.matches("[data-draft-field],[data-message-field],[data-equipment],[data-permission]")) scheduleDraftSave();
});

document.addEventListener("keydown", event => {
  if (event.key === "Escape" && contextPointerDrag) {
    event.preventDefault();
    return cancelContextPointerDrag();
  }
  const contextHandle = event.target.closest?.('[data-context-pointer-handle][data-context-drag-origin="draft"]');
  if (contextHandle && event.altKey && ["ArrowUp", "ArrowDown"].includes(event.key)) {
    event.preventDefault();
    const index = Number(contextHandle.closest("[data-context-message-index]").dataset.contextMessageIndex);
    const target = index + (event.key === "ArrowUp" ? -1 : 1);
    try {
      if (target >= 0 && target < state.contextDraft.messages.length) {
        moveContextMessage(index, target, true);
      }
    } catch (error) { setStatus(error.message, true); }
    return;
  }
  const input = event.target.closest("[data-skill-input]");
  if (!input) return;
  const action = skillPicker.keyAction(event.key);
  if (!action || pickerMatches(input) === null) return;
  const kind = input.dataset.skillInput;
  if (action === "close") {
    event.preventDefault();
    document.querySelector(`#${kind}SkillList`).hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    return;
  }
  const matches = pickerMatches(input);
  const query = skillPicker.queryFromInput(input.value) || "";
  const commitCount = commitCommandVisible(query) ? 1 : 0;
  if (action === "next" || action === "previous") {
    event.preventDefault();
    state.pickerActive[kind] = skillPicker.moveActive(
      state.pickerActive[kind], action === "next" ? 1 : -1, commitCount + matches.length
    );
    updateSkillMenu(input);
  } else if (action === "select") {
    event.preventDefault();
    if (state.pickerActive[kind] < commitCount) return selectCommitCommand(kind);
    const skillIndex = state.pickerActive[kind] - commitCount;
    if (matches[skillIndex]) selectSkill(kind, matches[skillIndex].name);
  }
});

document.addEventListener("toggle", event => {
  if (!event.target.matches(".draft-section") || !event.target.open) return;
  state.openDraftSection = event.target.dataset.section;
  document.querySelectorAll(".draft-section").forEach(section => { if (section !== event.target) section.open = false; });
}, true);

function beginContextPointerDrag(drag, event) {
  try { syncContextDraft(); }
  catch (error) { setStatus(error.message, true); return cancelContextPointerDrag(); }
  const rect = drag.card.getBoundingClientRect();
  drag.started = true;
  drag.offsetX = Math.max(18, Math.min(event.clientX - rect.left, rect.width - 18));
  drag.offsetY = Math.max(18, Math.min(event.clientY - rect.top, rect.height - 18));
  drag.preview = drag.card.cloneNode(true);
  drag.preview.className = "context-drag-preview";
  drag.preview.removeAttribute("data-context-message-index");
  drag.preview.querySelectorAll("button, textarea, details").forEach(node => { node.tabIndex = -1; });
  drag.preview.style.width = `${rect.width}px`;
  drag.placeholder = document.createElement("div");
  drag.placeholder.className = "context-drop-placeholder";
  drag.placeholder.style.height = `${Math.min(rect.height, 180)}px`;
  drag.card.classList.add("is-lifted");
  document.body.append(drag.preview);
  document.querySelector(".context-message-list")?.classList.add("is-drag-active");
  positionContextDragPreview(event.clientX, event.clientY);
  drag.autoFrame = requestAnimationFrame(runContextAutoScroll);
}

function positionContextDragPreview(clientX, clientY) {
  if (!contextPointerDrag?.preview) return;
  contextPointerDrag.clientX = clientX;
  contextPointerDrag.clientY = clientY;
  contextPointerDrag.preview.style.transform = `translate3d(${clientX - contextPointerDrag.offsetX}px, ${clientY - contextPointerDrag.offsetY}px, 0) scale(1.015)`;
}

function updateContextDropTarget(clientX, clientY) {
  const drag = contextPointerDrag;
  const list = document.querySelector(".context-message-list");
  if (!drag?.started || !list) return;
  const rect = list.getBoundingClientRect();
  const inside = clientX >= rect.left && clientX <= rect.right && clientY >= rect.top && clientY <= rect.bottom;
  if (!inside) {
    drag.dropIndex = null;
    drag.placeholder?.remove();
    return;
  }
  const rows = [...list.querySelectorAll(".context-message-editor")].filter(row => row !== drag.card);
  let index = rows.findIndex(row => clientY < row.getBoundingClientRect().top + row.getBoundingClientRect().height / 2);
  if (index < 0) index = rows.length;
  if (drag.dropIndex === index && drag.placeholder?.isConnected) return;
  const previous = contextListPositions();
  drag.dropIndex = index;
  list.insertBefore(drag.placeholder, rows[index] || null);
  animateContextReflow(previous);
}

function scrollContextPanelNearPointer(clientY) {
  const panel = document.querySelector(".context-definition-panel");
  const rect = panel?.getBoundingClientRect();
  if (panel && rect) {
    const edge = 58;
    const delta = clientY < rect.top + edge ? -12 : clientY > rect.bottom - edge ? 12 : 0;
    if (delta) {
      panel.scrollTop += delta;
      return true;
    }
  }
  return false;
}

function runContextAutoScroll() {
  const drag = contextPointerDrag;
  if (!drag?.started) return;
  if (scrollContextPanelNearPointer(drag.clientY)) updateContextDropTarget(drag.clientX, drag.clientY);
  drag.autoFrame = requestAnimationFrame(runContextAutoScroll);
}

function cleanupContextPointerDrag(keepPreview = false) {
  const drag = contextPointerDrag;
  if (!drag) return null;
  contextPointerDrag = null;
  cancelAnimationFrame(drag.autoFrame);
  drag.placeholder?.remove();
  drag.card?.classList.remove("is-lifted");
  document.querySelector(".context-message-list")?.classList.remove("is-drag-active");
  if (!keepPreview) drag.preview?.remove();
  try {
    if (drag.handle.hasPointerCapture?.(drag.pointerId)) drag.handle.releasePointerCapture(drag.pointerId);
  } catch {}
  return drag;
}

function cancelContextPointerDrag() {
  const drag = cleanupContextPointerDrag(true);
  if (!drag?.preview) return;
  if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return drag.preview.remove();
  const remove = () => drag.preview.remove();
  drag.preview.animate([{ opacity: 1 }, { opacity: 0, transform: `${drag.preview.style.transform} scale(.96)` }], { duration: 120, easing: "ease-out" }).finished.finally(remove);
  setTimeout(remove, 150);
}

function settleContextPointerPreview(drag, uiKey) {
  if (!drag.preview) return;
  const target = document.querySelector(`[data-context-ui-key="${uiKey}"]`);
  if (!target || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return drag.preview.remove();
  const rect = target.getBoundingClientRect();
  const remove = () => drag.preview.remove();
  drag.preview.animate([
    { transform: drag.preview.style.transform, opacity: 1 },
    { transform: `translate3d(${rect.left}px, ${rect.top}px, 0) scale(1)`, opacity: .2 },
  ], { duration: 170, easing: "cubic-bezier(.2,.8,.2,1)" }).finished.finally(remove);
  setTimeout(remove, 200);
}

document.addEventListener("pointerdown", event => {
  const handle = event.target.closest?.("[data-context-pointer-handle]");
  if (!handle || handle.disabled || event.button !== 0 || state.view !== "context") return;
  const origin = handle.dataset.contextDragOrigin;
  const card = origin === "source" ? handle.closest(".context-source-message") : handle.closest(".context-message-editor");
  if (!card) return;
  event.preventDefault();
  contextPointerDrag = {
    pointerId: event.pointerId,
    handle,
    card,
    origin,
    uiKey: card.dataset.contextUiKey,
    sourceContextId: card.dataset.sourceContextId,
    sourceIndex: Number(card.dataset.contextSourceIndex),
    startX: event.clientX,
    startY: event.clientY,
    clientX: event.clientX,
    clientY: event.clientY,
    started: false,
    dropIndex: null,
  };
  try { handle.setPointerCapture(event.pointerId); } catch {}
});

document.addEventListener("pointermove", event => {
  const drag = contextPointerDrag;
  if (!drag || event.pointerId !== drag.pointerId) return;
  if (!drag.started && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 5) return;
  event.preventDefault();
  if (!drag.started) beginContextPointerDrag(drag, event);
  if (!contextPointerDrag) return;
  positionContextDragPreview(event.clientX, event.clientY);
  updateContextDropTarget(event.clientX, event.clientY);
  if (scrollContextPanelNearPointer(event.clientY)) updateContextDropTarget(event.clientX, event.clientY);
});

document.addEventListener("pointerup", event => {
  const current = contextPointerDrag;
  if (!current || event.pointerId !== current.pointerId) return;
  if (!current.started || current.dropIndex == null) return cancelContextPointerDrag();
  const dropIndex = current.dropIndex;
  const drag = cleanupContextPointerDrag(true);
  try {
    let uiKey;
    if (drag.origin === "source") {
      const source = state.contextDraft.sourceSnapshots.find(item => item.context_id === drag.sourceContextId);
      uiKey = insertContextMessage(source.messages[drag.sourceIndex], dropIndex);
    } else {
      const from = state.contextDraft.uiKeys.indexOf(drag.uiKey);
      const to = Math.max(0, Math.min(dropIndex, state.contextDraft.messages.length - 1));
      moveContextMessage(from, to);
      uiKey = drag.uiKey;
    }
    settleContextPointerPreview(drag, uiKey);
  } catch (error) {
    drag.preview?.remove();
    setStatus(error.message, true);
  }
});

document.addEventListener("pointercancel", event => {
  if (contextPointerDrag?.pointerId === event.pointerId) cancelContextPointerDrag();
});

document.addEventListener("lostpointercapture", event => {
  if (contextPointerDrag?.pointerId === event.pointerId) cancelContextPointerDrag();
});

document.addEventListener("dragstart", event => {
  if (event.target.matches(".soldier-source")) event.dataTransfer.setData("application/x-focus-soldier", "new");
  const row = event.target.closest(".message-editor");
  if (row) event.dataTransfer.setData("application/x-focus-message", row.dataset.index);
});

document.addEventListener("dragover", event => {
  const card = event.target.closest(".task-card");
  if (card && event.dataTransfer.types.includes("application/x-focus-soldier")) { event.preventDefault(); card.classList.add("drop-target"); }
  const row = event.target.closest(".message-editor");
  if (row && event.dataTransfer.types.includes("application/x-focus-message")) event.preventDefault();
});

document.addEventListener("dragleave", event => event.target.closest(".task-card")?.classList.remove("drop-target"));
document.addEventListener("drop", event => {
  const card = event.target.closest(".task-card");
  if (card && event.dataTransfer.getData("application/x-focus-soldier")) { event.preventDefault(); return openDraft(card.dataset.taskId); }
  const row = event.target.closest(".message-editor");
  const from = Number(event.dataTransfer.getData("application/x-focus-message"));
  if (row && Number.isInteger(from)) {
    event.preventDefault(); const draft = syncDraftFromDom(); moveMessageGroup(draft.history_messages, from, Number(row.dataset.index)); renderDraft(); scheduleDraftSave();
  }
});

function messageGroup(messages, index) {
  const message = messages[index];
  const callIds = new Set((message.tool_calls || []).map(call => call.id));
  if (message.role === "tool") {
    callIds.add(message.tool_call_id);
    const parent = messages.find(item => (item.tool_calls || []).some(call => call.id === message.tool_call_id));
    (parent?.tool_calls || []).forEach(call => callIds.add(call.id));
  }
  if (!callIds.size) return [index];
  return messages.flatMap((item, itemIndex) => {
    const related = callIds.has(item.tool_call_id) || (item.tool_calls || []).some(call => callIds.has(call.id));
    return related ? [itemIndex] : [];
  });
}

function moveMessageGroup(messages, from, to) {
  const moving = new Set(messageGroup(messages, from));
  const target = messageGroup(messages, to);
  const group = messages.filter((_item, index) => moving.has(index));
  const insertion = messages.slice(0, Math.min(...target)).filter((_item, index) => !moving.has(index)).length;
  const remaining = messages.filter((_item, index) => !moving.has(index));
  remaining.splice(insertion, 0, ...group);
  messages.splice(0, messages.length, ...remaining);
}

document.querySelector("#taskForm").addEventListener("submit", createTask);
document.querySelector("#agentContinueForm").addEventListener("submit", event => { event.preventDefault(); return continueAgentDetails(); });
document.querySelector("#fileInput")?.addEventListener("change", () => {});
document.addEventListener("change", async event => {
  if (event.target.id !== "fileInput" || !event.target.files[0]) return;
  const body = new FormData(); body.append("file", event.target.files[0]);
  try {
    await api(`/desktop/api/tasks/${state.activeTaskId}/materials/upload`, { method: "POST", body });
    state.materials.set(state.activeTaskId, await api(`/desktop/api/tasks/${state.activeTaskId}/materials`)); renderFocus();
  } catch (error) { setStatus(error.message, true); }
});

function persistFocusState() {
  const detail = state.details.get(state.activeTaskId);
  if (!detail) return;
  detail.ui_state = {
    ...(detail.ui_state || {}),
    input: document.querySelector("#mainInput")?.value || "",
    skills: selectedSkills("main"),
    scrollTop: document.querySelector("#conversation")?.scrollTop || 0,
  };
  return api(`/desktop/api/tasks/${state.activeTaskId}/ui-state`, { method: "PUT", body: JSON.stringify(detail.ui_state) }).catch(() => {});
}

bootstrap();
