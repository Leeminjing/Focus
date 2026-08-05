"use strict";

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
  equipment: { models: [], tools: [], skills: [], permissions: [] },
  soldierArmed: false,
  openMaterial: null,
  openDraftSection: "history",
  saveTimer: null,
  statusTimer: null,
  deploying: false,
  streams: new Map(),
  streamBuffers: new Map(),
  streamFrames: new Map(),
};

const app = document.querySelector("#app");
const statusNode = document.querySelector("#globalStatus");
const dialog = document.querySelector("#taskDialog");

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
  const blocks = [];
  let paragraph = [];
  let list = [];
  let listType = null;
  const flushParagraph = () => {
    if (paragraph.length) blocks.push(`<p>${paragraph.map(escapeHtml).join("<br>")}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (list.length) blocks.push(`<${listType}>${list.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</${listType}>`);
    list = [];
    listType = null;
  };
  for (const rawLine of String(value).replace(/\r\n?/g, "\n").split("\n")) {
    const unordered = rawLine.match(/^\s*[-*+•]\s+(.+)$/);
    const ordered = rawLine.match(/^\s*\d+[.)]\s+(.+)$/);
    if (unordered || ordered) {
      flushParagraph();
      const nextType = unordered ? "ul" : "ol";
      if (listType && listType !== nextType) flushList();
      listType = nextType;
      list.push((unordered || ordered)[1]);
    } else if (!rawLine.trim()) {
      flushParagraph();
      flushList();
    } else {
      flushList();
      paragraph.push(rawLine.trim());
    }
  }
  flushParagraph();
  flushList();
  return `<div class="message-rich">${blocks.join("")}</div>`;
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
  const [detail, materials, agents] = await Promise.all([
    api(`/desktop/api/tasks/${state.activeTaskId}`),
    api(`/desktop/api/tasks/${state.activeTaskId}/materials`),
    api(`/desktop/api/tasks/${state.activeTaskId}/agents`),
  ]);
  state.details.set(state.activeTaskId, detail);
  state.materials.set(state.activeTaskId, materials);
  state.agents.set(state.activeTaskId, agents);
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
  else renderDraft();
}

function renderFocus() {
  const task = activeTask();
  const detail = state.details.get(task.task_id) || { messages: [] };
  const materials = state.materials.get(task.task_id) || [];
  const previousConversation = app.dataset.taskId === task.task_id ? document.querySelector("#conversation") : null;
  const wasPinned = previousConversation && previousConversation.scrollHeight - previousConversation.scrollTop - previousConversation.clientHeight < 80;
  const previousScrollTop = previousConversation?.scrollTop;
  app.innerHTML = `
    <section class="focus-view" data-task-id="${task.task_id}">
      <div class="conversation" id="conversation">
        ${renderConversation(detail, task)}
      </div>
      <div class="focus-bottom">
        <div class="composer">
          <textarea id="mainInput" aria-label="任务输入" placeholder="继续输入任务…">${escapeHtml(detail.ui_state?.input || "")}</textarea>
          <div class="composer-actions"><label class="attach-button">添加文件<input id="fileInput" type="file" hidden></label><button class="send-button" data-action="send-main">发送</button></div>
        </div>
        <section class="materials ${materials.length ? "" : "is-empty"}">${materials.length ? materials.map(renderMaterial).join("") : `<div class="materials-empty">暂无材料</div>`}</section>
        <div class="agents-strip">${renderAgentStrip(task.task_id)}</div>
      </div>
    </section>`;
  app.dataset.taskId = task.task_id;
  const conversation = document.querySelector("#conversation");
  conversation.scrollTop = previousConversation
    ? (wasPinned ? conversation.scrollHeight : previousScrollTop)
    : (detail.ui_state?.scrollTop ?? conversation.scrollHeight);
}

function renderMessage(message) {
  const role = { human: "你", user: "你", ai: "助手", assistant: "助手", system: "System", tool: "Tool" }[message.role] || message.role;
  const kind = { human: "human", user: "human", ai: "ai", assistant: "ai", system: "system", tool: "tool" }[message.role] || "system";
  const content = typeof message.content === "string" ? message.content : JSON.stringify(message.content, null, 2);
  const rendered = kind === "ai" ? renderAssistantContent(content) : escapeHtml(content);
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
  if (pinned) conversation.scrollTop = conversation.scrollHeight;
}

function appendToken(envelope) {
  const task = state.tasks.find(item => item.thread_id === envelope.thread_id && item.workspace_id === envelope.workspace_id);
  const content = envelope.data?.content;
  if (!task || !content || !envelope.agent_id.startsWith("main:")) return;
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
  return warning + agents.map(agent => `<button class="agent-chip" data-action="agent-menu" data-agent-id="${agent.agent_id}">小兵 ${agent.agent_id.slice(0, 5)} · ${agent.latest_run?.status || "ready"}</button>`).join("");
}

function taskCards(draftMode = false) {
  const draft = state.drafts.get(state.activeTaskId);
  return state.tasks.map(task => {
    const selected = draftMode && draft?.task_id === task.task_id;
    const status = task.active_run?.status;
    return `<button class="task-card ${draftMode && !selected ? "dimmed" : ""}" data-task-id="${task.task_id}" data-action="task-card">
      <span class="task-title">${escapeHtml(task.title)}</span>
      <span class="task-path">${escapeHtml(task.workspace_name)} · ${escapeHtml(task.thread_id)}</span>
      <span class="task-status ${status === "running" ? "running" : ""}">${status === "running" ? "主 Agent 运行中" : escapeHtml(task.workspace_path)}</span>
    </button>`;
  }).join("");
}

function renderMap() {
  app.innerHTML = `<section class="map-view">
    <div class="map-toolbar"><button class="soldier-source" draggable="true" aria-pressed="${state.soldierArmed}" data-action="arm-soldier">小兵 · 拖向任务</button></div>
    <div class="task-grid">${taskCards()}</div>
  </section>`;
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
        ${draftSection("final", "3. 最后一条 HumanMessage", `<textarea data-draft-field="final_human_message" placeholder="给小兵的任务…">${escapeHtml(draft.final_human_message)}</textarea>`)}
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
    <label>工具<select data-equipment="tools"><option value="auto" ${equipment.tools === "auto" ? "selected" : ""}>自动组装</option><option value="custom" ${Array.isArray(equipment.tools) ? "selected" : ""}>手动选择</option></select></label>
    <div><span class="tiny muted">权限</span><div class="check-line">${state.equipment.permissions.map(permission => `<label><input type="checkbox" data-permission="${permission}" ${permissions.includes(permission) ? "checked" : ""}>${permission}</label>`).join("")}</div></div>
    <div><span class="tiny muted">可用工具</span><div class="check-line">${state.equipment.tools.map(name => `<label><input type="checkbox" data-tool="${name}" ${equipment.tools === "auto" || equipment.tools?.includes?.(name) ? "checked" : ""}>${name}</label>`).join("")}</div></div>
    <div><span class="tiny muted">技能</span><div class="check-line">${state.equipment.skills.length ? state.equipment.skills.map(name => `<label><input type="checkbox" data-skill="${name}" checked>${name}</label>`).join("") : `<span class="muted tiny">当前没有启用的技能</span>`}</div></div>
    <p class="tiny danger">无沙箱：写入或命令权限会直接影响真实宿主机。命令权限可绕过文件工具规则。</p>
  </div>`;
}

async function openDraft(taskId) {
  setStatus("复制 checkpoint…");
  try {
    const draft = await api(`/desktop/api/tasks/${taskId}/drafts/open`, { method: "POST" });
    state.activeTaskId = taskId;
    state.drafts.set(taskId, draft);
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
  const toolMode = document.querySelector('[data-equipment="tools"]');
  if (toolMode) draft.equipment.tools = toolMode.value === "auto" ? "auto" : [...document.querySelectorAll("[data-tool]:checked")].map(input => input.dataset.tool);
  draft.equipment.permissions = [...document.querySelectorAll("[data-permission]:checked")].map(input => input.dataset.permission);
  draft.equipment.skills = state.equipment.skills.length ? [...document.querySelectorAll("[data-skill]:checked")].map(input => input.dataset.skill) : "auto";
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
  const input = document.querySelector("#mainInput");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  const detail = state.details.get(state.activeTaskId);
  detail.messages = [...(detail.messages || []), { role: "human", content: message }];
  renderFocus();
  try {
    const run = await api(`/desktop/api/tasks/${state.activeTaskId}/main/runs`, { method: "POST", body: JSON.stringify({ message }) });
    listenToRun(run);
  } catch (error) { setStatus(error.message, true); }
}

function listenToRun(run) {
  if (state.streams.has(run.run_id)) return;
  const source = new EventSource(`${runtime.apiBase}/desktop/api/runs/${run.run_id}/stream?session=${encodeURIComponent(runtime.session)}`);
  state.streams.set(run.run_id, source);
  source.addEventListener("tokens", event => appendToken(JSON.parse(event.data)));
  source.addEventListener("events", event => {
    const envelope = JSON.parse(event.data);
    const messages = envelope.data?.messages;
    const task = state.tasks.find(item => item.thread_id === envelope.thread_id && item.workspace_id === envelope.workspace_id);
    if (task && messages && envelope.agent_id.startsWith("main:")) {
      clearStreamBuffer(run.run_id);
      replaceConversation(task, messages);
    }
  });
  source.addEventListener("error", event => { if (event.data) setStatus(JSON.parse(event.data).data?.error || "运行失败", true); });
  source.addEventListener("end", async () => {
    source.close(); state.streams.delete(run.run_id); clearStreamBuffer(run.run_id); await refreshTasks();
    if (state.activeTaskId) await hydrateActive();
    render();
  });
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

document.addEventListener("click", async event => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  if (action === "reload") return bootstrap();
  if (action === "new-task") return dialog.showModal();
  if (action === "pick-workspace") return pickWorkspace();
  if (action === "show-map") { persistFocusState(); state.view = "map"; return render(); }
  if (action === "focus-home" && state.activeTaskId) { state.view = "focus"; await hydrateActive(); return render(); }
  if (action === "arm-soldier") { state.soldierArmed = !state.soldierArmed; return renderMap(); }
  if (action === "task-card") {
    const taskId = button.dataset.taskId;
    if (state.view === "draft") return;
    if (state.soldierArmed) return openDraft(taskId);
    state.activeTaskId = taskId; state.view = "focus"; await hydrateActive(); return render();
  }
  if (action === "send-main") return sendMain();
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
  if (action === "agent-menu") {
    const choice = prompt("输入操作：history / retry / continue / cancel", "history");
    const agentId = button.dataset.agentId;
    if (choice === "history") alert(JSON.stringify(await api(`/desktop/api/agents/${agentId}/history`), null, 2));
    if (choice === "retry") listenToRun(await api(`/desktop/api/agents/${agentId}/retry`, { method: "POST" }));
    if (choice === "continue") { const message = prompt("继续对话内容"); if (message) listenToRun(await api(`/desktop/api/agents/${agentId}/continue`, { method: "POST", body: JSON.stringify({ message }) })); }
    if (choice === "cancel") { const agent = (state.agents.get(state.activeTaskId) || []).find(item => item.agent_id === agentId); if (agent?.latest_run) await api(`/desktop/api/runs/${agent.latest_run.run_id}/cancel`, { method: "POST" }); }
  }
});

document.addEventListener("input", event => {
  if (event.target.matches("[data-draft-field],[data-message-field],[data-equipment],[data-permission],[data-tool],[data-skill]")) scheduleDraftSave();
});

document.addEventListener("toggle", event => {
  if (!event.target.matches(".draft-section") || !event.target.open) return;
  state.openDraftSection = event.target.dataset.section;
  document.querySelectorAll(".draft-section").forEach(section => { if (section !== event.target) section.open = false; });
}, true);

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
  detail.ui_state = { input: document.querySelector("#mainInput")?.value || "", scrollTop: document.querySelector("#conversation")?.scrollTop || 0 };
  api(`/desktop/api/tasks/${state.activeTaskId}/ui-state`, { method: "PUT", body: JSON.stringify(detail.ui_state) }).catch(() => {});
}

bootstrap();
