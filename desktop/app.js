/*
 * 本文件对外提供 Focus 桌面宿主的状态协调与原生 DOM 渲染。输入为同源 desktop API、SSE、
 * preload 运行时信息和用户操作，输出为持久导航、任务工作区、检查器、常驻会话 Patrol 小兵、
 * 对话/Context/Agent/Commitment/压缩/插件等视图；工作流在任务列表替换时统一归一化活动
 * Context，并通过单一异步事件边界更新宿主挂载点及保留任务级草稿、滚动、化身位置与运行状态。
 */
"use strict";

// markdown-it 单例(渲染器无状态):完成态助手消息的 md 渲染引擎。
// html:false 转义原始 HTML;validateLink 放行 file:// 协议(本地文件链接,
// 点击经全局拦截器打开右侧文件面板,不触发窗口导航);vendor 文件先行加载。
const mdRenderer = window.markdownit({ html: false, linkify: true });
mdRenderer.validateLink = url => /^(https?:|file:)/i.test(url);
const keywordCommand = window.FocusKeywordCommand;

const runtime = window.focusDesktop?.runtime?.() || {
  apiBase: location.protocol === "file:" ? "http://127.0.0.1:8765" : location.origin,
  session: "focus-dev-session",
};

const MAP_VIEW_PREFERENCES_KEY = "focus-map-view-v1";

function readMapViewPreferences() {
  const fallback = { mode: "tree", workspaceIds: [], contextIds: [] };
  try {
    const raw = localStorage.getItem(MAP_VIEW_PREFERENCES_KEY);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw);
    return {
      mode: ["tree", "cards"].includes(parsed?.mode) ? parsed.mode : fallback.mode,
      workspaceIds: Array.isArray(parsed?.workspaceIds) ? parsed.workspaceIds.filter(id => typeof id === "string") : [],
      contextIds: Array.isArray(parsed?.contextIds) ? parsed.contextIds.filter(id => typeof id === "string") : [],
    };
  } catch { return fallback; }
}

const mapViewPreferences = readMapViewPreferences();

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
  selectionMode: false,
  selectedContextIds: new Set(),
  mapViewMode: mapViewPreferences.mode,
  mapExpandedWorkspaceIds: new Set(mapViewPreferences.workspaceIds),
  mapExpandedContextIds: new Set(mapViewPreferences.contextIds),
  mapExpandedForTaskId: null,
  mapTreeFocusKey: "",
  openMaterial: null,
  openDraftSection: null,
  agentDetails: { agentId: null, messages: [], curation: null, busy: false },
  saveTimer: null,
  draftSaveRevisions: new Map(),
  creatingTask: false,
  statusTimer: null,
  deploying: false,
  quickCuratingTaskIds: new Set(),
  mainInterrupting: false,
  mainSubmitting: false,
  streams: new Map(),
  streamBuffers: new Map(),
  streamFrames: new Map(),
  composerErrors: new Map(),
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
  compression: {
    taskId: null,
    request: null,
    messages: [],
    selected: new Set(),
    ranges: [],
    busy: false,
    recovery: null,
  },
  plugins: { plugins: [], interfaces: {}, traces: [], filter: "all", selectedName: null },
  memory: { memories: [], selectedId: null, composing: false, draft: null, sessions: [], activeSessionId: null, enabledMessages: [], selectedMessageIds: [], activeMessageId: null, collectedSources: [], textSelection: "", textMessageId: null, textRange: null, editorRatio: 0.5, sourceRatio: 0.5, contentMode: "complete", segments: [], expandedGroups: [], mergeMode: false, selectedSourcesForMerge: [], sessionScrollTop: 0 },
  inspector: { open: window.innerWidth > 1100, tab: "context", returnFocus: null },
  filesPanel: null,   // f18:右侧文件面板当前打开的 material(relative_path 等)
  panelWidth: normalizePanelWidth(localStorage.getItem("focus-panel-width") || 400),
};

const app = document.querySelector("#app");
const statusNode = document.querySelector("#globalStatus");
const appInspector = document.querySelector("#appInspector");
const inspectorContent = document.querySelector("#inspectorContent");
const shellTaskTitle = document.querySelector("#shellTaskTitle");
const shellTaskMeta = document.querySelector("#shellTaskMeta");
const dialog = document.querySelector("#taskDialog");
const settingsDialog = document.querySelector("#settingsDialog");
const skillPicker = window.FocusSkillPicker;
const contextEditor = window.FocusContextEditor;
const compressionPanel = window.FocusCompressionPanel;
const pluginView = window.FocusPluginView;
const mapCollapsibleView = window.FocusMapCollapsibleView;
const memoryView = window.FocusMemoryView;
const conversationEvents = window.FocusConversationEvents;
const patrolPresence = window.FocusPatrolPresence;
const contextCuratorPresentation = window.FocusContextCuratorPresentation;
const patrolAvatar = window.FocusPatrolAvatar;
// f18 插件视图宿主:插件前端脚本加载后经此注册视图与材料打开器
window.__focusPluginViews = window.__focusPluginViews || {};
const pluginViews = window.__focusPluginViews;
let contextUiSequence = 0;
let contextRequestSequence = 0;
let contextPointerDrag = null;
let contextUndoTimer = null;
let agentDetailsRequestSequence = 0;
let taskSwitchSequence = 0;
let compressionRequestSequence = 0;
let draftOpenRequestSequence = 0;
let pluginHydrationSequence = 0;
let pluginViewRequestSequence = 0;
let memoryViewRequestSequence = 0;
let memoryHydrationSequence = 0;
let contextTreeRequestSequence = 0;
let panelResizeFrame = null;
let patrolAvatarController = null;
const pluginStyleAssets = new Set();
const pluginScriptAssets = new Map();

function normalizePanelWidth(value, viewportWidth = window.innerWidth) {
  const viewport = Number.isFinite(Number(viewportWidth)) && Number(viewportWidth) > 0
    ? Number(viewportWidth)
    : 1200;
  const navigationWidth = viewport <= 1100 ? 72 : 188;
  const workspaceWidth = Math.max(600, viewport - navigationWidth);
  const maximum = Math.max(280, Math.min(720, workspaceWidth - 360));
  const numeric = Number(value);
  return Math.round(Math.min(maximum, Math.max(280, Number.isFinite(numeric) ? numeric : 400)));
}

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
    const raw = await response.text().catch(() => "");
    let detail = raw;
    if (raw) {
      try {
        const parsed = JSON.parse(raw);
        detail = parsed && Object.prototype.hasOwnProperty.call(parsed, "detail") ? parsed.detail : parsed;
      } catch { /* 纯文本错误直接使用原文 */ }
    }
    const fallback = `HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ""}`;
    const message = typeof detail === "string"
      ? (detail.trim() || fallback)
      : detail == null ? fallback : JSON.stringify(detail);
    const error = new Error(message);
    error.status = response.status;
    error.detail = detail;
    throw error;
  }
  if (response.status === 204) return null;
  return response.json();
}

function loadPluginScript(src) {
  if (pluginScriptAssets.has(src)) return pluginScriptAssets.get(src);
  const loading = new Promise(resolve => {
    const script = document.createElement("script");
    script.src = src;
    script.onload = resolve;
    script.onerror = () => {
      pluginScriptAssets.delete(src);
      script.remove();
      console.error("插件脚本加载失败:", src);
      resolve();
    };
    document.head.append(script);
  });
  pluginScriptAssets.set(src, loading);
  return loading;
}

function cancelPendingViewRequests() {
  contextRequestSequence += 1;
  compressionRequestSequence += 1;
  draftOpenRequestSequence += 1;
  pluginViewRequestSequence += 1;
}

async function hydratePluginAssets(bootstrapPlugins = null) {
  // f18: 按启用插件清单注入前端资源(css 并行、js 串行;entry.js 固定最后执行,
  // 保证插件视图/打开器注册时其依赖模块已加载);失败不阻塞桌面
  try {
    const data = bootstrapPlugins == null
      ? await api("/desktop/api/plugins") : { plugins: bootstrapPlugins };
    const active = (data.plugins || []).filter(plugin => plugin.status === "active");
    for (const plugin of active) {
      const files = plugin.desktop_assets || [];
      for (const file of files.filter(name => name.endsWith(".css"))) {
        const href = `/plugins/${encodeURIComponent(plugin.name)}/desktop/${encodeURIComponent(file)}`;
        if (pluginStyleAssets.has(href)) continue;
        pluginStyleAssets.add(href);
        const link = document.createElement("link");
        link.rel = "stylesheet";
        link.href = href;
        link.onerror = () => {
          pluginStyleAssets.delete(href);
          link.remove();
          console.error("插件样式加载失败:", link.href);
        };
        document.head.append(link);
      }
      const jsFiles = files
        .filter(name => name.endsWith(".js"))
        .sort((a, b) => (a === "entry.js") - (b === "entry.js"));
      for (const file of jsFiles) {
        await loadPluginScript(`/plugins/${encodeURIComponent(plugin.name)}/desktop/${encodeURIComponent(file)}`);
      }
    }
  } catch (error) {
    console.error("插件前端资源注入失败:", error);
    setStatus(`插件资源注入失败: ${error.message}`, true);
  }
}

// f18: 插件视图返回 Focus 对话页的统一钩子
window.__focusBackToFocus = () => {
  state.view = "focus";
  render();
};

// f18: 文件面板关闭钩子(viewer 面板内关闭按钮调用)
window.__focusCloseFilePanel = () => {
  state.filesPanel = null;
  render();
};

function pluginViewForMaterial(material) {
  return Object.values(pluginViews).find(view =>
    typeof view.supportsMaterial === "function" && view.supportsMaterial(material)
  ) || null;
}

function currentSpatialTarget() {
  for (const view of Object.values(pluginViews)) {
    if (typeof view.getFocus !== "function") continue;
    const focus = view.getFocus();
    if (focus && focus.task_id === state.activeTaskId) return { view, focus };
  }
  return null;
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

function activeTask() {
  const task = state.tasks.find(item => item.task_id === state.activeTaskId);
  return sessionLifecycle(task) === "active" ? task : undefined;
}

function replaceTasks(tasks) {
  state.tasks = Array.isArray(tasks) ? tasks : [];
  const selected = state.tasks.find(task =>
    task.task_id === state.activeTaskId && sessionLifecycle(task) === "active"
  );
  state.activeTaskId = selected?.task_id
    || state.tasks.find(task => sessionLifecycle(task) === "active")?.task_id
    || null;
  return activeTask();
}

async function bootstrap() {
  setStatus("正在连接…");
  try {
    const data = await api("/desktop/api/bootstrap");
    replaceTasks(data.tasks);
    state.equipment = data.equipment;
    await hydratePluginAssets(data.plugins || []);
    await hydrateContextTrees();
    setStatus("");
    await hydrateActive();
  } catch (error) {
    setStatus(error.message, true);
    app.innerHTML = `<section class="empty-state"><h1>桌面服务未就绪</h1><p>${escapeHtml(error.message)}</p><button class="primary" data-action="reload">重试</button></section>`;
    return false;
  }
  render();
  return true;
}

async function hydrateActive(taskId = state.activeTaskId) {
  if (!taskId) return;
  const [detail, materials, agents, catalog] = await Promise.all([
    api(`/desktop/api/tasks/${taskId}`),
    api(`/desktop/api/tasks/${taskId}/materials`),
    api(`/desktop/api/tasks/${taskId}/agents`),
    api(`/desktop/api/tasks/${taskId}/skills`),
  ]);
  state.details.set(taskId, detail);
  state.materials.set(taskId, materials);
  state.agents.set(taskId, agents);
  state.skillCatalogs.set(taskId, catalog.skills);
  if (state.activeTaskId === taskId) {
    reconcileCommitmentRecovery(detail);
    reconcileCompressionRecovery(detail);
  }
  if (detail.active_run?.status === "pending" || detail.active_run?.status === "running") {
    listenToRun(detail.active_run);
  }
  agents.forEach(agent => {
    if (["pending", "running"].includes(agent.latest_run?.status)) listenToRun(agent.latest_run);
  });
  updatePatrolAvatarLayer(taskId);
}

function render() {
  document.body.dataset.view = state.view;
  renderShellChrome();
  if (!state.tasks.length) {
    app.replaceChildren(document.querySelector("#emptyTemplate").content.cloneNode(true));
    return;
  }
  if (state.view === "focus") {
    const task = activeTask();
    if (!task) renderNoActiveTask();
    else renderFocus(task);
  }
  else if (state.view === "map") renderMap();
  else if (state.view === "draft") renderDraft();
  else if (state.view === "compress") renderCompress();
  else if (state.view === "plugins") renderPlugins();
  else if (state.view === "memory") renderMemory();
  else if (state.view && pluginViews[state.view]) {
    app.replaceChildren();
    pluginViews[state.view].render(app, state);
  }
  else if (state.view === "context") renderContextEditor();
  else {
    state.view = "focus";
    setStatus("当前视图已不可用，已返回任务", true);
    renderShellChrome();
    const task = activeTask();
    if (!task) renderNoActiveTask();
    else renderFocus(task);
  }
}

function renderNoActiveTask() {
  app.innerHTML = `<section class="empty-state"><h1>暂无活动 Context</h1><p>当前没有可进入的活动 Context。可以新建任务，或从设置中恢复已归档的 Context。</p><div class="ui-toolbar"><button class="primary" data-action="new-task">新增任务</button><button class="text-button" data-action="open-settings">查看已归档 Context</button></div></section>`;
}

function activeNavigationKey() {
  if (state.view === "map") return "map";
  if (state.view === "plugins") return "plugins";
  if (state.view === "memory") return "memory";
  if (activeTask()?.harness_mode === "assembly") return "assembly";
  return "focus";
}

function renderShellChrome() {
  const task = activeTask();
  if (shellTaskTitle) shellTaskTitle.textContent = task?.title || "尚未选择任务";
  if (shellTaskMeta) shellTaskMeta.textContent = task
    ? `${task.workspace_name || "本地工作区"} · ${task.task_id.slice(0, 8)}`
    : "本地 Agent 工作台";
  const current = activeNavigationKey();
  document.querySelectorAll?.("[data-nav-key]").forEach(button => {
    if (button.dataset.navKey === current) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
    button.disabled = !state.tasks.length && !["plugins", "assembly", "memory"].includes(button.dataset.navKey);
  });
  renderInspector();
}

const RUN_STATUS_PRESENTATION = Object.freeze({
  pending: { label: "排队中", tone: "active" },
  running: { label: "运行中", tone: "active" },
  success: { label: "已完成", tone: "success" },
  interrupted: { label: "已中断", tone: "warning" },
  error: { label: "运行失败", tone: "danger" },
  cancelled: { label: "已取消", tone: "warning" },
  ready: { label: "就绪", tone: "neutral" },
});

function presentRunStatus(status) {
  return RUN_STATUS_PRESENTATION[status] || { label: status || "就绪", tone: "neutral" };
}

function renderInspector() {
  if (!appInspector?.setAttribute || !inspectorContent) return;
  appInspector.hidden = !state.inspector.open;
  appInspector.setAttribute("aria-hidden", String(!state.inspector.open));
  if (!state.inspector.open) return;
  const tab = state.inspector.tab;
  const task = activeTask();
  appInspector.querySelectorAll?.('[role="tab"]').forEach(button => {
    const selected = button.dataset.inspectorTab === tab;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  if (!task) {
    inspectorContent.innerHTML = '<section class="ui-empty-state"><h1>暂无任务</h1><p>创建任务后即可查看上下文信息。</p></section>';
    return;
  }
  if (tab === "context") {
    inspectorContent.innerHTML = renderContextRail(task);
    return;
  }
  if (tab === "materials") {
    const materials = state.materials.get(task.task_id) || [];
    const protectedCount = materials.filter(item => item.retention === "irreplaceable").length;
    inspectorContent.innerHTML = `<section class="inspector-section materials-inspector"><header><div><span class="workspace-kicker">TASK SOURCES</span><h3>任务材料</h3></div><span class="ui-badge">${materials.length}</span></header><p class="inspector-description">阅读策略、约束级别和版本保护只作用于当前任务。</p>${protectedCount ? `<div class="ui-notice is-success"><strong>${protectedCount} 份材料受版本保护</strong><span>不可遗失材料可置空或从历史版本恢复，但不会直接删除。</span></div>` : ""}<div class="inspector-list">${materials.length ? materials.map(renderMaterial).join("") : '<section class="ui-empty-state"><h1>暂无材料</h1><p>从 Composer 添加文件后，可在这里配置阅读和保留策略。</p></section>'}</div></section>`;
    return;
  }
  if (tab === "agents") {
    const agents = state.agents.get(task.task_id) || [];
    if (state.agentDetails.agentId) {
      const agent = agents.find(item => item.agent_id === state.agentDetails.agentId);
      const status = agent?.mode === "context_curator"
        ? curatorPresentationStatus(agent)
        : agent?.latest_run?.status || "ready";
      const presented = presentRunStatus(status);
      const curator = agent?.mode === "context_curator";
      inspectorContent.innerHTML = `<section class="inspector-section agent-inspector-detail">
        <header><button class="text-button" type="button" data-action="agent-list"><span class="ui-icon is-sm icon-chevron-left" aria-hidden="true"></span>Agents</button><span class="ui-badge is-${presented.tone}">${escapeHtml(curator ? curatorStateLabel(agent) : presented.label)}</span></header>
        <div><h3>${agent ? `小兵 ${escapeHtml(agent.agent_id.slice(0, 8))}` : "Agent"}</h3><p class="ui-meta">${escapeHtml(agent?.checkpoint_ns || "")}</p></div>
        ${curator
          ? (state.agentDetails.busy ? '<p class="muted">加载中…</p>' : renderCuratorDetails(agent, state.agentDetails.curation))
          : `<div class="agent-detail-history">${state.agentDetails.busy ? '<p class="muted">加载中…</p>' : state.agentDetails.messages.length ? state.agentDetails.messages.map(renderMessage).join("") : '<p class="muted">暂无已提交消息</p>'}</div>
            <div class="ui-toolbar agent-detail-actions"><button class="text-button" data-action="refresh-agent-details">刷新</button><button class="text-button" data-action="retry-agent-details">重试</button><button class="text-button danger" data-action="cancel-agent-details">取消运行</button></div>
            <form class="agent-inspector-continue" id="agentInspectorContinueForm"><label for="agentInspectorInput">继续对话</label><textarea id="agentInspectorInput" rows="3" placeholder="给这个 Agent 追加指令…"></textarea><button class="primary" type="submit">继续</button></form>`}
      </section>`;
      return;
    }
    inspectorContent.innerHTML = `<section class="inspector-section"><header><h3>协作 Agents</h3><span class="ui-badge">${agents.length}</span></header><div class="inspector-list">${agents.length ? renderAgentStrip(task.task_id) : '<p class="muted">尚未投放小兵</p>'}</div></section>`;
    return;
  }
  const run = state.details.get(task.task_id)?.active_run;
  const presented = presentRunStatus(run?.status || "ready");
  inspectorContent.innerHTML = `<section class="inspector-section"><header><h3>主 Agent</h3><span class="ui-badge is-${presented.tone}">${escapeHtml(presented.label)}</span></header><dl class="inspector-run-meta"><div><dt>任务</dt><dd>${escapeHtml(task.title)}</dd></div><div><dt>运行 ID</dt><dd>${escapeHtml(run?.run_id || "—")}</dd></div></dl>${state.commitment.stage ? `<section class="inspector-run-stage"><span class="workspace-kicker">COMMITMENT</span><strong>${state.commitment.stage}/9 · ${escapeHtml(COMMITMENT_STAGE_NAMES[state.commitment.stage] || "准备中")}</strong><span>${state.commitment.review ? "等待人工确认" : state.commitment.terminalStatus ? presentRunStatus(state.commitment.terminalStatus).label : "正在执行"}</span></section>` : ""}${renderInterruptButton(state.details.get(task.task_id))}</section>`;
}

function openInspector(tab, trigger) {
  const wasOpen = state.inspector.open;
  state.inspector.open = true;
  state.inspector.tab = tab;
  if (!wasOpen || !state.inspector.returnFocus) state.inspector.returnFocus = trigger || document.activeElement;
  renderShellChrome();
  requestAnimationFrame(() => {
    if (wasOpen && trigger?.matches?.('[role="tab"]')) trigger.focus();
    else appInspector?.focus();
  });
}

function closeInspector() {
  const returnFocus = state.inspector.returnFocus;
  state.inspector.open = false;
  state.inspector.returnFocus = null;
  renderShellChrome();
  requestAnimationFrame(() => returnFocus?.focus?.());
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

function atHighlightHtml(value) {
  return keywordCommand.highlightHtml(value, escapeHtml);
}

function updateAtHighlight(input) {
  if (!input) return;
  const wrap = typeof input.closest === "function" ? input.closest(".composer-input-wrap") : null;
  const highlight = wrap?.querySelector(".composer-input-highlight");
  if (!highlight) return;
  highlight.innerHTML = atHighlightHtml(input.value) + "\u200b";
  highlight.scrollTop = input.scrollTop;
  highlight.scrollLeft = input.scrollLeft;
}

function renderSkillPicker(kind, textarea, highlight = false) {
  const selected = selectedSkills(kind);
  const listId = `${kind}SkillList`;
  const tags = selected.map(name => `<span class="skill-tag">${escapeHtml(name)}<button type="button" data-action="remove-skill" data-picker-kind="${kind}" data-skill-name="${escapeHtml(name)}" aria-label="Remove ${escapeHtml(name)}"><span class="ui-icon is-sm icon-x" aria-hidden="true"></span></button></span>`).join("");
  const textareaWithAttrs = textarea.replace(">", ` data-skill-input="${kind}" aria-controls="${listId}" aria-expanded="false">`);
  const inputNode = highlight
    ? `<div class="composer-input-wrap"><div class="composer-input-highlight" aria-hidden="true"></div>${textareaWithAttrs}</div>`
    : textareaWithAttrs;
  return `<div class="skill-picker-shell ${kind === "draft" ? "draft-skill-picker" : ""}" data-skill-picker="${kind}">
    <div class="skill-tags" aria-label="Selected skills">${tags}</div>
    ${inputNode}
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

function composerFeedback(detail, projectionBlocked) {
  const error = state.composerErrors.get(state.activeTaskId);
  if (error) return { kind: "danger", text: `发送失败：${error}` };
  if (projectionBlocked) return { kind: "warning", text: "该 Context 需要完成执行投影决断后才能继续。" };
  if (activeTaskHasCommitmentLock()) return { kind: "warning", text: "当前任务正在等待 Commitment 审批，请先处理上方审批区。" };
  if (detail?.pending_compression) return { kind: "warning", text: "存在待确认的压缩计划，请先确认或取消。" };
  if (["pending", "running"].includes(detail?.active_run?.status)) return { kind: "active", text: "主 Agent 正在运行；你可以查看运行详情或中断。" };
  return { kind: "muted", text: "Enter 发送 · Shift+Enter 换行" };
}

function setComposerError(error = null) {
  if (error) state.composerErrors.set(state.activeTaskId, String(error));
  else state.composerErrors.delete(state.activeTaskId);
  const node = document.querySelector("#composerFeedback");
  if (!node) return;
  node.className = error ? "composer-feedback is-danger" : "composer-feedback is-muted";
  node.textContent = error ? `发送失败：${error}` : "Enter 发送 · Shift+Enter 换行";
}

function renderContextRail(task) {
  const tasks = contextEditor.contextFamilyTasks(state.tasks, state.contextTrees, task.task_id);
  const tree = state.contextTrees.get(task.workspace_id) || [];
  const nodes = new Map(tree.map(node => [node.context_id, node]));
  const cardList = tasks.map(item => {
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
    const lifecycle = item.lifecycle || node?.lifecycle || "active";
    if (lifecycle !== "active") {
      // 墓碑：仅当被归档/删除的会话仍被后代引用（有后代）时才保留原位占位；无后代直接消失。
      if (!contextHasDescendants(item.task_id, tree)) return "";
      const tag = lifecycle === "archived" ? "已归档" : "已删除";
      return `<div class="context-rail-item is-tombstone" style="--context-depth:${depth}" data-context-depth="${depth}">
        <span class="context-rail-card is-deleted" role="presentation">
          <span class="context-rail-title">${escapeHtml(item.title)}</span>
          <span class="context-rail-meta">${depth ? "派生 Context" : "根 Context"} · ${escapeHtml(item.task_id.slice(0, 8))} · ${tag}</span>
        </span>
      </div>`;
    }
    return `<div class="context-rail-item${node?.editable ? " is-editable" : ""}" style="--context-depth:${depth}" data-context-depth="${depth}">
      <button type="button" class="context-rail-card${item.task_id === task.task_id ? " is-current" : ""}${blocked ? " is-blocked" : ""}" data-action="context-rail-card" data-task-id="${escapeHtml(item.task_id)}" aria-current="${item.task_id === task.task_id ? "true" : "false"}">
        <span class="context-rail-title">${escapeHtml(item.title)}</span>
        <span class="context-rail-meta">${depth ? "派生 Context" : "根 Context"} · ${escapeHtml(item.task_id.slice(0, 8))}${node?.managed_status ? ` · 受管 ${escapeHtml(curatorTrackingLabel(node.managed_status))}${node.managed_health && node.managed_health !== "idle" ? `/${escapeHtml(node.managed_health)}` : ""}` : ""}${blocked ? ` · ${escapeHtml(projectionStatus)}` : ""} · 缓存 ${cacheRate}</span>
        ${otherParents ? `<span class="context-rail-parents">另含：${escapeHtml(otherParents)}</span>` : ""}
      </button>
      ${node?.editable ? `<button type="button" class="context-rail-edit" data-action="edit-context-definition" data-context-id="${escapeHtml(item.task_id)}">编辑</button>` : ""}
    </div>`;
  }).filter(Boolean);
  const cards = cardList.join("");
  return `<aside class="context-rail" aria-label="Context 树">
    <header class="context-rail-heading"><strong>Contexts</strong><span>${cardList.length}</span></header>
    <nav class="context-rail-list" aria-label="当前聊天派生的 Context">
      ${cards}
      <button type="button" class="context-rail-add" data-action="derive-context">新增 Context</button>
    </nav>
  </aside>`;
}

function renderFocus(task = activeTask()) {
  if (!task) return renderNoActiveTask();
  const detail = state.details.get(task.task_id) || { messages: [] };
  const projectionStatus = detail.context?.projection_status || "root";
  const projectionBlocked = !["root", "valid", "repaired", "approved"].includes(projectionStatus);
  const contextBlock = projectionBlocked
    ? `<section class="context-block-banner"><strong>该 Context 尚未获得安全执行投影</strong><span>${escapeHtml(projectionStatus)}</span><button class="primary" data-action="resume-context-decision">查看并决断</button></section>`
    : "";
  const feedback = composerFeedback(detail, projectionBlocked);
  const previousConversation = app.dataset.taskId === task.task_id ? document.querySelector("#conversation") : null;
  const previousRail = document.querySelector(".context-rail-list");
  const previousRailScrollTop = previousRail?.scrollTop;
  const wasPinned = previousConversation && previousConversation.scrollHeight - previousConversation.scrollTop - previousConversation.clientHeight < 80;
  const previousScrollTop = previousConversation?.scrollTop;
  // F20:任务记录只占主工作区；Context、材料和 Agents 迁入持久检查器，文件仍使用专用画布。
  // 显式行高约束 minmax(0,1fr):面板高度=工作区,内部文本视图可以独立滚动。
  const panelOpen = !!state.filesPanel;
  if (panelOpen) state.panelWidth = normalizePanelWidth(state.panelWidth);
  const shellStyle = `grid-template-rows: minmax(0, 1fr); grid-template-columns: minmax(0, 1fr)${panelOpen ? ` ${state.panelWidth}px` : ""}`;
  patrolAvatarController?.destroy();
  patrolAvatarController = null;
  app.innerHTML = `
    <section class="focus-shell" style="${shellStyle}">
      <section class="focus-view" data-task-id="${task.task_id}">
        ${contextBlock}
        <div class="commitment-progress" id="commitmentProgress" hidden>
          <div class="progress-heading"><strong>任务合同</strong><span id="progressLabel"></span></div>
          <ol id="progressSteps"></ol>
        </div>
        <div class="conversation" id="conversation">
          ${renderConversation(detail, task)}
        </div>
        <div class="patrol-avatar-layer" id="patrolAvatarLayer" aria-label="会话 Patrol 小兵"></div>
        <div class="focus-bottom">
          <div class="composer-shell">
            <div class="composer-context"><span class="ui-badge is-active">当前任务</span><span>${escapeHtml(task.title)}</span><button class="text-button" type="button" data-action="open-inspector-tab" data-inspector-tab="run">运行详情</button></div>
            <div class="composer">
              ${renderSkillPicker("main", `<textarea id="mainInput" aria-label="任务输入" placeholder="描述下一步，或输入 / 选择技能…">${escapeHtml(detail.ui_state?.input || "")}</textarea>`, true)}
              <div class="composer-actions"><label class="attach-button">添加文件<input id="fileInput" type="file" hidden></label>${renderInterruptButton(detail)}<button class="send-button" data-action="send-main">发送</button></div>
            </div>
            <p id="composerFeedback" class="composer-feedback is-${feedback.kind}" role="status">${escapeHtml(feedback.text)}</p>
          </div>
        </div>
      </section>
      ${panelOpen ? `<aside class="file-panel" id="filePanel"><div class="panel-resizer" id="panelResizer" title="拖拽调整面板宽度"></div><div class="file-panel-inner"></div></aside>` : ""}
    </section>`;
  app.dataset.taskId = task.task_id;
  const conversation = document.querySelector("#conversation");
  mountCommitmentRecovery(detail);
  restoreCommitmentPanels(conversation);
  const commitmentBlocked = activeTaskHasCommitmentLock() || projectionBlocked || !!detail.pending_compression;
  const mainInput = document.querySelector("#mainInput");
  const sendButton = document.querySelector('[data-action="send-main"]');
  if (mainInput) mainInput.disabled = commitmentBlocked;
  if (sendButton) sendButton.disabled = commitmentBlocked;
  updateAtHighlight(mainInput);
  conversation.scrollTop = previousConversation
    ? (wasPinned ? conversation.scrollHeight : previousScrollTop)
    : (detail.ui_state?.scrollTop ?? conversation.scrollHeight);
  const rail = document.querySelector(".context-rail-list");
  if (previousRailScrollTop != null && rail) rail.scrollTop = previousRailScrollTop;
  if (!previousConversation) requestAnimationFrame(() => rail?.querySelector('[aria-current="true"]')?.scrollIntoView({ block: "nearest" }));
  mountPatrolAvatarLayer(task, detail);
  if (panelOpen) {
    mountFilePanel();
    bindPanelResizer();
  }
  renderInspector();
}

function mountFilePanel() {
  const container = document.querySelector("#filePanel .file-panel-inner");
  if (!container || !state.filesPanel) return;
  const view = pluginViewForMaterial(state.filesPanel);
  if (view?.mountPanel) view.mountPanel(container, state.filesPanel, state);
}

function bindPanelResizer() {
  const resizer = document.querySelector("#panelResizer");
  const shell = document.querySelector(".focus-shell");
  if (!resizer || !shell) return;
  resizer.addEventListener("pointerdown", event => {
    event.preventDefault();
    resizer.setPointerCapture(event.pointerId);
    const startX = event.clientX;
    const startWidth = state.panelWidth;
    const move = moveEvent => {
      // 面板左缘:向左拖(负位移)= 面板变宽。
      const width = startWidth - (moveEvent.clientX - startX);
      state.panelWidth = normalizePanelWidth(width);
      shell.style.gridTemplateColumns = `minmax(0, 1fr) ${state.panelWidth}px`;
    };
    const finish = () => {
      try {
        if (resizer.hasPointerCapture?.(event.pointerId)) resizer.releasePointerCapture(event.pointerId);
      } catch {}
      resizer.removeEventListener("pointermove", move);
      resizer.removeEventListener("pointerup", finish);
      resizer.removeEventListener("pointercancel", finish);
      resizer.removeEventListener("lostpointercapture", finish);
      localStorage.setItem("focus-panel-width", String(state.panelWidth));
    };
    resizer.addEventListener("pointermove", move);
    resizer.addEventListener("pointerup", finish);
    resizer.addEventListener("pointercancel", finish);
    resizer.addEventListener("lostpointercapture", finish);
  });
}

// 记忆库编辑器：上下区（选源 vs 编辑）垂直分隔条 + 源/压缩区 水平分隔条，均可拖拽调高度/宽度。
function bindMemoryResizers() {
  const vertical = document.querySelector("[data-memory-resizer-vertical]");
  const horizontal = document.querySelector("[data-memory-resizer-horizontal]");
  if (vertical) {
    const top = document.querySelector("[data-memory-top]");
    const bottom = document.querySelector("[data-memory-bottom]");
    const parent = vertical.parentElement;
    vertical.addEventListener("pointerdown", event => {
      event.preventDefault();
      vertical.setPointerCapture(event.pointerId);
      const startY = event.clientY;
      const startH = parent.getBoundingClientRect().height || 1;
      const startTopH = top.getBoundingClientRect().height;
      const move = moveEvent => {
        const ratio = Math.min(0.85, Math.max(0.15, (startTopH + (moveEvent.clientY - startY)) / startH));
        top.style.flex = `${ratio} 1 0`;
        bottom.style.flex = `${1 - ratio} 1 0`;
        // 比例写回 state，作为重建布局的唯一数据源（不触发 render，避免拖拽卡顿）。
        state.memory = { ...state.memory, editorRatio: ratio };
      };
      const finish = () => {
        try { if (vertical.hasPointerCapture?.(event.pointerId)) vertical.releasePointerCapture(event.pointerId); } catch {}
        vertical.removeEventListener("pointermove", move);
        vertical.removeEventListener("pointerup", finish);
        vertical.removeEventListener("pointercancel", finish);
        vertical.removeEventListener("lostpointercapture", finish);
      };
      vertical.addEventListener("pointermove", move);
      vertical.addEventListener("pointerup", finish);
      vertical.addEventListener("pointercancel", finish);
      vertical.addEventListener("lostpointercapture", finish);
    });
  }
  if (horizontal) {
    const editor = document.querySelector("[data-memory-editor]");
    const left = document.querySelector(".memory-editor-sources");
    const right = document.querySelector(".memory-editor-target");
    horizontal.addEventListener("pointerdown", event => {
      event.preventDefault();
      horizontal.setPointerCapture(event.pointerId);
      const startX = event.clientX;
      const startW = editor.getBoundingClientRect().width || 1;
      const startLeftW = left.getBoundingClientRect().width;
      const move = moveEvent => {
        const ratio = Math.min(0.8, Math.max(0.2, (startLeftW + (moveEvent.clientX - startX)) / startW));
        left.style.flex = `${ratio} 1 0`;
        right.style.flex = `${1 - ratio} 1 0`;
        // 比例写回 state，作为重建布局的唯一数据源。
        state.memory = { ...state.memory, sourceRatio: ratio };
      };
      const finish = () => {
        try { if (horizontal.hasPointerCapture?.(event.pointerId)) horizontal.releasePointerCapture(event.pointerId); } catch {}
        horizontal.removeEventListener("pointermove", move);
        horizontal.removeEventListener("pointerup", finish);
        horizontal.removeEventListener("pointercancel", finish);
        horizontal.removeEventListener("lostpointercapture", finish);
      };
      horizontal.addEventListener("pointermove", move);
      horizontal.addEventListener("pointerup", finish);
      horizontal.addEventListener("pointercancel", finish);
      horizontal.addEventListener("lostpointercapture", finish);
    });
  }
}

window.addEventListener("resize", () => {
  if (!state.filesPanel) return;
  if (panelResizeFrame != null) cancelAnimationFrame(panelResizeFrame);
  panelResizeFrame = requestAnimationFrame(() => {
    panelResizeFrame = null;
    const width = normalizePanelWidth(state.panelWidth);
    if (width === state.panelWidth) return;
    state.panelWidth = width;
    const shell = document.querySelector(".focus-shell");
    if (shell) shell.style.gridTemplateColumns = `minmax(0, 1fr) ${width}px`;
  });
});

async function sha1Hex(text) {
  const buffer = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(text));
  return [...new Uint8Array(buffer)].map(byte => byte.toString(16).padStart(2, "0")).join("");
}

// f19 dsh-eyes:从消息内容提取图片 URL 列表(image_url 块 / 文本引用),
// 图片渲染为消息框上方的独立缩略图行(对齐 image8:图片在消息框上面,不嵌入气泡)。
const IMAGE_REF_RE = /【图片\d+ attachment_id=([0-9a-f]{12})】查看请调 view_image\(attachment_id=[0-9a-f]{12}\)/g;

// f18:消息文件卡片 —— 常见类型全部可点开(图片/PDF/docx/doc/md/txt),点击打开右侧文件面板
const FILE_VIEWABLE_RE = /\.(png|jpe?g|webp|bmp|gif|pdf|docx?|md|txt)$/i;

function fileNameFromLinkHref(href, hostPart = "") {
  const source = href.startsWith("file:") ? href : hostPart;
  return decodeURIComponent(source.replace(/[?#].*$/, "").replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "");
}

function renderFileCards(message) {
  const files = message.files;
  if (!Array.isArray(files) || !files.length) return "";
  const cards = files.map(file => {
    const name = String(file?.filename || file?.name || "");
    if (!name) return "";
    const viewable = FILE_VIEWABLE_RE.test(name)
      && !!pluginViewForMaterial({ relative_path: name, path: name });
    const icon = /\.(png|jpe?g|webp|bmp|gif)$/i.test(name) ? "file-image" : /\.(pdf|docx?|md|txt)$/i.test(name) ? "file-text" : "file";
    const size = file?.size ? ` · ${formatBytes(file.size)}` : "";
    const iconMarkup = `<span class="ui-icon is-sm icon-${icon}" aria-hidden="true"></span>`;
    return viewable
      ? `<button class="file-card" data-action="open-file-panel" data-file-name="${escapeHtml(name)}" title="点击在右侧面板打开">${iconMarkup}<span>${escapeHtml(name)}${size}</span></button>`
      : `<span class="file-card is-plain">${iconMarkup}<span>${escapeHtml(name)}${size}</span></span>`;
  }).join("");
  return cards ? `<div class="message-file-cards">${cards}</div>` : "";
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function collectMessageImages(content) {
  // 本地映射(本会话发送过的图片)优先;缺失时回退到后端附件接口
  // (重启后历史消息的引用仍可还原为图片)。
  // 引用可能位于字符串 content 或列表的 text 块中(后端剥离后列表形态)。
  const urls = window.__dshEyesAttachmentUrls || {};
  const threadId = state.details.get(state.activeTaskId)?.thread_id || "";
  const backendUrl = id => `/desktop/api/plugin/dsh-eyes/attachments/${encodeURIComponent(id)}?thread_id=${encodeURIComponent(threadId)}`;
  const images = [];
  const collect = text => {
    text.replace(IMAGE_REF_RE, (match, id) => {
      images.push(urls[id] || backendUrl(id));
      return match;
    });
  };
  if (typeof content === "string") {
    collect(content);
  } else if (Array.isArray(content)) {
    for (const block of content) {
      if (!block || typeof block !== "object") continue;
      if (block.type === "image_url") {
        const url = block.image_url && typeof block.image_url === "object" ? block.image_url.url : null;
        if (typeof url === "string" && url.startsWith("data:image/")) images.push(url);
      } else if (block.type === "text" && typeof block.text === "string") {
        collect(block.text);
      }
    }
  }
  return images;
}

function renderMessageImages(images) {
  if (!images.length) return "";
  return `<div class="dsh-eyes-message-images">${images
    .map(url => `<img class="dsh-eyes-message-image" src="${escapeHtml(url)}" alt="粘贴图片">`)
    .join("")}</div>`;
}

// 文本中移除图片引用标记(图片已提取到上方独立行,气泡内只留文字)
function stripImageReferences(text) {
  return text.replace(IMAGE_REF_RE, "");
}

function renderContentBlock(block) {
  if (!block || typeof block !== "object") return escapeHtml(String(block ?? ""));
  if (block.type === "image_url") {
    return ""; // 图片已由 collectMessageImages 提取到消息框上方,气泡内不输出
  }
  // text 块:移除图片引用(图片已提取到上方行),其余转义
  return stripImageReferences(escapeHtml(String(block.text ?? "")));
}

function renderMessageDetails(message) {
  const metadata = [];
  const id = message.id || message.message_id;
  if (id) metadata.push(["消息 ID", id]);
  if (message.tool_call_id) metadata.push(["工具调用 ID", message.tool_call_id]);
  if (message.name) metadata.push(["工具名称", message.name]);
  if (message.checkpoint_id) metadata.push(["Checkpoint", message.checkpoint_id]);
  if (message.created_at) metadata.push(["时间", message.created_at]);
  if (Array.isArray(message.tool_calls) && message.tool_calls.length) {
    metadata.push(["工具参数", JSON.stringify(message.tool_calls, null, 2)]);
  }
  if (message.additional_kwargs && Object.keys(message.additional_kwargs).length) {
    metadata.push(["附加数据", JSON.stringify(message.additional_kwargs, null, 2)]);
  }
  if (!metadata.length) return "";
  if (metadata.length === 1 && id) return `<span class="visually-hidden">消息 ID：${escapeHtml(String(id))}</span>`;
  return `<details class="message-details"><summary>技术详情</summary><dl>${metadata.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd><pre>${escapeHtml(String(value))}</pre></dd></div>`).join("")}</dl></details>`;
}

function renderMessage(message, { showRoleHeader = false } = {}) {
  // 后端兜底降级消息按工具结果样式渲染，不泄露原始 XML 标签
  const degraded = compressionPanel.degradedParts(message);
  if (degraded) {
    return `<article class="work-record message tool"><header class="work-record-header"><span class="work-record-kicker">TOOL</span><span class="message-role">工具 · ${escapeHtml(degraded.name || "tool")}</span></header><div class="message-content">${escapeHtml(degraded.content)}</div></article>`;
  }
  const semantics = contextCuratorPresentation.messageSemantics(message);
  const kind = semantics.kind;
  // f19 dsh-eyes:图片提取为消息框上方的独立缩略图行(对齐 image8),气泡内只留文字。
  // 安全:字符串 human 消息 MUST 转义(否则消息内 HTML 会注入 DOM,如 <style> 覆盖主题变量)。
  const messageImages = collectMessageImages(message.content);
  let content;
  if (typeof message.content === "string") {
    content = kind === "ai" ? message.content : stripImageReferences(escapeHtml(message.content));
  } else if (Array.isArray(message.content)) {
    content = message.content.map(renderContentBlock).join("\n");
  } else {
    content = escapeHtml(JSON.stringify(message.content, null, 2));
  }
  const renderedContent = kind === "ai" ? renderAssistantContent(content) : content;
  const associations = semantics.associations.map(item => `<span class="message-association">${escapeHtml(item.direction)} ${escapeHtml(item.name)}${item.tool_call_id ? ` · ${escapeHtml(item.tool_call_id)}` : ""}</span>`).join("");
  let header = "";
  if (showRoleHeader) {
    header = `<header class="work-record-header"><span class="work-record-kicker">${escapeHtml(semantics.roleLabel)}</span>${associations}</header>`;
  } else if (kind === "system") {
    header = `<header class="work-record-header"><span class="work-record-kicker">CONTEXT</span><span class="message-role">系统上下文</span>${associations}</header>`;
  }
  return `<article class="work-record message ${kind}">
    ${header}
    ${renderMessageImages(messageImages)}${renderFileCards(message)}
    <div class="message-content">${renderedContent}</div>
    ${renderMessageDetails(message)}
  </article>`;
}

function renderCompressionDivider(item) {
  // 压缩块/删除墓碑分界标记：原文已在其后原位展开显示；删除无摘要，仅提示
  if (item.deleted) {
    return `<div class="compression-block-divider is-deleted"><span><span class="ui-icon is-sm icon-trash-2" aria-hidden="true"></span>已删除 · 来源 ${item.count} 条（模型不可见，可在压缩面板中恢复）</span></div>`;
  }
  return `<div class="compression-block-divider">
    <details class="compression-block-summary"><summary><span class="ui-icon is-sm icon-package" aria-hidden="true"></span>压缩块 · 来源 ${item.count} 条</summary><div class="compression-block-summary-body">${escapeHtml(item.summary)}</div></details>
  </div>`;
}

function renderConversation(detail, task) {
  // 压缩块展开为来源原文渲染（保留原会话视觉），仅跳过执行协议占位
  const renderedParts = [];
  let messageGroup = [];
  const roleStructured = Boolean(detail.context?.managed_status);
  const flushMessages = () => {
    if (roleStructured) {
      for (const message of messageGroup) renderedParts.push(renderMessage(message, { showRoleHeader: true }));
      messageGroup = [];
      return;
    }
    let eventGroup = [];
    const flushEvents = () => {
      if (!eventGroup.length) return;
      renderedParts.push(`<section class="conversation-event-sequence" role="group" aria-label="执行过程">${eventGroup.map(conversationEvents.renderEvent).join("")}</section>`);
      eventGroup = [];
    };
    for (const item of conversationEvents.normalize(messageGroup)) {
      if (item.type !== "message") { eventGroup.push(item); continue; }
      flushEvents();
      renderedParts.push(renderMessage(item.message));
    }
    flushEvents();
    messageGroup = [];
  };
  for (const item of compressionPanel.expandForConversation(detail.messages || [])) {
    if (!item.divider) { messageGroup.push(item); continue; }
    flushMessages();
    renderedParts.push(renderCompressionDivider(item));
  }
  flushMessages();
  const rendered = renderedParts.join("");
  const messages = detail.messages?.length
    ? rendered
    : (task.harness_mode === "assembly"
      ? `<div class="assembly-empty">
          <p class="assembly-empty-title">无工作区模式</p>
          <p>配置全局 skill、mcp tools、插件等</p>
          <p>创造插件</p>
        </div>`
      : `<article class="work-record message system"><header class="work-record-header"><span class="work-record-kicker">READY</span><span class="message-role">任务已就绪</span></header><div class="message-content"><span class="muted">这是该工作区与线程的第一页。输入任务即可开始。</span><details class="message-details"><summary>工作区路径</summary><pre>${escapeHtml(task.workspace_path)}</pre></details></div></article>`);
  const streaming = [...state.streamBuffers.entries()]
    .filter(([, buffer]) => buffer.taskId === task.task_id && (buffer.text || buffer.reasoning))
    .map(([runId, buffer]) => `<article class="work-record message ai streaming" data-stream-run="${runId}">${renderStreamingContent(buffer)}</article>`)
    .join("");
  return messages + streaming;
}

function reconcileConversationMarkup(conversation, html) {
  const template = document.createElement?.("template");
  if (!template?.content || !conversation.replaceChildren) {
    conversation.innerHTML = html;
    return;
  }
  template.innerHTML = html;
  const existing = new Map([...conversation.querySelectorAll?.(".conversation-event[data-event-key]") || []]
    .map(node => [node.dataset.eventKey, node]));
  for (const next of template.content.querySelectorAll(".conversation-event[data-event-key]")) {
    const current = existing.get(next.dataset.eventKey);
    if (!current) continue;
    const expanded = current.open;
    current.className = next.className;
    current.innerHTML = next.innerHTML;
    current.open = expanded;
    next.replaceWith(current);
  }
  conversation.replaceChildren(template.content);
}

function replaceConversation(task, messages) {
  const detail = state.details.get(task.task_id) || {};
  detail.messages = messages;
  state.details.set(task.task_id, detail);
  if (state.view !== "focus" || state.activeTaskId !== task.task_id) return;
  const conversation = document.querySelector("#conversation");
  if (!conversation) return;
  const pinned = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 80;
  reconcileConversationMarkup(conversation, renderConversation(detail, task));
  restoreCommitmentPanels(conversation);
  if (pinned) conversation.scrollTop = conversation.scrollHeight;
}

function renderStreamingContent(buffer) {
  const reasoning = buffer.reasoning
    ? `<section class="conversation-event-sequence" role="group" aria-label="执行过程">${conversationEvents.renderEvent({ type: "reasoning", content: buffer.reasoning })}</section>`
    : "";
  const answer = buffer.text
    ? `<div class="message-content">${escapeHtml(buffer.text.replace(/\n{2,}/g, "\n"))}</div>`
    : "";
  return `<header class="work-record-header"><span class="ui-badge is-active">生成中</span></header>${reasoning}${answer}`;
}

function appendStreamDelta(envelope, field) {
  const task = state.tasks.find(item => item.thread_id === envelope.thread_id && item.workspace_id === envelope.workspace_id);
  const content = envelope.data?.content;
  const messageId = envelope.data?.message_id;
  // 防御：承诺子图冒泡消息（commitment-stage-*）只进轨迹面板，不进 lead 对话流
  if (typeof messageId === "string" && messageId.startsWith("commitment-stage-")) return;
  if (!task || !content || !envelope.agent_id.startsWith("main:")) return;
  beginLeadExecution(task.task_id);
  const buffer = state.streamBuffers.get(envelope.run_id) || { taskId: task.task_id, text: "", reasoning: "" };
  buffer[field] = (buffer[field] || "") + content;
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
      article.className = "work-record message ai streaming";
      article.dataset.streamRun = envelope.run_id;
      conversation.append(article);
    }
    article.innerHTML = renderStreamingContent(buffer);
    if (pinned) conversation.scrollTop = conversation.scrollHeight;
  }));
}

function appendToken(envelope) { appendStreamDelta(envelope, "text"); }
function appendReasoning(envelope) { appendStreamDelta(envelope, "reasoning"); }

function clearStreamBuffer(runId) {
  const frame = state.streamFrames.get(runId);
  if (frame) cancelAnimationFrame(frame);
  state.streamFrames.delete(runId);
  state.streamBuffers.delete(runId);
}

function renderMaterial(material) {
  const open = state.openMaterial === material.material_id;
  const viewable = !!pluginViewForMaterial(material);
  const reading = material.reading_mode === "full" ? "完整阅读" : "粗略阅读";
  const instruction = material.instruction_mode === "strict" ? "严格约束" : "参考材料";
  const retention = material.retention === "irreplaceable" ? "版本保护" : "可移除";
  return `<article class="material-row" data-material-id="${material.material_id}">
    <header class="material-summary">
      <button class="material-title-button" data-action="${viewable ? "open-material" : "toggle-material"}" title="${viewable ? "在文件工作台打开" : "查看材料规则"}"><span class="material-file-icon" aria-hidden="true"><span class="ui-icon is-sm icon-file-text"></span></span><span><strong class="material-name">${escapeHtml(material.relative_path)}</strong><small>${viewable ? "可在文件工作台查看" : "未启用匹配的查看器"}</small></span></button>
      <button class="text-button" data-action="toggle-material" aria-expanded="${open}">${open ? "收起" : "管理"}</button>
    </header>
    <div class="material-policy-row"><span class="ui-badge">${reading}</span><span class="ui-badge${material.instruction_mode === "strict" ? " is-warning" : ""}">${instruction}</span><span class="ui-badge${material.retention === "irreplaceable" ? " is-success" : ""}">${retention}</span></div>
    ${material.needs_confirmation ? `<div class="ui-notice is-warning"><strong>检测到外部删除</strong><span>Focus 已恢复文件，请从版本记录确认内容。</span></div>` : ""}
    ${open ? `<div class="material-editor">
      <label>阅读方式<select data-field="reading_mode"><option value="full" ${material.reading_mode === "full" ? "selected" : ""}>完整阅读</option><option value="rough" ${material.reading_mode === "rough" ? "selected" : ""}>粗略阅读</option></select></label>
      <label>约束<select data-field="instruction_mode"><option value="reference" ${material.instruction_mode === "reference" ? "selected" : ""}>仅供参考</option><option value="strict" ${material.instruction_mode === "strict" ? "selected" : ""}>严格遵守</option></select></label>
      <label>文件规则<select data-field="retention"><option value="removable" ${material.retention === "removable" ? "selected" : ""}>可移除</option><option value="irreplaceable" ${material.retention === "irreplaceable" ? "selected" : ""}>不可遗失</option></select></label>
      <div class="material-source-meta"><span>来源</span><code>${escapeHtml(material.relative_path)}</code></div>
      <span class="material-actions"><button class="primary" data-action="save-material">保存规则</button>
      ${material.retention === "irreplaceable" ? `<button class="text-button" data-action="clear-material">置空</button><button class="text-button" data-action="load-versions">版本</button>` : `<button class="text-button danger" data-action="delete-material">删除</button>`}</span>
      <ul class="version-list" data-versions></ul>
    </div>` : ""}
  </article>`;
}

function renderAgentStrip(taskId) {
  const agents = state.agents.get(taskId) || [];
  const warning = agents.some(agent => agent.permissions?.some(permission => permission === "write" || permission === "host_command"))
    ? `<span class="write-warning">共享宿主机写入：并发冲突采用最后写入者结果</span>` : "";
  return warning + agents.map(agent => {
    const status = agent.mode === "context_curator"
      ? curatorStateLabel(agent)
      : presentRunStatus(agent.latest_run?.status || "ready").label;
    return `<button class="agent-chip" data-action="agent-details" data-agent-id="${agent.agent_id}">${agent.mode === "context_curator" ? "Context 策展 · " : ""}小兵 ${agent.agent_id.slice(0, 5)} · ${escapeHtml(status)}</button>`;
  }).join("");
}

function patrolAvatarOptions(task, detail) {
  const positions = detail.ui_state?.patrol_avatar_positions || {};
  const composed = patrolPresence?.compose(state.agents.get(task.task_id) || [], positions) || {
    avatars: state.agents.get(task.task_id) || [],
    positions,
  };
  return {
    avatars: composed.avatars,
    positions: composed.positions,
    onPositionCommit: (avatarId, position) => savePatrolAvatarPosition(task.task_id, avatarId, position),
    onAction: (avatar, action) => {
      if (action.id === "quick-curate") return quickDeployContextCurator(task.task_id);
      if (action.id === "configure") return openDraft(task.task_id);
      if (action.id === "details" && avatar.agent_id) return openAgentDetails(avatar.agent_id);
    },
  };
}

function mountPatrolAvatarLayer(task, detail) {
  const root = document.querySelector("#patrolAvatarLayer");
  if (!root || !patrolAvatar) return;
  patrolAvatarController = patrolAvatar.mount(root, patrolAvatarOptions(task, detail));
}

function updatePatrolAvatarLayer(taskId = state.activeTaskId) {
  if (!patrolAvatarController || state.view !== "focus" || taskId !== state.activeTaskId) return;
  const task = activeTask();
  const detail = state.details.get(taskId);
  if (task && detail) patrolAvatarController.update(patrolAvatarOptions(task, detail));
}

function savePatrolAvatarPosition(taskId, avatarId, position) {
  const detail = state.details.get(taskId);
  if (!detail) return;
  detail.ui_state = {
    ...(detail.ui_state || {}),
    patrol_avatar_positions: {
      ...(detail.ui_state?.patrol_avatar_positions || {}),
      [avatarId]: position,
    },
  };
  if (taskId === state.activeTaskId && state.view === "focus") void persistFocusState();
}

function syncPatrolRunState(run, status, error = null) {
  if (run?.kind !== "patrol" || !run.task_id || !run.agent_id) return;
  const agents = state.agents.get(run.task_id) || [];
  const agent = agents.find(item => item.agent_id === run.agent_id);
  if (!agent) return;
  agent.latest_run = { ...(agent.latest_run || {}), ...run, status, error };
  updatePatrolAvatarLayer(run.task_id);
  if (state.inspector.open && state.inspector.tab === "agents" && run.task_id === state.activeTaskId) renderInspector();
}

function agentFromState(agentId) {
  return (state.agents.get(state.activeTaskId) || []).find(item => item.agent_id === agentId);
}

async function openAgentDetails(agentId) {
  state.agentDetails = { agentId, messages: [], curation: null, busy: false };
  openInspector("agents", document.activeElement);
  await refreshAgentDetails();
}

async function refreshAgentDetails({ appendCuration = false } = {}) {
  if (!state.agentDetails.agentId || state.agentDetails.busy) return;
  const requestId = ++agentDetailsRequestSequence;
  const agentId = state.agentDetails.agentId;
  const taskId = state.activeTaskId;
  state.agentDetails.busy = true;
  renderInspector();
  try {
    const agent = agentFromState(agentId);
    const cursor = appendCuration ? state.agentDetails.curation?.next_cursor : null;
    const [history, agents, curation] = await Promise.all([
      api(`/desktop/api/agents/${agentId}/history`),
      api(`/desktop/api/tasks/${taskId}/agents`),
      agent?.mode === "context_curator"
        ? api(`/desktop/api/agents/${agentId}/context-curation${cursor ? `?before=${encodeURIComponent(cursor)}` : ""}`)
        : Promise.resolve(null),
    ]);
    state.agents.set(taskId, agents);
    updatePatrolAvatarLayer(taskId);
    if (requestId === agentDetailsRequestSequence
        && state.activeTaskId === taskId
        && state.agentDetails.agentId === agentId) {
      state.agentDetails.messages = history;
      if (appendCuration && state.agentDetails.curation && curation) {
        const known = new Set(state.agentDetails.curation.revisions.map(item => item.revision_id));
        state.agentDetails.curation = {
          ...state.agentDetails.curation,
          next_cursor: curation.next_cursor,
          revisions: [
            ...state.agentDetails.curation.revisions,
            ...curation.revisions.filter(item => !known.has(item.revision_id)),
          ],
        };
      } else {
        state.agentDetails.curation = curation;
      }
    }
  } catch (error) { setStatus(error.message, true); }
  finally {
    if (requestId === agentDetailsRequestSequence
        && state.activeTaskId === taskId
        && state.agentDetails.agentId === agentId) {
      state.agentDetails.busy = false;
      renderInspector();
    }
  }
}

async function retryAgentDetails() {
  try {
    const run = await api(`/desktop/api/agents/${state.agentDetails.agentId}/retry`, { method: "POST" });
    listenToRun(run);
    setStatus("已发起小兵重试");
    await refreshAgentDetails();
  } catch (error) { setStatus(error.message, true); }
}

async function continueAgentDetails() {
  const input = document.querySelector("#agentInspectorInput");
  const message = input.value.trim();
  if (!message) return;
  try {
    const run = await api(`/desktop/api/agents/${state.agentDetails.agentId}/continue`, { method: "POST", body: JSON.stringify({ message }) });
    listenToRun(run);
    input.value = "";
    setStatus("已发起小兵继续对话");
    await refreshAgentDetails();
  } catch (error) { setStatus(error.message, true); }
}

async function cancelAgentDetails() {
  const agent = agentFromState(state.agentDetails.agentId);
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

function renderPlugins() {
  const { plugins, interfaces, traces, filter, selectedName } = state.plugins;
  app.innerHTML = pluginView.render(plugins, interfaces, traces, { filter, selectedName });
}

async function hydratePlugins() {
  const requestId = ++pluginHydrationSequence;
  try {
    const [data, traceData] = await Promise.all([
      api("/desktop/api/plugins"),
      api("/desktop/api/plugins/traces"),
    ]);
    const plugins = data.plugins || [];
    if (requestId !== pluginHydrationSequence) return false;
    const selectedName = plugins.some(item => item.name === state.plugins.selectedName)
      ? state.plugins.selectedName
      : (plugins.find(item => item.status === "rejected" || item.status === "unavailable") || plugins[0])?.name || null;
    state.plugins = { plugins, interfaces: data.interfaces || {}, traces: traceData.traces || [], filter: state.plugins.filter || "all", selectedName };
    return true;
  } catch (error) { if (requestId === pluginHydrationSequence) setStatus(error.message, true); return false; }
}

async function openPluginsView() {
  if (state.view === "focus") persistFocusState();
  cancelPendingViewRequests();
  const requestId = pluginViewRequestSequence;
  state.inspector.open = false;
  await hydratePlugins();
  if (requestId !== pluginViewRequestSequence) return;
  state.view = "plugins";
  render();
}

function renderMemory() {
  const s = state.memory;
  app.innerHTML = memoryView.render(s.memories, {
    selectedId: s.selectedId,
    composing: s.composing,
    draft: s.draft,
    sessions: s.sessions,
    activeSessionId: s.activeSessionId,
    enabledMessages: s.enabledMessages,
    selectedMessageIds: s.selectedMessageIds,
    activeMessageId: s.activeMessageId,
    collectedSources: s.collectedSources,
    editorRatio: s.editorRatio,
    sourceRatio: s.sourceRatio,
    contentMode: s.draft?.contentMode || "complete",
    segments: s.draft?.segments || [],
    draftContent: s.draft?.content || "",
    draftTitle: s.draft?.title || "",
    expandedGroups: s.expandedGroups || [],
    mergeMode: s.mergeMode || false,
    selectedSourcesForMerge: s.selectedSourcesForMerge || [],
  });
  bindMemoryResizers();
  restoreMemoryScroll();
}

// === 「对话」第一栏滚动位置：纳入 state 数据源，render 重建后恢复 ===
// 采用与 editorRatio/expandedGroups 一致的原则：临时 UI 状态进 state，重建后回填，避免整棵重建丢滚动。
function restoreMemoryScroll() {
  const list = document.querySelector(".memory-session-list");
  if (!list) return;
  // 回填上次滚动位置；用数据集标志跳过「回填本身触发的 scroll 写回」，避免循环/覆盖。
  list.dataset.restoringScroll = "1";
  list.scrollTop = state.memory.sessionScrollTop || 0;
  requestAnimationFrame(() => { delete list.dataset.restoringScroll; });
  // 滚动写回 state（数据源）。
  if (list.onmemoryScroll) return;
  list.onmemoryScroll = () => {
    if (list.dataset.restoringScroll === "1") return;
    if (state.memory.sessionScrollTop === list.scrollTop) return;
    state.memory = { ...state.memory, sessionScrollTop: list.scrollTop };
  };
  list.addEventListener("scroll", list.onmemoryScroll, { passive: true });
}

function syncMemoryField(field, value) {
  const draft = { ...(state.memory.draft || {}) };
  if (field === "title") draft.title = value;
  else if (field === "content") { draft.content = value; draft.contentDirty = true; }
  else if (field === "contentMode") { draft.contentMode = value; state.memory = { ...state.memory, draft }; render(); return; }
  state.memory = { ...state.memory, draft };
}

// 编辑分段记忆的某个段（title 或 body）。
function editMemorySegment(index, field, value) {
  const draft = { ...(state.memory.draft || {}) };
  const segments = (draft.segments || []).map((seg, i) => i === index ? { ...seg, [field]: value } : seg);
  state.memory = { ...state.memory, draft: { ...draft, segments } };
}

async function hydrateMemory() {
  const requestId = ++memoryHydrationSequence;
  try {
    const data = await api("/desktop/api/memory");
    if (requestId !== memoryHydrationSequence) return false;
    const memories = data.memories || [];
    const selectedId = memories.some(item => item.memory_id === state.memory.selectedId)
      ? state.memory.selectedId
      : (memories[0]?.memory_id || null);
    state.memory = { ...state.memory, memories, selectedId };
    return true;
  } catch (error) { if (requestId === memoryHydrationSequence) setStatus(error.message, true); return false; }
}

async function openMemoryView() {
  if (state.view === "focus") persistFocusState();
  cancelPendingViewRequests();
  const requestId = memoryViewRequestSequence;
  state.inspector.open = false;
  state.memory = { ...state.memory, composing: false, selectedId: state.memory.selectedId, sessions: [], activeSessionId: null, enabledMessages: [], selectedMessageIds: [], activeMessageId: null, collectedSources: [], draft: null };
  await hydrateMemory();
  if (requestId !== memoryViewRequestSequence) return;
  state.view = "memory";
  render();
}

function memorySessionCandidates() {
  return state.tasks.filter(task =>
    task.harness_mode !== "assembly" && sessionLifecycle(task) === "active"
  );
}

function openMemoryCompose(editing = false) {
  const existing = editing ? (state.memory.memories.find(m => m.memory_id === state.memory.selectedId) || null) : null;
  const fresh = {
    composing: true,
    draft: {
      title: existing?.title || "",
      content: existing?.content || "",
      contentDirty: false,
      contentMode: existing?.content_mode || "complete",
      segments: existing?.segments || [],
    },
    sessions: memorySessionCandidates(),
    activeSessionId: null,
    enabledMessages: [],
    selectedMessageIds: [],
    activeMessageId: null,
    collectedSources: [],
    expandedGroups: [],
    mergeMode: false,
    selectedSourcesForMerge: [],
    sessionScrollTop: 0,
  };
  state.memory = { ...state.memory, ...fresh };
  render();
}

// 折叠/展开某个会话分组：更新 state.expandedGroups（数据源），渲染由 state 驱动。
function toggleMemoryGroup(workspace) {
  const current = state.memory.expandedGroups || [];
  const has = current.includes(workspace);
  const expandedGroups = has ? current.filter(g => g !== workspace) : [...current, workspace];
  state.memory = { ...state.memory, expandedGroups };
  render();
}

async function pickMemorySession(contextId) {
  // 点选会话：加载其消息到消息栏，并清空选择。选中会话所在分组由渲染层恒展开。
  state.memory = { ...state.memory, activeSessionId: contextId, enabledMessages: [], selectedMessageIds: [], activeMessageId: null };
  render();
  try {
    const snapshot = await api(`/desktop/api/contexts/${contextId}/snapshot`);
    if (state.memory.activeSessionId !== contextId) return;
    state.memory = { ...state.memory, enabledMessages: snapshot.messages || [] };
    render();
  } catch (error) {
    state.memory = { ...state.memory, activeSessionId: null };
    setStatus(error.message, true);
    render();
  }
}

function toggleMemoryMessage(messageId) {
  // 消息卡点击：勾选/取消该消息（部分消息来源），并设为当前原文。
  const ids = new Set(state.memory.selectedMessageIds || []);
  ids.has(messageId) ? ids.delete(messageId) : ids.add(messageId);
  state.memory = { ...state.memory, selectedMessageIds: [...ids], activeMessageId: messageId };
  render();
}

function uniqueSources() {
  return state.memory.collectedSources || [];
}

function pushCollectedSource(source) {
  const sources = [...(state.memory.collectedSources || [])];
  sources.push(source);
  state.memory = { ...state.memory, collectedSources: sources };
  rebuildMemoryContent();
}

// 会话卡拖拽/「加入」：完整会话来源。
function addSessionSource(contextId, title) {
  if (!contextId) return;
  if (uniqueSources().some(s => s.kind === "session" && s.context_id === contextId)) return;
  pushCollectedSource({ kind: "session", context_id: contextId, title, rawText: `[完整会话] ${title || "会话"}` });
}

// 消息卡拖拽/勾选「加入」：部分消息来源（选中的消息 id，可任意多选不连续）。
function addMessagesSource(messageIds) {
  const ids = (messageIds || []).filter(Boolean);
  if (!ids.length || !state.memory.activeSessionId) return;
  // 按「会话 + 排序后的选中消息」作为内容指纹去重——不同勾选/不同会话的 messages 来源允许共存。
  const fingerprint = `${state.memory.activeSessionId}:${[...ids].sort().join(",")}`;
  if (uniqueSources().some(s => s.kind === "messages" && s.fingerprint === fingerprint)) return;
  // 收集所选消息正文，作为可编辑原文。
  const selected = (state.memory.enabledMessages || []).filter(m => ids.includes(m.id || ""));
  const text = selected.map(m => `${memoryView.roleLabel(m.role)}：${memoryView.messageText(m.content)}`).join("\n\n");
  pushCollectedSource({ kind: "messages", context_id: state.memory.activeSessionId, message_ids: ids, count: ids.length, text, rawText: text, fingerprint });
}

// 原文划选文字「加入」：部分文字来源。
// 每次划选建立一个**独立**的文字来源（不做同消息合并）——3 次划选 = 3 源，
// 且选取不要求连续（间断划选各自成源）。同消息多处文字自然表现为多个独立源。
function addTextSource(text) {
  const value = (text || "").trim();
  if (!value) { setStatus("请先在原文中划选文字", true); return; }
  const messageId = state.memory.textMessageId;
  const range = state.memory.textRange || { start: 0, end: value.length };
  const contextId = state.memory.activeSessionId;
  if (!messageId || !contextId) { setStatus("请先选择会话与消息再划选文字", true); return; }
  pushCollectedSource({ kind: "text", context_id: contextId, message_id: messageId, ranges: [{ start: range.start, end: range.end }], text: value, rawText: value });
}

function dropMemoryPayload(payload) {
  // 拖拽统一入口：payload = { kind, context_id?, title?, message_ids?, text? }
  if (!payload) return;
  if (payload.kind === "session") addSessionSource(payload.context_id, payload.title);
  else if (payload.kind === "messages") addMessagesSource(payload.message_ids);
  else if (payload.kind === "text") addTextSource(payload.text);
}

function removeCollectedSource(index) {
  const sources = (state.memory.collectedSources || []).filter((_, i) => i !== index);
  state.memory = { ...state.memory, collectedSources: sources };
  rebuildMemoryContent();
}

// === 记忆源合并（多选 → 归并成一个合并来源，分段压缩时作为一段） ===
function enterMergeSources() {
  state.memory = { ...state.memory, mergeMode: true, selectedSourcesForMerge: [] };
  render();
}

function cancelMergeSources() {
  state.memory = { ...state.memory, mergeMode: false, selectedSourcesForMerge: [] };
  render();
}

function toggleMergeSource(index) {
  const selected = new Set(state.memory.selectedSourcesForMerge || []);
  selected.has(index) ? selected.delete(index) : selected.add(index);
  state.memory = { ...state.memory, selectedSourcesForMerge: [...selected] };
  render();
}

// 合并：把选中的多个来源归并成一个来源（保留原始子来源引用，正文逐项拼接）。
function confirmMergeSources() {
  const all = state.memory.collectedSources || [];
  const selected = [...(state.memory.selectedSourcesForMerge || [])].filter(i => i >= 0 && i < all.length).sort((a, b) => a - b);
  if (selected.length < 2) { setStatus("请至少选择两个来源再合并", true); return; }
  const parts = selected.map(i => all[i]);
  const mergedText = parts.map(s => s.rawText || s.text || "").filter(Boolean).join("\n\n");
  const merged = {
    kind: "merged",
    rawText: mergedText,
    text: mergedText,
    subSources: parts.map(s => ({ kind: s.kind, context_id: s.context_id, title: s.title, message_ids: s.message_ids, ranges: s.ranges })),
  };
  // 用第一个被选中的来源的位置替换合并结果，移除其余选中项。
  const newSources = all.filter((_, i) => !selected.includes(i));
  newSources.splice(selected[0], 0, merged);
  state.memory = { ...state.memory, collectedSources: newSources, mergeMode: false, selectedSourcesForMerge: [] };
  rebuildMemoryContent();
}

// 拖拽重排左下栏「记忆源」列表顺序（来源顺序 = 合并/压缩/注入顺序）。
function moveCollectedSource(fromIndex, toIndex) {
  const sources = [...(state.memory.collectedSources || [])];
  if (fromIndex < 0 || fromIndex >= sources.length) return;
  const [moved] = sources.splice(fromIndex, 1);
  const insertAt = Math.max(0, Math.min(toIndex, sources.length));
  sources.splice(insertAt, 0, moved);
  state.memory = { ...state.memory, collectedSources: sources };
  rebuildMemoryContent();
}

function rebuildMemoryContent() {
  // 加/删来源时，为「完整记忆」生成一个可编辑的初始草稿（拼接各源原文）；用户可再点「重新压缩」调后端。
  // 「分段记忆」不在这里拼接——每段需逐来源压缩，交给「重新压缩」。
  const draft = state.memory.draft || {};
  if (draft.contentDirty) return;
  const mode = draft.contentMode || "complete";
  if (mode !== "complete") { render(); return; }
  const parts = (state.memory.collectedSources || []).map(s => s.rawText || s.text || "").filter(Boolean);
  const content = ["# 记忆", "", ...parts].join("\n\n");
  state.memory = { ...state.memory, draft: { ...draft, content } };
  render();
}

// 编辑某个记忆源的可编辑原文。
function editSourceText(index, text) {
  const sources = [...(state.memory.collectedSources || [])];
  if (!sources[index]) return;
  sources[index] = { ...sources[index], rawText: text };
  state.memory = { ...state.memory, collectedSources: sources };
}

// 把收集来源映射为后端可解析的 source 对象（仅保留后端允许的字段）。
function memorySourcesForApi(collectedSources, activeSessionId) {
  return (collectedSources || []).map(s => {
    if (s.kind === "session") return { type: "session", context_id: s.context_id, contextTitle: s.title || "会话" };
    if (s.kind === "messages") return { type: "messages", context_id: s.context_id, contextTitle: "会话", message_ids: s.message_ids || [] };
    if (s.kind === "text") return { type: "text", context_id: s.context_id || activeSessionId, parts: [{ message_id: s.message_id, ranges: s.ranges || [] }] };
    if (s.kind === "merged") return { type: "manual", text: s.rawText || "", contextTitle: "合并来源" };
    return null;
  }).filter(Boolean).map(memorySourceForApi);
}

// 重新压缩：按当前压缩语义（完整/分段）调用后端生成草稿，填充目标区。
async function recompressMemory() {
  const draft = (state.memory.draft || {});
  const sources = state.memory.collectedSources || [];
  const mode = draft.contentMode || "complete";
  const payloadSources = memorySourcesForApi(sources, state.memory.activeSessionId);
  if (!payloadSources.length) { setStatus("请先添加至少一个记忆来源", true); return; }
  try {
    const result = await api("/desktop/api/memory/summarize", { method: "POST", body: JSON.stringify({ selection: { sources: payloadSources }, mode }) });
    const segments = result.segments || [];
    state.memory = { ...state.memory, draft: { ...draft, content: result.content || "", segments, contentMode: mode, contentDirty: false } };
    render();
  } catch (error) { setStatus(error.message, true); }
}

// 后端 MemorySource 仅接受的字段（前端展示字段 contextTitle/count/text 不发送）。
const MEMORY_SOURCE_FIELDS = new Set(["type", "context_id", "message_ids", "parts", "text"]);

function memorySourceForApi(source) {
  return Object.fromEntries(Object.entries(source).filter(([key]) => MEMORY_SOURCE_FIELDS.has(key)));
}

async function saveMemory() {
  const draft = state.memory.draft || {};
  const title = (draft.title || "").trim();
  const content = (draft.content || "").trim();
  if (!content) { setStatus("记忆内容不能为空", true); return; }
  const sources = state.memory.collectedSources || [];
  const payloadSources = memorySourcesForApi(sources, state.memory.activeSessionId);
  const firstSession = sources.find(s => s.kind === "session");
  const anyText = sources.some(s => s.kind === "text");
  const sourceKind = sources.length
    ? (firstSession ? "full_session" : anyText ? "partial_text" : "partial_messages")
    : "manual";
  const body = {
    title: title || (content.slice(0, 20) || "未命名记忆"),
    content,
    content_mode: draft.contentMode || "complete",
    segments: draft.segments || [],
    source_kind: sourceKind,
    source: { sources: payloadSources.length ? payloadSources : [{ type: "manual", text: content }] },
  };
  try {
    await api("/desktop/api/memory", { method: "POST", body: JSON.stringify(body) });
    state.memory = { ...state.memory, composing: false, sessions: [], activeSessionId: null, enabledMessages: [], selectedMessageIds: [], activeMessageId: null, collectedSources: [], draft: null };
    await hydrateMemory();
    render();
  } catch (error) { setStatus(error.message, true); }
}

async function deleteMemory(memoryId) {
  if (!memoryId) return;
  try {
    await api(`/desktop/api/memory/${memoryId}`, { method: "DELETE" });
    await hydrateMemory();
    render();
  } catch (error) { setStatus(error.message, true); }
}

async function setCurationTrackingState(target) {
  const agentId = state.agentDetails.agentId;
  if (!agentId) return;
  try {
    state.agentDetails.curation = await api(`/desktop/api/agents/${agentId}/context-curation/state`, {
      method: "PUT",
      body: JSON.stringify({ state: target }),
    });
    setStatus(`Context 策展跟踪已${target === "following" ? "恢复" : target === "paused" ? "暂停" : "停止"}`);
    await refreshAgentDetails();
  } catch (error) { setStatus(error.message, true); }
}

function curatorTrackingLabel(status) {
  return contextCuratorPresentation.trackingLabel(status);
}

function curatorPresentationStatus(agent) {
  return contextCuratorPresentation.presentationStatus(agent);
}

function curatorStateLabel(agent) {
  return contextCuratorPresentation.stateLabel(agent);
}

function shortCheckpoint(value) {
  return value ? String(value).slice(0, 12) : "—";
}

function renderCurationAudit(curation) {
  const revisions = curation?.revisions || [];
  if (!revisions.length) return '<p class="muted">尚无策展版本。</p>';
  return revisions.map(revision => {
    const dispositions = revision.disposition_manifest || [];
    const summary = dispositions.reduce((counts, item) => {
      counts[item.action] = (counts[item.action] || 0) + 1;
      return counts;
    }, {});
    const planItemTypes = contextCuratorPresentation.revisionPlanItemTypes(revision);
    const sourceMessages = revision.source_payload?.source_snapshot?.messages || [];
    return `<details class="curation-revision">
      <summary><span>${escapeHtml(shortCheckpoint(revision.source_checkpoint_id))}</span><span class="ui-badge">${escapeHtml(revision.status)}</span></summary>
      <dl class="curation-meta"><div><dt>投影</dt><dd>${escapeHtml(revision.projection_status || "—")}</dd></div><div><dt>发布时间</dt><dd>${escapeHtml(revision.published_at || "—")}</dd></div></dl>
      <p class="tiny muted">使用 ${summary.used || (summary.retained || 0) + (summary.synthesized || 0)} · 舍弃 ${summary.discarded || 0}${planItemTypes.length ? ` · 计划 ${planItemTypes.map(escapeHtml).join(" / ")}` : ""}</p>
      ${revision.error ? `<p class="tiny danger">${escapeHtml(revision.error)}</p>` : ""}
      ${sourceMessages.length ? `<details class="curation-source-evidence"><summary>来源证据 · ${sourceMessages.length} 条</summary><div>${sourceMessages.map(message => `<div><code>${escapeHtml(message.source_message_id || "—")}</code><span>${escapeHtml(message.role || "—")}</span><span>${escapeHtml(compressionPanel.messageText(message).replace(/\s+/g, " ").slice(0, 160))}</span></div>`).join("")}</div></details>` : ""}
      <div class="curation-dispositions">${dispositions.map(item => {
        const targets = item.target_message_indexes || (item.target_message_index == null ? [] : [item.target_message_index]);
        const itemTypes = (item.plan_items || []).map(planItem => planItem.plan_item_type).filter(Boolean);
        return `<div><code>${escapeHtml(item.source_message_id)}</code><span>${escapeHtml(item.action)}</span><span>${escapeHtml(item.reason_category)}${targets.length ? ` · 目标消息 ${targets.map(index => `#${index + 1}`).join(", ")}` : ""}${itemTypes.length ? ` · ${itemTypes.map(escapeHtml).join(" / ")}` : ""}</span></div>`;
      }).join("")}</div>
      <div class="curation-attempts">${contextCuratorPresentation.attemptRows(revision).map(attempt => `<div><span>#${attempt.attempt_number}</span><span>${escapeHtml(attempt.model_name)}</span><span>${escapeHtml(attempt.output_method)}</span><span>${escapeHtml(attempt.status)}</span>${attempt.error_text ? `<span class="danger">${escapeHtml(attempt.error_text)}</span>` : ""}</div>`).join("")}</div>
    </details>`;
  }).join("");
}

function renderCuratorDetails(agent, curation) {
  if (!curation) return '<p class="muted">加载策展状态中…</p>';
  const status = curation.control_state;
  const current = curation.latest_revision;
  const managed = curation.managed_context;
  return `<section class="curation-detail">
    <div class="curation-mode"><span class="ui-badge is-success">Context 策展</span><strong>${escapeHtml(curatorStateLabel(curation))}</strong></div>
    <dl class="curation-meta">
      <div><dt>根 Context</dt><dd>${escapeHtml(curation.root_context?.title || curation.root_context?.context_id || "—")}</dd></div>
      <div><dt>受管 Context</dt><dd>${escapeHtml(managed?.title || "等待首次发布")}</dd></div>
      <div><dt>已观察 checkpoint</dt><dd><code>${escapeHtml(shortCheckpoint(curation.observed_checkpoint_id))}</code></dd></div>
      <div><dt>目标 checkpoint</dt><dd><code>${escapeHtml(shortCheckpoint(curation.desired_checkpoint_id))}</code></dd></div>
      <div><dt>已准备 checkpoint</dt><dd><code>${escapeHtml(shortCheckpoint(curation.prepared_checkpoint_id))}</code></dd></div>
      <div><dt>已发布 checkpoint</dt><dd><code>${escapeHtml(shortCheckpoint(curation.published_checkpoint_id))}</code></dd></div>
      <div><dt>处理健康</dt><dd>${escapeHtml(curation.health_state || "—")}</dd></div>
      <div><dt>当前尝试</dt><dd>${current ? `${escapeHtml(current.status)} · ${escapeHtml(shortCheckpoint(current.source_checkpoint_id))}` : "—"}</dd></div>
    </dl>
    ${curation.last_error ? `<div class="ui-notice is-warning"><strong>最近失败</strong><span>${escapeHtml(curation.last_error)}</span></div>` : ""}
    <div class="ui-toolbar agent-detail-actions">
      ${managed ? `<button class="text-button" data-action="open-managed-context" data-context-id="${escapeHtml(managed.context_id)}">打开受管 Context</button>` : ""}
      ${status === "following" ? '<button class="text-button" data-action="set-curation-state" data-state="paused">暂停</button>' : ""}
      ${status === "paused" ? '<button class="text-button" data-action="set-curation-state" data-state="following">恢复</button>' : ""}
      ${["following", "paused"].includes(status) ? '<button class="text-button danger" data-action="set-curation-state" data-state="stopped">停止跟踪</button>' : ""}
      ${["pending", "running"].includes(agent?.latest_run?.status) ? '<button class="text-button danger" data-action="cancel-agent-details">取消当前尝试</button>' : ""}
    </div>
    <section class="curation-audit"><h4>策展版本</h4>${renderCurationAudit(curation)}${curation.next_cursor ? '<button class="text-button" data-action="load-more-curation">加载更早版本</button>' : ""}</section>
  </section>`;
}

function persistMapViewPreferences() {
  try {
    localStorage.setItem(MAP_VIEW_PREFERENCES_KEY, JSON.stringify({
      mode: state.mapViewMode,
      workspaceIds: [...state.mapExpandedWorkspaceIds],
      contextIds: [...state.mapExpandedContextIds],
    }));
  } catch { /* 本机禁用持久化时仍保持当前会话状态 */ }
}

function setsEqual(left, right) {
  return left.size === right.size && [...left].every(value => right.has(value));
}

function prepareMapTreeModel() {
  const model = mapCollapsibleView.buildModel(state.tasks, state.contextTrees);
  const validWorkspaceIds = new Set(model.workspaces.map(workspace => workspace.id));
  const validContextIds = new Set(model.contexts.keys());
  const workspaceIds = new Set([...state.mapExpandedWorkspaceIds].filter(id => validWorkspaceIds.has(id)));
  const contextIds = new Set([...state.mapExpandedContextIds].filter(id => validContextIds.has(id)));

  if (state.activeTaskId && state.mapExpandedForTaskId !== state.activeTaskId) {
    const path = mapCollapsibleView.expansionForTask(model, state.activeTaskId);
    if (path.workspaceId) workspaceIds.add(path.workspaceId);
    path.contextIds.forEach(id => contextIds.add(id));
    state.mapExpandedForTaskId = state.activeTaskId;
  }

  const changed = !setsEqual(workspaceIds, state.mapExpandedWorkspaceIds)
    || !setsEqual(contextIds, state.mapExpandedContextIds);
  state.mapExpandedWorkspaceIds = workspaceIds;
  state.mapExpandedContextIds = contextIds;
  if (changed) persistMapViewPreferences();
  return model;
}

function renderMapTree() {
  const model = prepareMapTreeModel();
  return mapCollapsibleView.render(model, {
    tasks: state.tasks,
    activeTaskId: state.activeTaskId,
    expandedWorkspaceIds: state.mapExpandedWorkspaceIds,
    expandedContextIds: state.mapExpandedContextIds,
    selectedContextIds: state.selectedContextIds,
    selectionMode: state.selectionMode,
    focusKey: state.mapTreeFocusKey,
    presentStatus: presentRunStatus,
  });
}

function setMapExpansion(kind, id, expanded, focusKey) {
  const stateKey = kind === "workspace" ? "mapExpandedWorkspaceIds" : "mapExpandedContextIds";
  const next = new Set(state[stateKey]);
  expanded ? next.add(id) : next.delete(id);
  state[stateKey] = next;
  state.mapTreeFocusKey = focusKey;
  persistMapViewPreferences();
  renderMap(focusKey);
}

function toggleMapExpansion(kind, id, focusKey) {
  const expanded = kind === "workspace"
    ? state.mapExpandedWorkspaceIds.has(id)
    : state.mapExpandedContextIds.has(id);
  setMapExpansion(kind, id, !expanded, focusKey);
}

function collapseMapTree() {
  state.mapExpandedWorkspaceIds = new Set();
  state.mapExpandedContextIds = new Set();
  state.mapExpandedForTaskId = state.activeTaskId;
  state.mapTreeFocusKey = state.tasks[0] ? `workspace:${state.tasks[0].workspace_id}` : "";
  persistMapViewPreferences();
  renderMap(state.mapTreeFocusKey);
}

function taskCardMarkup(task, draftMode = false) {
  const draft = state.drafts.get(state.activeTaskId);
  const selected = draftMode && draft?.task_id === task.task_id;
  const status = task.active_run?.status;
  const presented = presentRunStatus(status || "ready");
  const tree = state.contextTrees.get(task.workspace_id) || [];
  const context = tree.find(item => item.context_id === task.task_id);
  const projectionStatus = context?.projection_status || "root";
  const parents = (context?.parents || []).slice(1).map(parent => state.tasks.find(item => item.task_id === parent.context_id)?.title || parent.context_id).join("、");
  const contextMeta = parents ? `<span class="context-parent-tags">另含：${escapeHtml(parents)}</span>` : "";
  const contextIdentity = context
    ? `${context.depth ? "派生 Context" : "根 Context"}${!["root", "valid", "repaired", "approved"].includes(projectionStatus) ? ` · ${escapeHtml(projectionStatus)}` : ""}`
    : "兼容任务";
  const selectable = state.selectionMode;
  const isSelected = state.selectedContextIds.has(task.task_id);
  const cardAction = selectable ? "toggle-select-session" : "task-card";
  const cardClass = `task-card${selectable && isSelected ? " is-selected" : ""}`;
  const selectMarkup = selectable ? `<span class="task-select-mark" aria-hidden="true"><span class="task-select-box"></span></span>` : "";
  const actionsMarkup = selectable ? "" : `
    ${sessionLifecycle(task) === "active" ? `
      <details class="task-card-more">
        <summary>更多操作</summary>
        <div class="task-card-actions">
          <button class="text-button" data-action="archive-context" data-context-id="${escapeHtml(task.task_id)}">归档</button>
          <button class="text-button" data-action="cascade-archive-context" data-context-id="${escapeHtml(task.task_id)}">级联归档</button>
          <button class="text-button danger" data-action="delete-context" data-context-id="${escapeHtml(task.task_id)}">删除</button>
          <button class="text-button danger" data-action="cascade-delete-context" data-context-id="${escapeHtml(task.task_id)}">级联删除</button>
        </div>
      </details>` : ""}
    <details class="task-technical"><summary>技术详情</summary><dl><div><dt>Context ID</dt><dd>${escapeHtml(task.task_id)}</dd></div><div><dt>Thread</dt><dd>${escapeHtml(task.thread_id)}</dd></div><div><dt>路径</dt><dd>${escapeHtml(task.workspace_path)}</dd></div></dl></details>`;
  return `<article class="task-card-shell ${draftMode && !selected ? "dimmed" : ""}" data-context-depth="${context?.depth || 0}">
    <button class="${cardClass}" data-task-id="${task.task_id}" data-action="${cardAction}" ${selectable ? `aria-pressed="${isSelected}"` : ""}>
      <span class="task-card-heading">${selectMarkup}<span class="task-title">${escapeHtml(task.title)}</span><span class="ui-badge is-${presented.tone}">${escapeHtml(presented.label)}</span></span>
      <span class="context-identity">${contextIdentity}</span>
      ${contextMeta}
    </button>
    ${actionsMarkup}
  </article>`;
}

function taskCards(draftMode = false) {
  return contextEditor.orderTasksByTree(state.tasks, state.contextTrees)
    .map(task => taskCardMarkup(task, draftMode))
    .join("");
}

function contextRootId(task, tree) {
  const nodes = new Map(tree.map(node => [node.context_id, node]));
  let current = nodes.get(task.task_id);
  const visited = new Set();
  while (current?.parents?.[0]?.context_id && !visited.has(current.context_id)) {
    visited.add(current.context_id);
    current = nodes.get(current.parents[0].context_id) || current;
    if (visited.has(current.context_id)) break;
  }
  return current?.context_id || task.task_id;
}

function renderMapGroups() {
  const ordered = contextEditor.orderTasksByTree(state.tasks, state.contextTrees);
  const workspaces = new Map();
  ordered.forEach(task => {
    const tree = state.contextTrees.get(task.workspace_id) || [];
    const workspace = workspaces.get(task.workspace_id) || { name: task.workspace_name || "本地工作区", roots: new Map() };
    const rootId = contextRootId(task, tree);
    if (!workspace.roots.has(rootId)) workspace.roots.set(rootId, []);
    workspace.roots.get(rootId).push(task);
    workspaces.set(task.workspace_id, workspace);
  });
  return [...workspaces.entries()].map(([workspaceId, workspace]) => {
    const allActive = [...workspace.roots.values()].reduce(
      (sum, tasks) => sum + tasks.filter(task => sessionLifecycle(task) === "active").length, 0
    );
    return `<section class="map-workspace-group" data-workspace-id="${escapeHtml(workspaceId)}">
      <header><div><span class="workspace-kicker">WORKSPACE</span><h2>${escapeHtml(workspace.name)}</h2></div><span class="ui-badge">${allActive} 个 Context</span></header>
      <div class="map-root-groups">${[...workspace.roots.entries()].map(([rootId, tasks]) => {
        const activeTasks = tasks.filter(task => sessionLifecycle(task) === "active");
        const root = tasks.find(task => task.task_id === rootId) || tasks[0];
        const rootLifecycle = sessionLifecycle(root);
        const headerLabel = rootLifecycle === "active"
          ? (activeTasks.length === 1 ? "仅根 Context" : `${activeTasks.length - 1} 个派生`)
          : `根 context 已${rootLifecycle === "archived" ? "归档" : "删除"} · ${activeTasks.length} 个派生`;
        return `<section class="map-root-group"><header><strong>${escapeHtml(root?.title || rootId)}</strong><span>${escapeHtml(headerLabel)}</span></header><div class="task-grid">${activeTasks.map(task => taskCardMarkup(task)).join("")}</div></section>`;
      }).join("")}</div>
    </section>`;
  }).join("");
}

function renderMap(focusKey = null) {
  const sel = state.selectedContextIds.size;
  const treeMode = state.mapViewMode === "tree";
  const presentationControls = `<div class="map-presentation-controls">
    <div class="map-presentation-switch" role="group" aria-label="全图展示方式">
      <button type="button" data-action="set-map-view" data-map-view="tree" aria-pressed="${treeMode}">折叠视图</button>
      <button type="button" data-action="set-map-view" data-map-view="cards" aria-pressed="${!treeMode}">卡片视图</button>
    </div>
    ${treeMode ? '<button type="button" class="text-button map-collapse-all" data-action="collapse-map-tree">全部折叠</button>' : ""}
  </div>`;
  const batchControls = state.selectionMode
    ? `<span class="map-selection-info">已选 ${sel} 个会话</span>
      <button class="text-button" data-action="batch-delete-selected" ${sel ? "" : "disabled"}>删除选中</button>
      <button class="text-button" data-action="batch-cascade-delete-selected" ${sel ? "" : "disabled"}>级联删除</button>
      <button class="text-button" data-action="toggle-selection-mode">取消</button>`
    : `<button class="text-button" data-action="toggle-selection-mode">批量删除</button>`;
  app.innerHTML = `<section class="map-view">
    <div class="map-toolbar">${presentationControls}<button class="soldier-source" draggable="true" aria-pressed="${state.soldierArmed}" data-action="arm-soldier">${state.soldierArmed ? "已装备小兵 · 选择任务" : "装备小兵"}</button>${batchControls}</div>
    <div class="${treeMode ? "map-tree-host" : "map-groups"}">${treeMode ? renderMapTree() : renderMapGroups()}</div>
  </section>`;
  if (focusKey) {
    const target = [...app.querySelectorAll("[role='treeitem'][data-tree-key]")]
      .find(item => item.dataset.treeKey === focusKey)
      || app.querySelector("[role='treeitem'][tabindex='0']");
    target?.focus();
  }
}

async function hydrateContextTrees() {
  const requestId = ++contextTreeRequestSequence;
  const workspaceIds = [...new Set(state.tasks.map(task => task.workspace_id))];
  const trees = await Promise.all(workspaceIds.map(async workspaceId => [
    workspaceId,
    await api(`/desktop/api/workspaces/${workspaceId}/contexts/tree`),
  ]));
  if (requestId !== contextTreeRequestSequence) return;
  trees.forEach(([workspaceId, tree]) => state.contextTrees.set(workspaceId, tree));
}

async function openContextEditor(contextId) {
  const task = state.tasks.find(item => item.task_id === contextId);
  if (!task) return;
  const requestId = ++contextRequestSequence;
  const navigationId = taskSwitchSequence;
  setStatus("读取 Context checkpoint…");
  try {
    const snapshot = await api(`/desktop/api/contexts/${contextId}/snapshot`);
    if (requestId !== contextRequestSequence || navigationId !== taskSwitchSequence) return;
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
  } catch (error) { if (requestId === contextRequestSequence) setStatus(error.message, true); }
}

async function reopenContextDecision(contextId) {
  const requestId = ++contextRequestSequence;
  const navigationId = taskSwitchSequence;
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
    if (requestId !== contextRequestSequence || navigationId !== taskSwitchSequence) return;
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
  } catch (error) { if (requestId === contextRequestSequence) setStatus(error.message, true); }
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
    return `<section class="context-decision repaired"><span class="ui-badge is-success">已修复</span><h2>执行投影已安全生成</h2><p>协议结构已补齐，来源消息与用户定义未被静默改写。</p>${repairList}<div class="context-decision-actions ui-action-bar"><span class="muted tiny">可以安全进入派生 Context</span><button class="primary" data-action="open-derived-context">进入新 Context</button></div></section>`;
  }
  if (!["approval_required", "rejected", "initialization_failed"].includes(context.projection_status)) return repairList;
  const issues = (context.issues || []).map(issue => `<article class="context-issue">
    <strong>${escapeHtml(issue.reason)}</strong>
    <div class="context-diff"><div><span>原始片段</span><pre>${escapeHtml(JSON.stringify(issue.original, null, 2))}</pre></div><div><span>拟议投影</span><pre>${escapeHtml(JSON.stringify(issue.proposed, null, 2))}</pre></div></div>
  </article>`).join("");
  const actions = context.projection_status === "approval_required"
    ? `<button class="primary" data-action="accept-context-projection">接受本次降级</button><button class="text-button" data-action="edit-context-projection">返回编辑</button><button class="text-button danger" data-action="cancel-context-projection">取消发送</button>`
    : `<button class="text-button" data-action="edit-context-projection">返回编辑</button><button class="text-button danger" data-action="exit-context-editor">关闭</button>`;
  return `<section class="context-decision blocked"><span class="ui-badge is-warning">需要决断</span><h2>执行投影需要你的确认</h2><p>以下差异会影响 Agent 实际接收的上下文；Focus 不会静默采用降级。</p>${repairList}${issues}<div class="context-decision-actions ui-action-bar"><span class="muted tiny">确认前不会启动新 Context</span><div class="dialog-actions">${actions}</div></div></section>`;
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
    return `<span class="context-source-tab-wrap"><button type="button" class="context-source-tab${source.context_id === active?.context_id ? " is-active" : ""}" data-action="context-source-select" data-context-id="${escapeHtml(source.context_id)}">${escapeHtml(task?.title || source.context_id)}</button>${!created && draft.sources.length > 1 ? `<button type="button" class="context-source-remove" data-action="remove-context-source" data-source-index="${index}" aria-label="移除此来源"><span class="ui-icon is-sm icon-x" aria-hidden="true"></span></button>` : ""}</span>`;
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
  state.contextDraft = null;
  await switchTask(contextId);
}

function renderDraft() {
  const draft = state.drafts.get(state.activeTaskId);
  const task = state.tasks.find(item => item.task_id === draft?.task_id);
  if (!draft || !task) { state.view = "map"; renderMap(); return; }
  const curator = draft.mode === "context_curator";
  app.innerHTML = `<section class="draft-view">
    <section class="draft-panel">
      <header class="draft-heading"><span class="ui-meta">${curator ? "跟踪起点" : "来源"} checkpoint ${escapeHtml(draft.source_checkpoint_id || "空历史")}</span><span class="ui-badge is-success">${curator ? "只读策展 · 无通用工具" : "主 Agent 可继续运行"}</span></header>
      <nav class="draft-mode-switch" aria-label="Patrol 运行模式">
        <button type="button" class="${curator ? "" : "is-active"}" data-action="set-patrol-mode" data-mode="standard">普通 Patrol</button>
        <button type="button" class="${curator ? "is-active" : ""}" data-action="set-patrol-mode" data-mode="context_curator">Context 策展</button>
      </nav>
      <div class="draft-workflow">
        ${curator ? renderCuratorDraftSteps(draft) : renderStandardDraftSteps(draft)}
        <section class="draft-step draft-confirm" data-step="confirm"><header><span>04</span><div><h2>确认投放</h2><p>草稿会自动保存。检查 token 预算和权限后再启动 Agent。</p></div></header><div class="draft-confirm-summary"><span id="draftSaveState" class="ui-badge is-success">自动保存</span><span class="token-count" id="tokenCount">估算 ${draft.token_estimate} tokens</span></div></section>
      </div>
      <footer class="draft-footer ui-action-bar"><button class="text-button" data-action="exit-draft">退出并保存</button><span class="muted tiny">草稿仅属于当前任务</span><button class="primary" data-action="deploy">确认并投放</button></footer>
    </section>
  </section>`;
  updateTokenState();
}

function renderStandardDraftSteps(draft) {
  return `<section class="draft-step" data-step="objective"><header><span>01</span><div><h2>目标摘要</h2><p>说明这个 Agent 要完成什么，以及它应继承的系统约束。</p></div></header><div class="draft-step-body">
    <div class="draft-field"><label for="draftFinalMessage">最终任务指令</label>${renderSkillPicker("draft", `<textarea id="draftFinalMessage" data-draft-field="final_human_message" placeholder="给小兵一个清晰、可验收的目标…">${escapeHtml(draft.final_human_message)}</textarea>`)}</div>
    <details class="draft-advanced" ${state.openDraftSection === "system" ? "open" : ""}><summary>高级：System Prompt</summary><textarea data-draft-field="system_prompt">${escapeHtml(draft.system_prompt)}</textarea></details>
  </div></section>
  <section class="draft-step" data-step="history"><header><span>02</span><div><h2>消息编排</h2><p>调整交接历史；工具调用关联项会作为一个整体处理。</p></div></header><div class="draft-step-body">${renderHistory(draft)}</div></section>
  <section class="draft-step" data-step="equipment"><header><span>03</span><div><h2>装备与权限</h2><p>选择模型与宿主权限；写入和命令会直接影响真实工作区。</p></div></header><div class="draft-step-body">${renderEquipment(draft)}</div></section>`;
}

function renderCuratorDraftSteps(draft) {
  const policy = draft.curation_policy || {};
  const root = state.tasks.find(item => item.task_id === draft.root_context_id);
  return `<section class="draft-step" data-step="objective"><header><span>01</span><div><h2>跟踪来源</h2><p>Patrol 会持续读取这个聊天根的稳定主 checkpoint，而不是冻结当前历史。</p></div></header><div class="draft-step-body">
    <dl class="curation-meta"><div><dt>聊天根 Context</dt><dd>${escapeHtml(root?.title || draft.root_context_id || "保存后解析")}</dd></div><div><dt>权限边界</dt><dd>只读 · 无技能 · 无通用工具</dd></div></dl>
  </div></section>
  <section class="draft-step" data-step="policy"><header><span>02</span><div><h2>策展策略</h2><p>定义需要保留、合成和舍弃的信息；每次投放后策略会被冻结。</p></div></header><div class="draft-step-body curation-policy-fields">
    <label>策展指令<textarea data-curation-policy="instructions" placeholder="例如：保留目标、约束、已确认决策、有效事实和未完成事项。">${escapeHtml(policy.instructions || "")}</textarea></label>
    <label>必须保留（每行一条）<textarea data-curation-policy="preserve_rules">${escapeHtml((policy.preserve_rules || []).join("\n"))}</textarea></label>
    <label>应当舍弃（每行一条）<textarea data-curation-policy="discard_rules">${escapeHtml((policy.discard_rules || []).join("\n"))}</textarea></label>
  </div></section>
  <section class="draft-step" data-step="equipment"><header><span>03</span><div><h2>模型窗口</h2><p>完整根快照必须落在模型窗口内；系统不会静默裁剪来源。</p></div></header><div class="draft-step-body">
    <label>模型<select data-equipment="model_name">${state.equipment.models.map(model => `<option value="${model.name}" ${model.name === draft.equipment?.model_name ? "selected" : ""}>${escapeHtml(model.display_name)} · ${model.context_window || "未知"}</option>`).join("")}</select></label>
    <p class="tiny muted">运行时固定为 read 权限，不装配 skills、write 或 host_command。</p>
  </div></section>`;
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

const sourceMeta = {
  builtin: { label: "内置", className: "tool-source builtin" },
  custom: { label: "自定义", className: "tool-source custom" },
  mcp: { label: "MCP", className: "tool-source mcp" },
  plugin: { label: "插件", className: "tool-source plugin" },
};

function renderEquipmentTools() {
  const tools = state.equipment.tools || [];
  if (!tools.length) return "";
  const grouped = tools.reduce((map, tool) => {
    const source = tool.source || "builtin";
    (map[source] = map[source] || []).push(tool);
    return map;
  }, {});
  const blocks = Object.entries(grouped)
    .sort(([a], [b]) => scoreSource(a) - scoreSource(b))
    .map(([source, items]) => {
      const meta = sourceMeta[source] || { label: source, className: "tool-source" };
      return `<div class="equipment-tool-group">
        <span class="${meta.className}">${escapeHtml(meta.label)}</span>
        <ul class="equipment-tool-list">${items.map(tool => `<li title="${escapeHtml(String(tool.source || ""))}">${escapeHtml(tool.label || tool.name)}</li>`).join("")}</ul>
      </div>`;
    }).join("");
  return `<div class="equipment-tools"><span class="tiny muted">可用工具（按来源）</span>${blocks}</div>`;
}

function scoreSource(source) {
  return source === "builtin" ? 0 : source === "custom" ? 1 : source === "mcp" ? 2 : 3;
}

function renderEquipment(draft) {
  const equipment = draft.equipment || {};
  const permissions = equipment.permissions || ["read"];
  return `<div class="equipment-grid">
    <label>模型<select data-equipment="model_name">${state.equipment.models.map(model => `<option value="${model.name}" ${model.name === equipment.model_name ? "selected" : ""}>${escapeHtml(model.display_name)}</option>`).join("")}</select></label>
    <div><span class="tiny muted">权限</span><div class="check-line">${state.equipment.permissions.map(permission => `<label><input type="checkbox" data-permission="${permission}" ${permissions.includes(permission) ? "checked" : ""}>${permission}</label>`).join("")}</div></div>
    ${renderEquipmentTools()}
    <p class="tiny danger">无沙箱：写入或命令权限会直接影响真实宿主机。命令权限可绕过文件工具规则。</p>
  </div>`;
}

async function openDraft(taskId) {
  const requestId = ++draftOpenRequestSequence;
  const navigationId = taskSwitchSequence;
  setStatus("复制 checkpoint…");
  try {
    const [draft, catalog] = await Promise.all([
      api(`/desktop/api/tasks/${taskId}/drafts/open`, { method: "POST" }),
      api(`/desktop/api/tasks/${taskId}/skills`),
    ]);
    if (requestId !== draftOpenRequestSequence || navigationId !== taskSwitchSequence) return;
    state.activeTaskId = taskId;
    state.drafts.set(taskId, draft);
    state.skillCatalogs.set(taskId, catalog.skills);
    state.view = "draft";
    state.soldierArmed = false;
    setStatus("");
    render();
  } catch (error) { if (requestId === draftOpenRequestSequence) setStatus(error.message, true); }
}

async function quickDeployContextCurator(taskId) {
  if (state.quickCuratingTaskIds.has(taskId)) return;
  state.quickCuratingTaskIds.add(taskId);
  setStatus("正在启动持续 Context 策展…");
  try {
    const run = await api(`/desktop/api/tasks/${taskId}/context-curation/quick-deploy`, {
      method: "POST",
      body: JSON.stringify({ deployment_id: crypto.randomUUID() }),
    });
    listenToRun(run);
    await Promise.all([refreshTasks(), hydrateContextTrees(), hydrateActive(taskId)]);
    if (state.activeTaskId === taskId) render();
    setStatus("已启动持续 Context 策展 Patrol");
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    state.quickCuratingTaskIds.delete(taskId);
  }
}

function defaultCurationPolicy(draft) {
  const source = draft?.default_curation_policy || {};
  return {
    instructions: String(source.instructions || ""),
    preserve_rules: Array.isArray(source.preserve_rules) ? [...source.preserve_rules] : [],
    discard_rules: Array.isArray(source.discard_rules) ? [...source.discard_rules] : [],
  };
}

function setDraftPatrolMode(draft, mode) {
  if (!draft || !["standard", "context_curator"].includes(mode) || draft.mode === mode) return false;
  draft.mode = mode;
  if (mode === "context_curator") {
    draft.curation_policy = defaultCurationPolicy(draft);
    draft.equipment = {
      ...(draft.equipment || {}),
      model_name: draft.default_curation_model_name || draft.equipment?.model_name,
      permissions: ["read"],
      skills: [],
    };
  }
  return true;
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
  document.querySelectorAll("[data-curation-policy]").forEach(input => {
    const field = input.dataset.curationPolicy;
    draft.curation_policy ||= {};
    draft.curation_policy[field] = field === "instructions"
      ? input.value
      : input.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
  });
  const model = document.querySelector('[data-equipment="model_name"]');
  if (model) draft.equipment.model_name = model.value;
  if (draft.mode === "context_curator") {
    draft.equipment.permissions = ["read"];
    draft.equipment.skills = [];
  } else {
    draft.equipment.permissions = [...document.querySelectorAll("[data-permission]:checked")].map(input => input.dataset.permission);
    draft.equipment.skills = normalizeSkillNames(draft.equipment.skills);
  }
  return draft;
}

function scheduleDraftSave() {
  const taskId = state.activeTaskId;
  syncDraftFromDom();
  clearTimeout(state.saveTimer);
  const revision = (state.draftSaveRevisions.get(taskId) || 0) + 1;
  state.draftSaveRevisions.set(taskId, revision);
  const indicator = document.querySelector("#draftSaveState");
  if (indicator) { indicator.textContent = "待保存"; indicator.className = "ui-badge is-warning"; }
  state.saveTimer = setTimeout(() => saveDraft(taskId, { syncDom: false, revision }), 350);
}

async function saveDraft(taskId = state.activeTaskId, options = {}) {
  if (options.cancelTimer !== false) {
    clearTimeout(state.saveTimer);
    state.saveTimer = null;
  }
  const draft = options.syncDom === false || taskId !== state.activeTaskId || state.view !== "draft"
    ? state.drafts.get(taskId)
    : syncDraftFromDom();
  if (!draft) return false;
  const revision = options.revision ?? ((state.draftSaveRevisions.get(taskId) || 0) + 1);
  state.draftSaveRevisions.set(taskId, Math.max(revision, state.draftSaveRevisions.get(taskId) || 0));
  const payload = {
    system_prompt: draft.system_prompt,
    history_messages: draft.history_messages,
    final_human_message: draft.final_human_message,
    equipment: draft.equipment,
    mode: draft.mode || "standard",
    curation_policy: draft.curation_policy || {},
  };
  try {
    const saved = await api(`/desktop/api/drafts/${draft.draft_id}`, { method: "PUT", body: JSON.stringify(payload) });
    if (state.draftSaveRevisions.get(taskId) === revision) {
      state.drafts.set(taskId, { ...saved, deployment_id: draft.deployment_id || saved.deployment_id });
      if (state.activeTaskId === taskId && state.view === "draft") {
        const counter = document.querySelector("#tokenCount");
        if (counter) { counter.textContent = `估算 ${saved.token_estimate} tokens`; updateTokenState(); }
        const indicator = document.querySelector("#draftSaveState");
        if (indicator) { indicator.textContent = "已自动保存"; indicator.className = "ui-badge is-success"; }
      }
      setStatus("草稿已保存");
    }
    return true;
  } catch (error) {
    const indicator = state.activeTaskId === taskId ? document.querySelector("#draftSaveState") : null;
    if (indicator) { indicator.textContent = "保存失败，可重试"; indicator.className = "ui-badge is-danger"; }
    setStatus(error.message, true);
    return false;
  }
}

function updateTokenState() {
  const draft = state.drafts.get(state.activeTaskId);
  const model = state.equipment.models.find(item => item.name === draft?.equipment?.model_name);
  const over = model?.context_window && draft.token_estimate > model.context_window;
  document.querySelector("#tokenCount")?.classList.toggle("over", Boolean(over));
  const deploy = document.querySelector('[data-action="deploy"]');
  if (deploy) deploy.disabled = Boolean(over) || (draft.mode !== "context_curator" && !draft.final_human_message?.trim());
}

async function deployDraft() {
  if (state.deploying) return;
  state.deploying = true;
  document.querySelector('[data-action="deploy"]')?.setAttribute("disabled", "");
  if (!await saveDraft(state.activeTaskId)) {
    state.deploying = false;
    updateTokenState();
    return;
  }
  const draft = state.drafts.get(state.activeTaskId);
  if (draft.mode !== "context_curator" && !draft.final_human_message.trim()) {
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

async function sendMainOnce() {
  adoptCommitmentContext();
  if (activeTaskHasCommitmentLock()) {
    return setStatus("存在尚未处理的承诺流程，请先处理审批面板", true);
  }
  if (state.details.get(state.activeTaskId)?.pending_compression) {
    return setStatus("存在待确认的压缩请求，请先完成压缩或取消", true);
  }
  setStatus("");
  const input = document.querySelector("#mainInput");
  const message = input.value.trim();
  if (!message) return;
  // f35 仅保留规范命令；旧 @压缩 输入按普通用户消息发送。
  const quickKeyword = keywordCommand.parse(message);
  if (quickKeyword !== null) {
    const keyword = quickKeyword;
    const task = activeTask();
    if (!task) return setStatus("当前没有活动任务", true);
    input.value = "";
    return openCompressionView(task, null, keyword);
  }
  setComposerError();
  const spatialTarget = currentSpatialTarget();
  if (spatialTarget?.focus.kind === "patrol"
      && typeof spatialTarget.view.sendFocusedMessage === "function") {
    try {
      if (await spatialTarget.view.sendFocusedMessage(message)) {
        input.value = "";
        updateAtHighlight(input);
        const focusedDetail = state.details.get(state.activeTaskId);
        focusedDetail.ui_state = { ...(focusedDetail.ui_state || {}), input: "" };
        persistFocusState();
        setStatus("后续指令已交给当前空间小兵");
        return;
      }
    } catch (error) {
      setStatus(error.message, true);
      setComposerError(error.message);
      return;
    }
  }
  // f19 dsh-eyes:插件前端粘贴的待发图片以 image_url 内容块随消息发送
  // (纯文本时保持原形态;取走即清空插件队列)
  const pendingImages = typeof window.__dshEyesPeekPendingImages === "function"
    ? window.__dshEyesPeekPendingImages()
    : typeof window.__dshEyesTakePendingImages === "function"
      ? window.__dshEyesTakePendingImages()
    : [];
  // f19 dsh-eyes:预计算 attachment_id(与后端剥离同算法 sha1(url) 前 12 位),
  // 存本地映射供消息区把引用还原为缩略图(values 快照会用后端剥离后的引用覆盖本地消息)
  window.__dshEyesAttachmentUrls = window.__dshEyesAttachmentUrls || {};
  for (const image of pendingImages) {
    const digest = await sha1Hex(image.url);
    window.__dshEyesAttachmentUrls[digest.slice(0, 12)] = image.url;
  }
  const messagePayload = pendingImages.length
    ? [{ type: "text", text: message },
      ...pendingImages.map(image => ({ type: "image_url", image_url: { url: image.url } }))]
    : message;
  const detail = state.details.get(state.activeTaskId);
  try {
    const run = await api(`/desktop/api/tasks/${state.activeTaskId}/main/runs`, {
      method: "POST",
      body: JSON.stringify({
        message: messagePayload,
        skills: selectedSkills("main"),
        spatial_focus: spatialTarget?.focus || null,
      }),
    });
    const contextNode = (state.contextTrees.get(activeTask().workspace_id) || [])
      .find(item => item.context_id === state.activeTaskId);
    if (contextNode) contextNode.editable = false;
    if (detail.context) detail.context.editable = false;
    // 运行已发起：立即暴露中断入口（否则运行中 active_run 仍为旧值，按钮不渲染）
    detail.active_run = run;
    window.__dshEyesCommitPendingImages?.();
    state.composerErrors.delete(state.activeTaskId);
    detail.messages = [...(detail.messages || []), { role: "human", content: messagePayload }];
    detail.ui_state = { ...(detail.ui_state || {}), input: "", skills: [] };
    renderFocus();
    persistFocusState();
    listenToRun(run);
  } catch (error) { setStatus(error.message, true); setComposerError(error.message); }
}

function sendMain() {
  if (state.mainSubmitting) return Promise.resolve();
  state.mainSubmitting = true;
  return sendMainOnce().finally(() => { state.mainSubmitting = false; });
}

const COMMITMENT_STAGE_NAMES = {
  1: "明确目标", 2: "要求与兼容性", 3: "优先级", 4: "必要输入",
  5: "技术版本", 6: "官方知识", 7: "合同落盘", 8: "产出合同", 9: "交接准备",
};
const TRACE_ACTORS = { supervisor: "Supervisor", worker: "Worker", evaluator: "Evaluator" };

function listenToRun(run) {
  if (state.streams.has(run.run_id)) return;
  syncPatrolRunState(run, run.status || "pending", run.error || null);
  let runError = null;
  const parseEvent = (event, fallback = null) => {
    if (!event?.data) return fallback;
    try { return JSON.parse(event.data); }
    catch {
      runError = "运行流返回了无法解析的数据";
      setStatus(runError, true);
      return fallback;
    }
  };
  const source = new EventSource(`${runtime.apiBase}/desktop/api/runs/${run.run_id}/stream?session=${encodeURIComponent(runtime.session)}`);
  state.streams.set(run.run_id, source);
  source.addEventListener("metadata", event => {
    const envelope = parseEvent(event);
    if (envelope?.data?.status) syncPatrolRunState(run, envelope.data.status);
  });
  source.addEventListener("tokens", event => {
    const token = parseEvent(event);
    if (token) appendToken(token);
  });
  source.addEventListener("reasoning", event => {
    const reasoning = parseEvent(event);
    if (reasoning) appendReasoning(reasoning);
  });
  source.addEventListener("events", event => {
    const envelope = parseEvent(event);
    if (!envelope) return;
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
    const envelope = parseEvent(event);
    if (!envelope) return;
    const value = envelope.data?.value;
    if (!value) return;
    if (value.type === "commitment_review") {
      if (!commitmentBelongsToTask(envelope)) return;
      showReview(value);
      return;
    }
    if (value.type === "compression_request") {
      const task = state.tasks.find(item => item.thread_id === envelope.thread_id && item.workspace_id === envelope.workspace_id);
      if (!task) return;
      openCompressionView(task, value);
    }
  });
  source.addEventListener("error", event => {
    if (!event.data) return;
    const error = parseEvent(event)?.data?.error || "运行失败";
    runError = error;
    syncPatrolRunState(run, "error", error);
    setStatus(error, true);
  });
  source.addEventListener("end", async event => {
    const terminal = parseEvent(event, { status: "error", error: "运行流异常结束" });
    if (!terminal.error && runError) terminal.error = runError;
    syncPatrolRunState(run, terminal.status, terminal.error || null);
    // 主动中断判定：主 Agent run 终态 interrupted 且无错误、且非承诺审批（审批面板已接管界面状态）
    const wasMainInterrupted = run.kind === "main" && terminal.status === "interrupted" && !terminal.error;
    source.close(); state.streams.delete(run.run_id); clearStreamBuffer(run.run_id);
    await refreshActiveAfterTxn();
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
  container.dataset.stage = String(stage || 0);
  list.replaceChildren();
  for (let number = 1; number <= 9; number += 1) {
    const item = document.createElement("li");
    item.className = number < stage ? "done" : number === stage ? "active" : "";
    item.title = `${number}. ${COMMITMENT_STAGE_NAMES[number] || ""}`;
    list.append(item);
  }
  container.querySelector("#progressLabel").textContent = stage
    ? `${stage}/9 · ${COMMITMENT_STAGE_NAMES[stage] || "正在准备"}`
    : "正在准备任务合同";
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
    // 当前任务无承诺流程：若残留的是其它任务的承诺状态，或已恢复，统一复位
    if (state.commitment.taskId && state.commitment.taskId !== state.activeTaskId) {
      resetCommitment();
    } else if (state.commitment.taskId === state.activeTaskId && state.commitment.recoveryRestored) {
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

// === 压缩视图（human-in-the-loop 上下文压缩）===

function formatTokens(value) {
  const number = Number(value || 0);
  return number >= 1000 ? `${Math.round(number / 1000)}k` : String(number);
}

function compressionRoleLabel(message) {
  const base = { human: "你", user: "你", ai: "助手", assistant: "助手", system: "系统", tool: "工具" }[message?.role] || message?.role || "消息";
  return message?.role === "tool" && message.name ? `工具 · ${message.name}` : base;
}

function compressionBelongsToTask(envelope) {
  const task = state.tasks.find(item => item.thread_id === envelope.thread_id && item.workspace_id === envelope.workspace_id);
  if (!task) return false;
  if (state.compression.taskId === task.task_id) return true;
  state.compression.taskId = task.task_id;
  return true;
}

async function openCompressionView(task, request, quickKeyword) {
  if (!task) return setStatus("当前没有活动任务", true);
  if (state.compression.busy && state.compression.taskId === task.task_id) return;
  const requestId = ++compressionRequestSequence;
  const navigationId = taskSwitchSequence;
  try {
    const [detail, snapshot] = await Promise.all([
      api(`/desktop/api/tasks/${task.task_id}`),
      api(`/desktop/api/tasks/${task.task_id}/messages`),
    ]);
    if (requestId !== compressionRequestSequence || navigationId !== taskSwitchSequence) return;
    state.activeTaskId = task.task_id;
    state.compression = {
      taskId: task.task_id,
      request: request || detail.pending_compression || null,
      messages: snapshot.messages || [],
      selected: new Set(),
      ranges: [],
      busy: false,
      recovery: detail.compression_recovery || null,
      quick: quickKeyword ? { keyword: quickKeyword } : null,
    };
    // 快捷关键字压缩：自动把命中该词的消息勾选进 SOURCE，供用户调粒度
    if (quickKeyword) {
      const hitIds = compressionPanel.hitKeywordMessageIds(state.compression.messages, quickKeyword);
      const indexes = [];
      state.compression.messages.forEach((message, index) => {
        if (hitIds.includes(message.id)) indexes.push(index);
      });
      state.compression.selected = new Set(indexes);
      setStatus(`关键字快捷压缩「${quickKeyword}」共命中 ${indexes.length} 条，请确认后继续`);
    } else {
      setStatus("上下文接近上限，等待压缩确认");
    }
    state.view = "compress";
    render();
  } catch (error) { setStatus(error.message, true); }
}

function closeCompressionView() {
  compressionRequestSequence += 1;
  state.compression = {
    taskId: null, request: null, messages: [], selected: new Set(),
    ranges: [], busy: false, recovery: null,
  };
  state.view = "focus";
  render();
}

function reconcileCompressionRecovery(detail) {
  const recovery = detail?.compression_recovery || null;
  if (!recovery) {
    if (state.compression.taskId === state.activeTaskId && state.compression.recovery) {
      closeCompressionView();
    }
    return;
  }
  if (state.compression.taskId === state.activeTaskId && state.view === "compress") return;
  if (recovery.status === "resumable" || recovery.status === "orphaned") {
    const task = state.tasks.find(item => item.task_id === state.activeTaskId);
    return openCompressionView(task, detail.pending_compression || recovery.request);
  }
  setStatus("压缩请求正在处理，请等待当前运行结束", true);
}

function compressionRowPreview(message, isBlock) {
  // 行预览文案：块/墓碑显示标记，合成占位与空内容显示友好提示，降级消息取去标签内容
  if (isBlock) {
    const sourceCount = message.compression?.source?.length ?? 0;
    // 压缩块：显示该块的摘要内容（它具体把这段对话浓缩成了什么），而非笼统的"压缩块"标记。
    if (message.compression?.deleted) {
      return sourceCount ? `已删除 · 来源 ${sourceCount} 条` : "已删除";
    }
    const summary = compressionPanel.messageText(message).replace(/\s+/g, " ").trim();
    if (summary) return summary.slice(0, 140);
    return sourceCount ? `来源 ${sourceCount} 条` : "（空压缩消息）";
  }
  if (compressionPanel.isProtocolPlaceholder(message)) return "（工具结果已在压缩中省略）";
  const degraded = compressionPanel.degradedParts(message);
  const raw = degraded ? degraded.content : compressionPanel.messageText(message);
  const text = raw.replace(/\s+/g, " ").trim().slice(0, 140);
  if (!text) return message.tool_calls?.length ? "（工具调用）" : "（空内容）";
  return text;
}

function renderCompressionNested(messages, level = 0) {
  // 来源原文的递归渲染：嵌套压缩块继续展开，降级消息按工具结果展示
  return (Array.isArray(messages) ? messages : []).map(message => {
    const isBlock = Boolean(message.compression);
    const sourceCount = message.compression?.source?.length ?? 0;
    const nested = isBlock && sourceCount ? renderCompressionNested(message.compression.source, level + 1) : "";
    return `<div class="compression-nested-row" style="--nested-level: ${level}">
      <span class="compression-role">${escapeHtml(compressionRoleLabel(message))}</span>
      <span class="compression-body">
        <span class="compression-preview">${isBlock ? `<span class="ui-icon is-sm ${message.compression?.deleted ? "icon-trash-2" : "icon-package"}" aria-hidden="true"></span>` : ""}${escapeHtml(compressionRowPreview(message, isBlock))}</span>
        ${nested ? `<div class="compression-nested-source">${nested}</div>` : ""}
      </span>
    </div>`;
  }).join("");
}

function renderCompressionMessages() {
  const c = state.compression;
  const plannedSourceIds = new Set(c.ranges.flatMap(range => range.source_ids || []));
  return c.messages.map((message, index) => {
    const isBlock = Boolean(message.compression);
    const isProtocolPlaceholder = compressionPanel.isProtocolPlaceholder(message);
    const planned = plannedSourceIds.has(message.id);
    return `<label class="compression-message-row${isBlock ? " is-block" : ""}${message.compression?.deleted ? " is-deleted" : ""}${isProtocolPlaceholder ? " is-synthetic" : ""}${planned ? " is-planned" : ""}">
      <input type="checkbox" data-action="toggle-compress-message" data-index="${index}" ${c.selected.has(index) ? "checked" : ""} ${planned ? "disabled title=\"已加入变更计划\"" : ""}>
      <span class="compression-index">${String(index + 1).padStart(2, "0")}</span>
      <span class="compression-role">${escapeHtml(compressionRoleLabel(message))}</span>
      <span class="compression-body">
        <span class="compression-preview">${isBlock ? `<span class="ui-icon is-sm ${message.compression?.deleted ? "icon-trash-2" : "icon-package"}" aria-hidden="true"></span>` : ""}${escapeHtml(compressionRowPreview(message, isBlock))}</span>
        ${isBlock ? `<details class="compression-block-source"><summary>展开来源原文</summary><div class="compression-block-source-body">${renderCompressionNested(message.compression.source || [], 1)}</div></details>` : ""}
      </span>
    </label>`;
  }).join("");
}

function renderCompressionRanges() {
  const c = state.compression;
  if (!c.ranges.length) return `<p class="muted">在左侧勾选消息并加入计划或直接删除；确认前当前上下文不会发生任何变化。</p>`;
  return c.ranges.map((range, rangeIndex) => {
    const indexes = range.source_ids.map(id => c.messages.findIndex(message => message.id === id));
    const label = indexes.length ? `消息 ${Math.min(...indexes) + 1}~${Math.max(...indexes) + 1}` : "范围";
    const singleBlock = range.source_ids.length === 1
      && c.messages.find(message => message.id === range.source_ids[0])?.compression;
    if (range.delete) {
      return `<article class="compression-range-card is-delete">
        <header><strong>${label}</strong><span class="muted tiny">${range.source_ids.length} 条消息</span></header>
        <p>从上下文中删除这 ${range.source_ids.length} 条消息（不生成摘要）</p>
        <div class="compression-range-actions">
          <button class="text-button" data-action="undelete-range" data-range-index="${rangeIndex}">改回压缩</button>
          <button class="text-button" data-action="remove-range" data-range-index="${rangeIndex}">移除</button>
        </div>
      </article>`;
    }
    return `<article class="compression-range-card${range.restore ? " is-restore" : ""}">
      <header><strong>${label}</strong><span class="muted tiny">${range.source_ids.length} 条消息</span></header>
      ${range.restore
        ? `<p>恢复该压缩块的来源原文（撤销此次压缩）</p>
           <div class="compression-range-actions">
             <button class="text-button" data-action="unrestore-range" data-range-index="${rangeIndex}">改回压缩</button>
             <button class="text-button" data-action="remove-range" data-range-index="${rangeIndex}">移除</button>
           </div>`
        : `<textarea data-range-index="${rangeIndex}" placeholder="摘要内容（可编辑，也可完全重写）">${escapeHtml(range.replacement)}</textarea>
           ${range.error ? `<p class="compression-range-error" role="alert">${escapeHtml(range.error)}</p>` : ""}
           <div class="compression-range-actions">
             <button class="text-button" data-action="summarize-range" data-range-index="${rangeIndex}" ${range.summarizing ? "disabled" : ""}>${range.summarizing ? "生成中…" : range.generated ? "重新生成" : "生成摘要"}</button>
             <button class="text-button danger" data-action="delete-range" data-range-index="${rangeIndex}">删除</button>
             <button class="text-button" data-action="remove-range" data-range-index="${rangeIndex}">移除</button>
             ${singleBlock ? `<button class="text-button" data-action="restore-range" data-range-index="${rangeIndex}">恢复原消息</button>` : ""}
           </div>`}
    </article>`;
  }).join("");
}

function compressionReady() {
  const ranges = state.compression.ranges;
  const allSourceIds = ranges.flatMap(range => range.source_ids || []);
  return ranges.length > 0
    && new Set(allSourceIds).size === allSourceIds.length
    && ranges.every(range => range.restore || range.delete || String(range.replacement || "").trim());
}

function renderCompress() {
  const c = state.compression;
  const task = state.tasks.find(item => item.task_id === c.taskId) || activeTask();
  if (!task || !c.messages.length) { state.view = "focus"; return render(); }
  // 勾选/生成等操作会整体重渲染：保留左右两栏滚动位置，避免列表弹回顶部
  const previousList = document.querySelector(".compression-message-list");
  const previousPlan = document.querySelector(".compression-plan");
  const listScrollTop = previousList?.scrollTop;
  const planScrollTop = previousPlan?.scrollTop;
  const stats = compressionPanel.beforeAfter(c.messages, c.ranges);
  const usage = c.request?.usage ?? 0;
  const limit = c.request?.limit ?? 0;
  app.innerHTML = `
    <section class="compression-view">
      <header class="compression-heading">
        <p class="compression-usage">当前用量 <strong>${formatTokens(usage)} / ${formatTokens(limit)} tokens</strong>${c.request ? ` · 触发阈值 ${Math.round((c.request.ratio ?? 0.9) * 100)}%` : ""}</p>
        <div class="compression-heading-actions"><span class="ui-badge is-warning">确认前不写入</span><button class="text-button" data-action="cancel-compression">取消压缩</button></div>
      </header>
      <div class="compression-panels">
        <aside class="compression-messages">
          <header class="compression-panel-heading"><div><span class="workspace-kicker">01 · SOURCE</span><strong>选择原始消息</strong></div><span>${c.messages.length} 条</span></header>
          <div class="compression-message-list">${renderCompressionMessages()}</div>
          <footer>
            <button class="text-button danger" data-action="compression-delete-selection" ${c.selected.size ? "" : "disabled"}>直接删除</button>
            <button class="primary" data-action="compression-join-selection" ${c.selected.size ? "" : "disabled"}>加入计划（${compressionPanel.selectionRanges(c.selected).length} 个范围）</button>
          </footer>
        </aside>
        <section class="compression-plan">
          <header class="compression-panel-heading"><div><span class="workspace-kicker">02 · PLAN</span><strong>审阅变更计划</strong></div><span>${c.ranges.length} 个范围</span></header>
          ${renderCompressionRanges()}
          <div class="compression-stats">
            <span><small>变更前</small><strong>${stats.beforeCount} 条 · ${formatTokens(stats.beforeTokens)} tokens</strong></span>
            <span><small>预计变更后</small><strong>${stats.afterCount} 条 · ${formatTokens(stats.afterTokens)} tokens</strong></span>
          </div>
          <details class="compression-mapping-details" ${stats.mapping.length ? "open" : ""}><summary>来源映射 · ${stats.mapping.length} 项</summary><ul class="compression-mapping">${stats.mapping.map(item => `<li>${escapeHtml(item.label)} → ${item.delete ? "删除" : item.restore ? "恢复原消息" : "压缩块"}</li>`).join("")}</ul></details>
          <footer>
            <span class="muted tiny">只有确认后才会写入 checkpoint</span><button class="primary" data-action="confirm-compression" ${compressionReady() ? "" : "disabled"}>确认压缩并继续</button>
          </footer>
        </section>
      </div>
    </section>`;
  if (listScrollTop != null) document.querySelector(".compression-message-list").scrollTop = listScrollTop;
  if (planScrollTop != null) document.querySelector(".compression-plan").scrollTop = planScrollTop;
}

function refreshCompressionStats() {
  if (state.view !== "compress") return;
  const c = state.compression;
  const stats = compressionPanel.beforeAfter(c.messages, c.ranges);
  const statsNode = document.querySelector(".compression-stats");
  const mappingNode = document.querySelector(".compression-mapping");
  if (statsNode) {
    statsNode.innerHTML = `<span><small>变更前</small><strong>${stats.beforeCount} 条 · ${formatTokens(stats.beforeTokens)} tokens</strong></span><span><small>预计变更后</small><strong>${stats.afterCount} 条 · ${formatTokens(stats.afterTokens)} tokens</strong></span>`;
  }
  if (mappingNode) {
    mappingNode.innerHTML = stats.mapping.map(item => `<li>${escapeHtml(item.label)} → ${item.delete ? "删除" : item.restore ? "恢复原消息" : "压缩块"}</li>`).join("");
  }
  const confirm = document.querySelector('[data-action="confirm-compression"]');
  if (confirm) confirm.disabled = !compressionReady();
}

function joinCompressionSelection() {
  const c = state.compression;
  const plannedSourceIds = new Set(c.ranges.flatMap(range => range.source_ids || []));
  const ranges = compressionPanel.selectionRanges(c.selected).map(range => ({
    source_ids: c.messages.slice(range.start, range.end + 1).map(message => message.id),
    replacement: "",
    generated: false,
    restore: false,
    summarizing: false,
  })).filter(range => range.source_ids.every(id => !plannedSourceIds.has(id)));
  c.ranges.push(...ranges);
  c.selected = new Set();
  renderCompress();
}

function deleteCompressionSelection() {
  const c = state.compression;
  const plannedSourceIds = new Set(c.ranges.flatMap(range => range.source_ids || []));
  const ranges = compressionPanel.selectionRanges(c.selected).map(range => ({
    source_ids: c.messages.slice(range.start, range.end + 1).map(message => message.id),
    delete: true,
  })).filter(range => range.source_ids.every(id => !plannedSourceIds.has(id)));
  c.ranges.push(...ranges);
  c.selected = new Set();
  renderCompress();
}


async function summarizeRange(rangeIndex) {
  const c = state.compression;
  const range = c.ranges[rangeIndex];
  if (!range || range.summarizing) return;
  const indexes = range.source_ids.map(id => c.messages.findIndex(message => message.id === id));
  const slice = c.messages.slice(Math.min(...indexes), Math.max(...indexes) + 1);
  // 每范围独立 busy 标记：多个范围可并发生成摘要
  range.summarizing = true;
  range.error = "";
  renderCompress();
  try {
    const result = await api("/desktop/api/compression/summarize", {
      method: "POST",
      body: JSON.stringify({
        messages: slice,
        forbid_terms: state.compression.quick
          ? [state.compression.quick.keyword]
          : [],
      }),
    });
    range.replacement = result.summary;
    range.generated = true;
    range.restore = false;
  } catch (error) { range.error = error.message; }
  finally { range.summarizing = false; renderCompress(); }
}

async function confirmCompression() {
  const c = state.compression;
  if (c.busy) return;
  const task = state.tasks.find(item => item.task_id === c.taskId) || activeTask();
  if (!task) return setStatus("当前没有活动任务", true);
  const ranges = c.ranges.map(range => range.delete
    ? { source_ids: range.source_ids, delete: true }
    : range.restore
      ? { source_ids: range.source_ids, restore: true }
      : { source_ids: range.source_ids, replacement: range.replacement });
  c.busy = true;
  try {
    if (c.quick) {
      // 快捷关键字压缩：先机械剥离禁用词再写回，不走 resume
      const keyword = c.quick.keyword;
      await api("/desktop/api/compression/quick-apply", {
        method: "POST",
        body: JSON.stringify({
          task_id: task.task_id,
          ranges,
          scrub_terms: [keyword],
        }),
      });
      // 快捷 apply 无 run/SSE，需手动重载会话与上下文树，否则压缩块不会立刻显示
      await refreshActiveAfterTxn();
      closeCompressionView();
      setStatus(`已关键字快捷压缩「${keyword}」，上下文已更新`);
      return;
    }
    const run = await api(`/desktop/api/threads/${task.thread_id}/runs/resume`, {
      method: "POST",
      body: JSON.stringify({ resume: { type: "compression", decision: "apply", ranges } }),
    });
    closeCompressionView();
    listenToRun(run);
    setStatus("压缩已确认，Agent 继续运行…");
  } catch (error) {
    // 上下文已变化（部分消息被折叠进块）：清掉 busy，重新从最新 checkpoint 加载并命中关键词
    if (c.quick && error?.detail?.code === "context_changed") {
      c.busy = false; // 必须清：否则 openCompressionView 开头的忙守卫会短路，刷新不会发生
      setStatus("上下文已更新，已按最新内容重新加载压缩面板");
      return openCompressionView(task, null, c.quick.keyword);
    }
    setStatus(error.message, true);
  }
  finally { c.busy = false; }
}

async function cancelCompression() {
  const c = state.compression;
  if (c.busy) return;
  const task = state.tasks.find(item => item.task_id === c.taskId) || activeTask();
  if (!task) return setStatus("当前没有活动任务", true);
  c.busy = true;
  try {
    if (c.quick) {
      // 快捷关键字压缩不经 interrupt/resume 通道（quick-apply 直接写回，无中断可取消）：
      // 取消只需关闭本地面板并清空状态，调用 resume 只会得到 409「无可恢复的承诺流程」。
      closeCompressionView();
      setStatus("已取消关键字快捷压缩");
      return;
    }
    const run = await api(`/desktop/api/threads/${task.thread_id}/runs/resume`, {
      method: "POST",
      body: JSON.stringify({ resume: { type: "compression", decision: "cancel" } }),
    });
    closeCompressionView();
    listenToRun(run);
    setStatus("已取消压缩，Agent 原样继续…");
  } catch (error) { setStatus(error.message, true); }
  finally { c.busy = false; }
}

async function refreshTasks() { return replaceTasks(await api("/desktop/api/tasks")); }

// 一次 run/事务结束后重载任务列表、上下文树与当前任务详情，使对话区反映最新消息。
// run 结束事件与快捷压缩 apply 共用，避免两处重复刷新逻辑。渲染由调用方负责。
async function refreshActiveAfterTxn() {
  await refreshTasks();
  await hydrateContextTrees();
  if (state.activeTaskId) await hydrateActive();
}

async function createTask(event) {
  event.preventDefault();
  if (state.creatingTask) return;
  const path = document.querySelector("#workspacePath").value.trim();
  const title = document.querySelector("#threadTitle").value.trim();
  if (!path || !title) return;
  state.creatingTask = true;
  const submit = document.querySelector('#taskForm button[type="submit"]');
  if (submit) submit.disabled = true;
  try {
    const workspace = await api("/desktop/api/workspaces", { method: "POST", body: JSON.stringify({ path }) });
    const task = await api(`/desktop/api/workspaces/${workspace.workspace_id}/threads`, { method: "POST", body: JSON.stringify({ title }) });
    dialog.close(); replaceTasks([...state.tasks, task]); state.activeTaskId = task.task_id; state.view = "focus";
    await hydrateActive(); render();
  } catch (error) { setStatus(error.message, true); }
  finally {
    state.creatingTask = false;
    if (submit?.isConnected) submit.disabled = false;
  }
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
  try {
  if (button.dataset.action === "toggle-material") { state.openMaterial = state.openMaterial === materialId ? null : materialId; return renderFocus(); }
  if (button.dataset.action === "open-material") {
    // f18:材料「查看」→ 右侧文件面板(不再全屏替换)
    if (!material) return setStatus("材料不存在", true);
    if (!pluginViewForMaterial(material)) return setStatus("当前没有启用的材料查看器", true);
    state.filesPanel = { ...material };
    render();
  }
  if (button.dataset.action === "open-file-panel") {
    // f18:消息文件卡片 → 右侧文件面板打开(优先匹配已登记材料,否则按文件名)
    const fileName = button.dataset.fileName;
    const taskId = state.activeTaskId;
    const material = (state.materials.get(taskId) || [])
      .find(item => item.relative_path === fileName || item.path === fileName);
    const target = material
      ? { ...material }
      : { relative_path: fileName, path: fileName };
    if (!pluginViewForMaterial(target)) return setStatus("当前没有启用的文件查看器", true);
    state.filesPanel = target;
    render();
  }
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
  } catch (error) {
    setStatus(error.message, true);
  }
}

async function switchTask(taskId) {
  const requestId = ++taskSwitchSequence;
  cancelPendingViewRequests();
  if (state.view === "focus") persistFocusState();
  state.filesPanel = null;
  state.activeTaskId = taskId;
  state.view = "focus";
  render();
  await hydrateActive(taskId);
  if (requestId !== taskSwitchSequence || state.activeTaskId !== taskId) return;
  const projectionStatus = state.details.get(taskId)?.context?.projection_status || "root";
  if (!["root", "valid", "repaired", "approved"].includes(projectionStatus)) return reopenContextDecision(taskId);
  render();
}

async function goFocusHome() {
  // 若当前任务是「无工作区模式」任务，点击「任务」默认回到最近的工作区任务
  if (activeTask()?.harness_mode === "assembly") {
    const recent = [...state.tasks].reverse().find(task => task.harness_mode !== "assembly");
    if (recent) return switchTask(recent.task_id);
  }
  const taskId = state.activeTaskId;
  if (!taskId) return;
  if (state.view === "focus") persistFocusState();
  cancelPendingViewRequests();
  state.inspector.open = false;
  state.view = "focus";
  render();
  await hydrateActive(taskId);
  if (state.activeTaskId === taskId && state.view === "focus") render();
}

// === 会话生命周期：归档 / 恢复 / 删除 + 设置面板 ===

function sessionLifecycle(task) {
  return task?.lifecycle || "active";
}

function contextHasDescendants(contextId, tree) {
  return (Array.isArray(tree) ? tree : []).some(node => node.parents?.[0]?.context_id === contextId);
}

async function archiveContext(contextId, cascade) {
  try {
    await api(`/desktop/api/contexts/${contextId}/archive${cascade ? "?cascade=true" : ""}`, { method: "POST" });
    setStatus(cascade ? "已级联归档会话" : "已归档会话");
    return refreshAfterSessionChange();
  } catch (error) { return setStatus(error.message, true); }
}

async function unarchiveContext(contextId) {
  try {
    await api(`/desktop/api/contexts/${contextId}/unarchive`, { method: "POST" });
    setStatus("已恢复会话");
    if (settingsDialog?.open) await openSettings();
    return refreshAfterSessionChange();
  } catch (error) { return setStatus(error.message, true); }
}

async function deleteContext(contextId, cascade) {
  const message = cascade
    ? "确定永久删除该会话及其全部派生后代？此操作不可恢复。"
    : "确定永久删除该会话？此操作不可恢复。";
  if (!confirm(message)) return;
  try {
    await api(`/desktop/api/contexts/${contextId}${cascade ? "?cascade=true" : ""}`, { method: "DELETE" });
    setStatus(cascade ? "已级联删除会话" : "已删除会话");
    if (settingsDialog?.open) settingsDialog.close();
    return refreshAfterSessionChange();
  } catch (error) { return setStatus(error.message, true); }
}

async function refreshAfterSessionChange() {
  return bootstrap();
}

function toggleSelectionMode() {
  state.selectionMode = !state.selectionMode;
  if (!state.selectionMode) state.selectedContextIds = new Set();
  return render();
}

function toggleSelectSession(contextId) {
  const ids = new Set(state.selectedContextIds);
  ids.has(contextId) ? ids.delete(contextId) : ids.add(contextId);
  state.selectedContextIds = ids;
  if (state.view === "map" && state.mapViewMode === "tree") {
    state.mapTreeFocusKey = `context:${contextId}`;
    return renderMap(state.mapTreeFocusKey);
  }
  return render();
}

async function batchDeleteSessions(cascade) {
  const selected = [...state.selectedContextIds];
  if (!selected.length) { setStatus("请先选中要删除的会话", true); return; }
  const message = cascade
    ? `确定永久删除选中的 ${selected.length} 个会话及其全部派生后代？此操作不可恢复。`
    : `确定永久删除选中的 ${selected.length} 个会话？此操作不可恢复。`;
  if (!confirm(message)) return;
  try {
    await api("/desktop/api/contexts/batch-delete", {
      method: "POST",
      body: JSON.stringify({ context_ids: selected, cascade }),
    });
    setStatus(cascade ? "已级联批量删除会话" : "已批量删除会话");
    state.selectionMode = false;
    state.selectedContextIds = new Set();
    return refreshAfterSessionChange();
  } catch (error) { return setStatus(error.message, true); }
}

async function openSettings() {
  try {
    const sessions = await api("/desktop/api/sessions/archived");
    const list = document.querySelector("#archivedSessions");
    if (list) {
      list.innerHTML = sessions.length
        ? sessions.map(item => `
            <div class="archived-session-item" data-context-id="${escapeHtml(item.context_id)}">
              <div class="archived-session-info">
                <span class="archived-session-title">${escapeHtml(item.title)}</span>
                <span class="archived-session-meta">${escapeHtml(item.workspace_name || "本地工作区")} · ${escapeHtml(item.context_id.slice(0, 8))}</span>
              </div>
              <div class="archived-session-actions">
                <button class="text-button" data-action="unarchive-context" data-context-id="${escapeHtml(item.context_id)}">恢复</button>
                <button class="text-button danger" data-action="delete-context" data-context-id="${escapeHtml(item.context_id)}">删除</button>
              </div>
            </div>`).join("")
        : '<p class="muted tiny" style="padding:var(--space-3)">暂无已归档会话</p>';
    }
  } catch (error) { setStatus(error.message, true); }
  if (!settingsDialog?.open) settingsDialog?.showModal();
}

// f18:拦截消息内链接导航(避免 Electron 窗口跳转到本地路径白屏)。
// capture 阶段拦截 + stopPropagation。判据:
//   - file:// 链接:直接阻止导航,按文件打开面板;
//   - http/https 链接:若 host 含中文或本地文件扩展结尾(markdown-it linkify 把
//     file:///C:/.../中文名.md 误解析成 http://中文名.md/ 的形态)→ 视为文件误解析,
//     阻止导航并从误解析的 host 提取文件名打开面板;真实外链放行。
document.addEventListener("click", event => {
  const anchor = event.target.closest("a[href]");
  if (!anchor) return;
  const href = anchor.getAttribute("href") || "";
  const isHttp = /^https?:\/\//i.test(href);
  const hostPart = isHttp ? href.replace(/^https?:\/\//i, "").split("/")[0] : "";
  const looksLikeFileHost = isHttp && (
    /\.(md|txt|png|jpe?g|pdf|docx?)$/i.test(hostPart)
    || /[一-鿿]/.test(hostPart)
  );
  if (!isHttp && !href.startsWith("file:")) return; // 非 http/file 链接不处理
  if (isHttp && !looksLikeFileHost) {
    event.preventDefault();
    event.stopPropagation();
    if (typeof window.focusDesktop?.openExternal !== "function") return setStatus("当前运行环境无法打开外部链接", true);
    Promise.resolve(window.focusDesktop.openExternal(href)).catch(error => setStatus(error.message, true));
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  // 显示文字可能含图标/说明,路径只取自 href(file URL)或兼容分支的 host。
  const fileName = fileNameFromLinkHref(href, hostPart);
  if (FILE_VIEWABLE_RE.test(fileName)) {
    const taskId = state.activeTaskId;
    const material = (state.materials.get(taskId) || [])
      .find(item => item.relative_path === fileName || item.path === fileName);
    const target = material ? { ...material } : { relative_path: fileName, path: fileName };
    if (!pluginViewForMaterial(target)) return setStatus("当前没有启用的文件查看器", true);
    state.filesPanel = target;
    render();
  }
}, true);

// 全局错误可见化:任何未捕获异常显示在状态栏,避免白屏时无从排查
window.addEventListener("error", event => {
  try { setStatus(`页面错误: ${event.message}`, true); } catch { /* 初始阶段无状态栏 */ }
});
window.addEventListener("unhandledrejection", event => {
  try { setStatus(`未处理 Promise 错误: ${String(event.reason || "").slice(0, 120)}`, true); } catch { /* ignore */ }
});

function runUiAction(action) {
  return Promise.resolve()
    .then(action)
    .catch(error => {
      const message = error instanceof Error ? error.message : String(error || "未知错误");
      setStatus(`页面操作失败: ${message}`, true);
    });
}

async function handleDocumentClick(event) {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  if (action === "reload") return bootstrap();
  if (action === "new-task") return dialog.showModal();
  if (action === "close-task-dialog") return dialog.close();
  if (action === "pick-workspace") return pickWorkspace();
  if (action === "select-skill") return selectSkill(button.dataset.pickerKind, button.dataset.skillName);
  if (action === "select-commit") return selectCommitCommand(button.dataset.pickerKind);
  if (action === "remove-skill") return removeSkill(button.dataset.pickerKind, button.dataset.skillName);
  if (action === "show-map") { if (state.view === "focus") persistFocusState(); cancelPendingViewRequests(); state.inspector.open = false; state.view = "map"; return render(); }
  if (action === "set-map-view") {
    const mode = button.dataset.mapView;
    if (!["tree", "cards"].includes(mode)) return;
    state.mapViewMode = mode;
    state.mapTreeFocusKey = "";
    if (mode === "tree") state.mapExpandedForTaskId = null;
    persistMapViewPreferences();
    renderMap();
    app.querySelector(`[data-action="set-map-view"][data-map-view="${mode}"]`)?.focus();
    return;
  }
  if (action === "toggle-map-workspace") {
    const id = button.dataset.workspaceId;
    if (id) toggleMapExpansion("workspace", id, `workspace:${id}`);
    return;
  }
  if (action === "toggle-map-context") {
    const id = button.dataset.contextId;
    if (id) toggleMapExpansion("context", id, `context:${id}`);
    return;
  }
  if (action === "collapse-map-tree") return collapseMapTree();
  if (action === "show-contexts") return openInspector("context", button);
  if (action === "show-agents") return openInspector("agents", button);
  if (action === "open-inspector-tab") return openInspector(button.dataset.inspectorTab, button);
  if (action === "show-plugins") return openPluginsView();
  if (action === "show-memory") return openMemoryView();
  if (action === "new-memory") return openMemoryCompose(false);
  if (action === "select-memory") { state.memory = { ...state.memory, selectedId: button.dataset.memoryId || null }; return render(); }
  if (action === "pick-session") return pickMemorySession(button.dataset.contextId);
  if (action === "pick-session-source") { addSessionSource(button.dataset.contextId, button.dataset.contextTitle); return; }
  if (action === "toggle-message") { toggleMemoryMessage(button.dataset.messageId); return; }
  if (action === "add-checked-messages") { addMessagesSource(state.memory.selectedMessageIds || []); return; }
  if (action === "add-text") { addTextSource(state.memory.textSelection); return; }
  if (action === "remove-collected") { removeCollectedSource(Number(button.dataset.collectedIndex)); return; }
  if (action === "enter-merge-sources") { enterMergeSources(); return; }
  if (action === "cancel-merge-sources") { cancelMergeSources(); return; }
  if (action === "toggle-merge-source") { toggleMergeSource(Number(button.dataset.sourceIndex)); return; }
  if (action === "confirm-merge-sources") { confirmMergeSources(); return; }
  if (action === "recompress-memory") { recompressMemory(); return; }
  if (action === "set-memory-mode") { syncMemoryField("contentMode", button.dataset.mode || "complete"); return; }
  if (action === "toggle-memory-group") { event.preventDefault(); toggleMemoryGroup(button.dataset.group); return; }
  if (action === "save-memory") return saveMemory();
  if (action === "cancel-compose") { state.memory = { ...state.memory, composing: false, draft: null, sessions: [], activeSessionId: null, enabledMessages: [], selectedMessageIds: [], activeMessageId: null, collectedSources: [] }; return render(); }
  if (action === "edit-memory") { state.memory = { ...state.memory, selectedId: button.dataset.memoryId || null }; return openMemoryCompose(true); }
  if (action === "delete-memory") return deleteMemory(button.dataset.memoryId);
  if (action === "show-assembly") {
    try {
      const task = await api("/desktop/api/assembly/task");
      // 只在首次新建时把装配任务并入 state.tasks，避免每次全量刷新（大任务列表耗时）
      if (task?.task_id && !state.tasks.some(item => item.task_id === task.task_id)) replaceTasks([...state.tasks, task]);
      if (task?.task_id) return switchTask(task.task_id);
    } catch (error) { setStatus(error.message, true); }
    return render();
  }
  if (action === "refresh-plugins") { await hydratePlugins(); return render(); }
  if (action === "filter-plugins") {
    state.plugins.filter = button.dataset.pluginStatus || "all";
    const visible = state.plugins.plugins.filter(item => state.plugins.filter === "all" || item.status === state.plugins.filter);
    if (!visible.some(item => item.name === state.plugins.selectedName)) state.plugins.selectedName = visible[0]?.name || null;
    return renderPlugins();
  }
  if (action === "select-plugin") {
    state.plugins.selectedName = button.dataset.pluginName || null;
    return renderPlugins();
  }
  if (action === "focus-home" && state.activeTaskId) return goFocusHome();
  if (action === "derive-context") return openContextEditor(state.activeTaskId);
  if (action === "open-settings") return openSettings();
  if (action === "close-settings") return settingsDialog.close();
  if (action === "archive-context") return archiveContext(button.dataset.contextId, false);
  if (action === "cascade-archive-context") return archiveContext(button.dataset.contextId, true);
  if (action === "unarchive-context") return unarchiveContext(button.dataset.contextId);
  if (action === "delete-context") return deleteContext(button.dataset.contextId, false);
  if (action === "cascade-delete-context") return deleteContext(button.dataset.contextId, true);
  if (action === "toggle-selection-mode") return toggleSelectionMode();
  if (action === "toggle-select-session") return toggleSelectSession(button.dataset.taskId);
  if (action === "batch-delete-selected") return batchDeleteSessions(false);
  if (action === "batch-cascade-delete-selected") return batchDeleteSessions(true);
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
  if (action === "toggle-compress-message") {
    state.compression.selected = compressionPanel.toggleSelect(
      state.compression.selected, Number(button.dataset.index)
    );
    return renderCompress();
  }
  if (action === "compression-join-selection") return joinCompressionSelection();
  if (action === "compression-delete-selection") return deleteCompressionSelection();
  if (action === "delete-range") {
    const range = state.compression.ranges[Number(button.dataset.rangeIndex)];
    if (range) { range.delete = true; range.replacement = ""; range.summarizing = false; }
    return renderCompress();
  }
  if (action === "undelete-range") {
    const range = state.compression.ranges[Number(button.dataset.rangeIndex)];
    if (range) { range.delete = false; }
    return renderCompress();
  }
  if (action === "summarize-range") return summarizeRange(Number(button.dataset.rangeIndex));
  if (action === "remove-range") {
    state.compression.ranges.splice(Number(button.dataset.rangeIndex), 1);
    return renderCompress();
  }
  if (action === "restore-range") {
    const range = state.compression.ranges[Number(button.dataset.rangeIndex)];
    if (range) { range.restore = true; range.replacement = ""; }
    return renderCompress();
  }
  if (action === "unrestore-range") {
    const range = state.compression.ranges[Number(button.dataset.rangeIndex)];
    if (range) { range.restore = false; }
    return renderCompress();
  }
  if (action === "confirm-compression") return confirmCompression();
  if (action === "cancel-compression") return cancelCompression();
  if (action === "abandon-commitment") return abandonCommitment();
  if (action === "exit-draft") { if (await saveDraft(state.activeTaskId)) { state.view = "map"; return render(); } return; }
  if (action === "set-patrol-mode") {
    const draft = syncDraftFromDom();
    const mode = button.dataset.mode;
    if (!setDraftPatrolMode(draft, mode)) return;
    renderDraft();
    if (await saveDraft(state.activeTaskId, { syncDom: false })) renderDraft();
    return;
  }
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
  if (action === "agent-details") return openAgentDetails(button.dataset.agentId);
  if (action === "agent-list") { state.agentDetails = { agentId: null, messages: [], curation: null, busy: false }; return renderInspector(); }
  if (action === "refresh-agent-details") return refreshAgentDetails();
  if (action === "retry-agent-details") return retryAgentDetails();
  if (action === "cancel-agent-details") return cancelAgentDetails();
  if (action === "set-curation-state") return setCurationTrackingState(button.dataset.state);
  if (action === "load-more-curation") return refreshAgentDetails({ appendCuration: true });
  if (action === "open-managed-context") {
    state.agentDetails = { agentId: null, messages: [], curation: null, busy: false };
    return switchTask(button.dataset.contextId);
  }
  if (action === "interrupt-main-run") return interruptMainRun();
}

document.addEventListener("click", event => {
  runUiAction(() => handleDocumentClick(event));
});

document.addEventListener("input", event => {
  if (event.target.id === "mainInput") updateAtHighlight(event.target);
  if (event.target.matches("[data-skill-input]")) updateSkillMenu(event.target, true);
  if (event.target.matches("[data-draft-field],[data-message-field],[data-equipment],[data-permission],[data-curation-policy]")) scheduleDraftSave();
  if (event.target.matches("[data-memory-field]")) syncMemoryField(event.target.dataset.memoryField, event.target.value);
  if (event.target.matches("[data-source-field]")) editSourceText(Number(event.target.dataset.sourceIndex), event.target.value);
  if (event.target.matches("[data-segment-field]")) editMemorySegment(Number(event.target.dataset.segmentIndex), event.target.dataset.segmentField, event.target.value);
  if (event.target.matches("[data-range-index]")) {
    const range = state.compression.ranges[Number(event.target.dataset.rangeIndex)];
    if (range) {
      range.replacement = event.target.value;
      range.generated = false;
      refreshCompressionStats();
    }
  }
});

// 主输入框滚动时同步 @命令高亮 backdrop（textarea 的 scroll 不冒泡，用捕获监听）。
document.addEventListener("scroll", event => {
  if (event.target.id === "mainInput") updateAtHighlight(event.target);
}, true);

// 记忆库：划选原文文字 → 高亮 → 采集 text + 字符区间 + message_id（供「加入选中文字」/拖拽）。
document.addEventListener("mouseup", event => {
  if (state.view !== "memory" || !state.memory?.composing) return;
  const pre = event.target.closest("[data-selectable-text]");
  if (!pre) return;
  const sel = window.getSelection();
  const text = sel ? sel.toString() : "";
  if (!text) return;
  const container = pre.closest("[data-message-id]");
  const messageId = container ? container.getAttribute("data-message-id") : null;
  // 用选区文本在全文中的位置估算字符区间（同一条消息内划选足够准确）。
  const full = pre.textContent || "";
  const start = full.indexOf(text);
  const end = start >= 0 ? start + text.length : null;
  state.memory = {
    ...state.memory,
    textSelection: text,
    textMessageId: messageId,
    textRange: start >= 0 ? { start, end } : { start: 0, end: text.length },
  };
});

function handleMapTreeKeydown(event, treeItem) {
  const keys = ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"];
  if (state.view !== "map" || state.mapViewMode !== "tree" || !keys.includes(event.key)) return false;
  const elements = [...app.querySelectorAll(".map-collapsible-tree [role='treeitem'][data-tree-key]")];
  const currentIndex = elements.indexOf(treeItem);
  const items = elements.map(item => ({
    key: item.dataset.treeKey,
    parentKey: item.dataset.treeParentKey || "",
    hasChildren: item.dataset.treeHasChildren === "true",
    expanded: item.getAttribute("aria-expanded") === "true",
  }));
  const action = mapCollapsibleView.treeKeyAction(event.key, currentIndex, items);
  event.preventDefault();
  if (!action) return true;
  if (action.type === "focus") {
    elements.forEach((item, index) => item.tabIndex = index === action.index ? 0 : -1);
    const target = elements[action.index];
    state.mapTreeFocusKey = target.dataset.treeKey;
    target.focus();
    return true;
  }
  const kind = treeItem.dataset.treeKind;
  const id = kind === "workspace" ? treeItem.dataset.workspaceId : treeItem.dataset.taskId;
  if (id) setMapExpansion(kind, id, action.type === "expand", treeItem.dataset.treeKey);
  return true;
}

document.addEventListener("keydown", event => {
  if (event.key === "Escape" && contextPointerDrag) {
    event.preventDefault();
    return cancelContextPointerDrag();
  }
  if (event.key === "Escape" && state.inspector.open) {
    event.preventDefault();
    closeInspector();
    return;
  }
  const mapTreeItem = event.target.closest?.(".map-collapsible-tree [role='treeitem'][data-tree-key]");
  if (mapTreeItem && handleMapTreeKeydown(event, mapTreeItem)) return;
  const inspectorTab = event.target.closest?.('[role="tab"][data-inspector-tab]');
  if (inspectorTab && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    const tabs = [...appInspector.querySelectorAll('[role="tab"][data-inspector-tab]')];
    const index = tabs.indexOf(inspectorTab);
    const nextIndex = event.key === "Home" ? 0
      : event.key === "End" ? tabs.length - 1
        : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    openInspector(tabs[nextIndex].dataset.inspectorTab, tabs[nextIndex]);
    tabs[nextIndex].focus();
    return;
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
  const mainEnter = input.id === "mainInput" && event.key === "Enter";
  if (mainEnter && (event.isComposing || event.keyCode === 229)) return;
  if (mainEnter && (event.shiftKey || event.altKey || event.ctrlKey || event.metaKey)) return;
  const matches = pickerMatches(input);
  const action = skillPicker.keyAction(event.key);
  if (!action || matches === null) {
    if (mainEnter && matches === null) {
      event.preventDefault();
      sendMain();
    }
    return;
  }
  const kind = input.dataset.skillInput;
  if (action === "close") {
    event.preventDefault();
    document.querySelector(`#${kind}SkillList`).hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    return;
  }
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
  if (!event.target.matches(".draft-advanced")) return;
  state.openDraftSection = event.target.open ? "system" : null;
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
  // 左下栏「记忆源」卡拖拽重排：携带来源索引，与三栏「添加来源」payload 区分。
  const sourceCard = event.target.closest(".memory-source-edit[data-source-index]");
  if (sourceCard) {
    event.dataTransfer.setData("application/x-focus-source-index", sourceCard.dataset.sourceIndex);
    event.dataTransfer.effectAllowed = "move";
    return;
  }
  const sessionItem = event.target.closest(".memory-session-item");
  if (sessionItem) {
    event.dataTransfer.setData("application/x-focus-memory", JSON.stringify({ kind: "session", context_id: sessionItem.dataset.contextId, title: sessionItem.dataset.contextTitle }));
    event.dataTransfer.effectAllowed = "copy";
    return;
  }
  const memoryMsg = event.target.closest(".memory-msg");
  if (memoryMsg) {
    const id = memoryMsg.dataset.messageId;
    const checked = state.memory.selectedMessageIds || [];
    const ids = checked.length ? checked : [id];
    event.dataTransfer.setData("application/x-focus-memory", JSON.stringify({ kind: "messages", message_ids: ids }));
    event.dataTransfer.effectAllowed = "copy";
    return;
  }
  const sourceText = event.target.closest(".memory-source-text");
  if (sourceText) {
    const sel = window.getSelection();
    const text = (sel && sel.toString()) || sourceText.textContent || "";
    event.dataTransfer.setData("application/x-focus-memory", JSON.stringify({ kind: "text", text }));
    event.dataTransfer.effectAllowed = "copy";
  }
});

document.addEventListener("dragover", event => {
  const card = event.target.closest(".task-card, .map-collapsible-context-item");
  if (card && event.dataTransfer.types.includes("application/x-focus-soldier")) { event.preventDefault(); card.classList.add("drop-target"); }
  const row = event.target.closest(".message-editor");
  if (row && event.dataTransfer.types.includes("application/x-focus-message")) event.preventDefault();
  if (event.target.closest(".memory-source-edit-list") && event.dataTransfer.types.includes("application/x-focus-source-index")) { event.preventDefault(); event.dataTransfer.dropEffect = "move"; }
  if (event.target.closest(".memory-compose") && event.dataTransfer.types.includes("application/x-focus-memory")) { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; }
});

document.addEventListener("dragleave", event => event.target.closest(".task-card, .map-collapsible-context-item")?.classList.remove("drop-target"));
document.addEventListener("drop", event => {
  const card = event.target.closest(".task-card, .map-collapsible-context-item");
  if (card && event.dataTransfer.getData("application/x-focus-soldier")) { event.preventDefault(); return openDraft(card.dataset.taskId); }
  const row = event.target.closest(".message-editor");
  const from = Number(event.dataTransfer.getData("application/x-focus-message"));
  if (row && Number.isInteger(from)) {
    event.preventDefault(); const draft = syncDraftFromDom(); moveMessageGroup(draft.history_messages, from, Number(row.dataset.index)); renderDraft(); scheduleDraftSave();
  }
  const sourceCard = event.target.closest(".memory-source-edit[data-source-index]");
  const fromIndex = Number(event.dataTransfer.getData("application/x-focus-source-index"));
  if (sourceCard && Number.isInteger(fromIndex) && event.dataTransfer.getData("application/x-focus-source-index") !== "") {
    event.preventDefault();
    moveCollectedSource(fromIndex, Number(sourceCard.dataset.sourceIndex));
    return;
  }
  if (event.target.closest(".memory-compose") && event.dataTransfer.getData("application/x-focus-memory")) {
    event.preventDefault();
    try {
      const payload = JSON.parse(event.dataTransfer.getData("application/x-focus-memory"));
      dropMemoryPayload(payload);
    } catch { /* 非法拖拽载荷，忽略 */ }
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
document.addEventListener("submit", event => {
  if (event.target.id !== "agentInspectorContinueForm") return;
  event.preventDefault();
  runUiAction(continueAgentDetails);
});
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

runUiAction(bootstrap);
