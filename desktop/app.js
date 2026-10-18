/*
 * 本文件对外提供应用组合根；输入为真实 API/Live 与用户导航动作，输出为六页及原业务入口。
 * 历史提问提供复制、编辑重发与从此重试，助手提供复制、引用和重新回答；重试从冻结 checkpoint 的提问前缀派生，保留原会话与附件清单，并复用普通提交恢复。任务工具从完整消息快照导出 Markdown。
 * 工作区绑定与投递复用 WorkspacePatrolIntake；桌面工作台使用独立 userData 仓库，浏览器使用 IndexedDB，恢复后开放编辑；机器人继续使用独立输入仓库。来源导航按最新请求身份生效，迟到读取不能覆盖后来选择。
 * 任务与图卡共用 Run SSE 订阅；图卡通过只读观察器呈现真实角色与短尾部，按工作区和 Run 隔离，退出页面释放观察而不触发审批。
 * 工作流为沿原控制器装配页面、按任务保留草稿，卡片/列表偏好仅留在页面状态。示例：renderPlugins() 展示 registry 的当前状态。
 * Loop 激活由服务端解析初始 Run 装备，客户端不从详情缓存猜测权限；继承请求输出完整有效授权快照。Main 接口的 direct_message 回执仅表示受理，不写 active_run 或材料执行历史。默认 Patrol，任务列表与材料检查器承接传统操作，复杂能力通过明确次级入口使用原业务用例。
 * 控制材质复用语义 token；检查器显隐使用 FocusSurfaceTransition 保留壳层，退出即隔离命中、结束后退出布局；业务状态仍来自原 API/Live。
 * Loop 介入提交保留原文；成功后只清空同一 Context、模式和内容的当前表单，重绘替换及提交期间的新草稿不被旧节点重置影响。全图节点在选择/装备模式复用原操作，普通检查仍不改变发送目标；选择重绘恢复原节点焦点。
 * 任务工具提供手工整理并复用既有 quick-apply；已有压缩待决仍使用原 resume。授权/等待表单解析复用共享视图，文件预览暂时隐藏材料检查器，关闭后恢复原 UI 偏好；独立会话的内部目录不呈现为用户已绑定工作区。
 * 会话 standard Patrol 由独立 Document/Workbench/Branches 模块负责 typed 左右编排、精确来源选择、保存、只读请求预览和精确执行分支；本文件组合路由、装备、检查器与运行订阅，材料变更按所属任务使检查过期并按当前页面刷新，保留编辑挂载。
 * 传统任务与独立会话在同任务刷新时保持输入区及其祖先挂载，只更新消息与外围状态；后台其他任务结束只更新任务列表，回执不清除正在组字的草稿。
 * 示例：refreshMaterialView(taskId, true) 使该任务材料变更后的检查过期并刷新当前路由；await sendMain()。模型配置显式选择 Provider 与协议，详情展示有效协议。
 * 本文件对外提供 Focus 桌面宿主的状态协调与原生 DOM 渲染。输入为同源 desktop API、SSE、
 * preload 运行时信息和用户操作，输出为持久导航、任务工作区、检查器、常驻会话 Patrol 小兵、
 * 对话/Context/Agent/Commitment/压缩/插件与模块化 Agent Loop Portfolio 控制台、结构化 Mission 编辑、Context Expansion 资源授权/状态、自主压缩授权/审计/来源恢复、终止 Loop 退出/后继 Loop 准备等视图；逐轮材料以有序 binding 草稿和独立图片必看
 * 集合表达，自定义分组是服务端事实，分组模式与折叠是任务 UI 偏好。具体工作流为在任务切换时加载
 * 材料、历史和分组，用纯函数规范化选择/分组，再通过单一异步事件边界更新 DOM 和运行状态；
 * Loop 运行活动由 journal 增量投影到选中 Context 的当前执行区，分钟计时只刷新活动栏而不重取历史会话；任务详情刷新带请求身份守卫，迟到或跨 Context 的响应不得覆盖更新的会话状态；会话容器只有一个
 * 写者——DOM 写入侧：顶层重建先接管既有会话节点，随即统一经对账写入内容（写入侧分只读的"计划"与唯一
 * 改动 DOM 的"应用"两个阶段，决策前不改动活动树）；对账单元以身份键匹配，承诺轨迹面板与审批/恢复面板
 * 作为显式保留节点原位存活，流式占位也由写入侧按 run 身份就地创建并同步，本文件不直接向会话容器增删
 * 子节点；快照是该 run 的权威状态，命中消息 id 或同一 run 即回收流式占位，占位不叠加同一 Run 内前一条
 * 消息的正文与推理；会话只呈现人类可读摘要，工具参数、工具输出与推理全文均不进入 DOM，由会话视图在
 * 用户展开时按 conversationEventIndex 按需生成；Main Run 提交与流状态按 task/run identity 隔离，Loop wait request
 * 使用独立响应面和按 request identity 持久草稿，沿用当前 Mission 的恢复需显式确认且不发送普通聊天消息；会话渲染走两步：buildUnits 产出带内容签名的单元，
 * _unitHtml 按签名命中缓存，因此连续相同帧为零重建、增量帧只重建变化单元；流式正文按顶层块缓存，
 * 只重渲染未闭合尾块，自动解析以 `STREAM_TEXT_LIMIT` 为界（超限后完整累积正文、冻结可见块，
 * 用户可按需展开最新全文）；同一 tick 内的多次状态变化由 scheduleRender
 * 合并为一次；快照帧在写入前先登记滚动基线（此时的高度与贴底判定才是写入前的状态），写入后由写入侧按
 * 阅读意图与阅读锚点决定跟随或回正；作曲区未发送内容的事实来源是按任务归属的草稿镜像
 * （`FocusComposerDraft`），输入事件即镜像、去抖落盘、页面隐藏与卸载流程各补一次落盘，因此任何界面重建
 * 与模式切换都不丢内容，也不依赖 `beforeunload`；作曲区输入与操作分行，文件模式使用紧凑菜单；
 * 三档模式分别保存新会话默认与当前会话常驻值，
 * 当前会话模式经独立 API 串行保存，成功后才更新界面及后续运行策略；
 * 沙箱状态视图展示已准备或曾尝试准备的工作区及持久限制；
 * 主任务 Composer 仅在存在错误或需处理的提醒时显示反馈，不为常态和运行提示预留空行；
 * 普通任务投递前持久化原文、材料及完整请求；未确认受理时按原身份重放，显式进入编辑后才新建身份。回执确认后与本地刷新错误分开处理。
 * 审批弹窗区分单次放宽和常驻切换；Agent Loop 的 snapshot、sequence reducer、断线重放和重同步
 * 由独立 Live Store/Connection 负责，切换 Loop 时清除旧投影，启动边界观察意外连接拒绝并归入连接状态；Expansion 表单和阻断视图委托 FocusLoopExpansionBudget，普通 API 响应统一委托无 DOM 的 FocusHttpResponse 解码，本文件只组合页面生命周期和控制请求。
 * 独立 Patrol 入口经系统文件夹选择器选取路径，picker 仅确认本次选择，显式进入才登记绑定；原导航偏好保存已确认工作区，重载不读取历史目录列表。
 * 全图复用共享玻璃图、精确关系与原检查器，局部更新保留视口和历史版本；批选、装备及只读检查沿既有任务身份分流，生命周期变更成功后重读关系。
 * 普通会话照旧，workspace Loop 不进入旧 Mission/介入控制台，草稿和单路 Live 由专责控制器保持；Patrol 不展示普通会话检查器，壳层刷新保留本次路径选择。
 * 记忆库沿用同一 CRUD，区分普通记忆与演化策略编辑；列表响应按导航身份、压缩响应按请求及编辑快照隔离，迟到结果不得覆盖后续页面、选择或草稿。
 */
"use strict";

// Shared reader also supplies the Markdown instance used by streaming blocks.
const messageReading = window.FocusMessageReading.create({ translate: (key, fallback) => uiText(key, fallback) });
const mdRenderer = messageReading.markdown;
const keywordCommand = window.FocusKeywordCommand;
const interfaceI18n = window.FocusI18n || {
  apply() {},
  locale: () => "zh-CN",
  setLocale: () => false,
  t: key => key,
};

function uiText(key, fallback, variables = {}) {
  const translated = interfaceI18n.t(key, variables);
  // 文案表不可用时回落中文文案，但占位符替换必须与走文案表时一致，
  // 否则同一条提示在两种路径下会呈现未替换的 {name}
  return (translated === key ? fallback : translated)
    .replace(/\{(\w+)\}/g, (_match, name) => String(variables[name] ?? `{${name}}`));
}

const runtime = window.focusDesktop?.runtime?.() || {
  apiBase: location.protocol === "file:" ? "http://127.0.0.1:8765" : location.origin,
  session: "focus-dev-session",
};
const materialContentLoader = window.MaterialContentLoader
  ? new window.MaterialContentLoader(runtime)
  : {
      bindAll: async () => {},
      load: async () => { throw new Error("材料内容加载器不可用"); },
      releaseMaterial() {},
      releaseTask() {},
      releaseAll() {},
    };

const MAP_VIEW_PREFERENCES_KEY = "focus-map-view-v1";
const preferenceStorage = {
  getItem(key) {
    try { return (window.focusDesktop?.preferences || localStorage).getItem(key); }
    catch (error) { console.warn("读取界面偏好失败:", key, error.message); return null; }
  },
  setItem(key, value) {
    try { (window.focusDesktop?.preferences || localStorage).setItem(key, value); }
    catch (error) { console.warn("保存界面偏好失败:", key, error.message); }
  },
};

function readMapViewPreferences() {
  const fallback = { mode: "graph", workspaceIds: [], contextIds: [] };
  try {
    const raw = preferenceStorage.getItem(MAP_VIEW_PREFERENCES_KEY);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw);
    return {
      mode: ["graph", "tree", "cards"].includes(parsed?.mode) ? parsed.mode : fallback.mode,
      workspaceIds: Array.isArray(parsed?.workspaceIds) ? parsed.workspaceIds.filter(id => typeof id === "string") : [],
      contextIds: Array.isArray(parsed?.contextIds) ? parsed.contextIds.filter(id => typeof id === "string") : [],
    };
  } catch { return fallback; }
}

const mapViewPreferences = readMapViewPreferences();

/* 壳层三栏可拖拽布局：唯一尺寸来源是 .app-shell 上的 --shell-nav-width / --inspector-width
   自定义属性，JS 只写入内联值；media query 只改其样式表默认值，因此用户偏好总是胜过断点。 */
const SHELL_LAYOUT_KEY = "focus-shell-layout";

// 壳层三栏拖拽边界：与 tokens.css 的 min/max 一致；导航可折叠到 0（collapse）即最小值 0。
const SHELL_LAYOUT_BOUNDS = Object.freeze({
  navMin: 0,
  navMax: 288,
  inspectorMin: 240,
  inspectorMax: 480,
  workspaceMin: 320,
});

/* 整页缩放：渲染器持有快捷键与持久化，经 preload 桥由主进程应用（唯一缩放权威）。
   级别沿用 Chromium zoomLevel 语义：0 为原始大小，每级约 ×1.2。 */
const ZOOM_LEVEL_KEY = "focus-zoom-level";
const ZOOM_LEVEL_MIN = -3;
const ZOOM_LEVEL_MAX = 5;

const state = {
  view: "patrol",
  patrolWorkspace: window.FocusWorkspacePatrolIntake.readBinding(localStorage),
  tasks: [],
  activeTaskId: null,
  details: new Map(),
  drafts: new Map(),
  materials: new Map(),
  materialSelections: new Map(),
  composerAttachments: new Map(),
  composerFileReferences: new Map(),
  mainPreparations: new Map(),
  materialGroups: new Map(),
  materialHistory: new Map(),
  agents: new Map(),
  skillCatalogs: new Map(),
  capabilityRefreshResults: new Map(),
  contextTrees: new Map(),
  contextDraft: null,
  pickerActive: { main: 0, draft: 0 },
  equipment: { models: [], tools: [], skills: [], permissions: [] },
  soldierArmed: false,
  selectionMode: false,
  selectedContextIds: new Set(),
  historySearch: { query: "", results: [], loading: false, error: "", hasMore: false, unavailable: 0, sequence: 0 },
  historyReader: { contextId: null, detail: null, loading: false, error: "", targetKey: "", messageId: null, start: 0, sequence: 0 },
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
  streams: new Map(),
  streamBuffers: new Map(),
  streamFrames: new Map(),
  composerErrors: new Map(),
  mainRecoveryRestored: new Map(),
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
  accessReviews: { panel: null, payload: null, taskId: null, key: null, busy: false },
  plugins: { plugins: [], interfaces: {}, traces: [], filter: "all", selectedName: null },
  memory: { memories: [], selectedId: null, composing: false, draft: null, sessions: [], activeSessionId: null, enabledMessages: [], selectedMessageIds: [], activeMessageId: null, collectedSources: [], textSelection: "", textMessageId: null, textRange: null, editorRatio: 0.5, sourceRatio: 0.5, contentMode: "complete", segments: [], expandedGroups: [], mergeMode: false, selectedSourcesForMerge: [], sessionScrollTop: 0 },
  loop: { loopId: null, loading: false, revisionSnapshot: null },
  inspector: { open: window.innerWidth > 1100, tab: "materials", returnFocus: null },
  filesPanel: null,   // f18:右侧文件面板当前打开的 material(relative_path 等)
  panelWidth: normalizePanelWidth(preferenceStorage.getItem("focus-panel-width") || 400),
  shellLayout: normalizeShellLayout(readShellLayout(), window.innerWidth),
  zoomLevel: normalizeZoomLevel(preferenceStorage.getItem(ZOOM_LEVEL_KEY)),
};

const app = document.querySelector("#app");
const statusNode = document.querySelector("#globalStatus");
const accessPendingNode = document.querySelector("#accessPending");
const appInspector = document.querySelector("#appInspector");
const inspectorContent = document.querySelector("#inspectorContent");
const shellTaskTitle = document.querySelector("#shellTaskTitle");
const shellTaskMeta = document.querySelector("#shellTaskMeta");
const dialog = document.querySelector("#taskDialog");
dialog?.addEventListener("cancel", event => { event.preventDefault(); void window.FocusSurfaceTransition.visible(dialog, false, { modal: true }); });
const settingsDialog = document.querySelector("#settingsDialog");
settingsDialog?.addEventListener("cancel", event => { event.preventDefault(); void window.FocusSurfaceTransition.visible(settingsDialog, false, { modal: true }); });
const skillPicker = window.FocusSkillPicker;
const contextEditor = window.FocusContextEditor;
const compressionPanel = window.FocusCompressionPanel;
const pluginView = window.FocusPluginView;
const mapCollapsibleView = window.FocusMapCollapsibleView;
const memoryView = window.FocusMemoryView;
const conversationEvents = window.FocusConversationEvents;
const conversationReconciler = window.FocusConversationReconciler;
const conversationRender = window.FocusConversationRender;
const conversationView = window.FocusConversationView;
const conversationEventIndex = new Map();
let renderScheduled = false;

function scheduleRender() {
  if (renderScheduled) return;
  renderScheduled = true;
  const flush = () => {
    renderScheduled = false;
    render();
  };
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(flush);
  else flush();
}
const patrolPresence = window.FocusPatrolPresence;
const contextCuratorPresentation = window.FocusContextCuratorPresentation;
const patrolAvatar = window.FocusPatrolAvatar;
const runMaterialPicker = window.runMaterialPicker;
const materialGrouping = window.materialGrouping;
const accessMode = window.FocusAccessMode;
const composerDraft = window.FocusComposerDraft;
const composerFiles = window.FocusComposerFiles;
let mainFilePicker = null;

function fileReferences(taskId) {
  if (!state.details.has(taskId) && !state.composerFileReferences.has(taskId)) return [];
  if (!state.composerFileReferences.has(taskId)) state.composerFileReferences.set(taskId,
    [...(state.details.get(taskId)?.ui_state?.file_references || [])]);
  return state.composerFileReferences.get(taskId);
}

function updateFileReferences(taskId, refs) {
  state.composerFileReferences.set(taskId, refs);
  const detail = state.details.get(taskId);
  if (detail) detail.ui_state = { ...(detail.ui_state || {}), file_references: refs };
  if (state.activeTaskId === taskId) {
    const cards = document.querySelector("#composerFileReferences");
    if (cards) cards.innerHTML = composerFiles.cards(refs);
  }
  void persistUiState(taskId).catch(error => setStatus(`文件引用保存失败：${error.message}`, true));
}

function addFileReference(value, taskId = state.activeTaskId) {
  if (!taskId || !value.path) return;
  const ref = { ...fileLinkReference(value), ...(typeof value.text === "string" ? { text: value.text } : {}) };
  const refs = fileReferences(taskId);
  if (!refs.some(item => composerFiles.key(item) === composerFiles.key(ref))) updateFileReferences(taskId, [...refs, ref]);
}

function addTaskFileReference(value, taskId) {
  const task = state.tasks.find(item => item.task_id === taskId);
  let path = value.path;
  if (task?.workspace_path && !/^(?:[a-z]:[\\/]|[\\/])/i.test(path)) path = `${task.workspace_path.replace(/[\\/]$/, "")}/${path}`;
  addFileReference({ ...value, path, workspace_id: task?.workspace_id }, taskId);
}

function consumeFileReferences(taskId, submitted) {
  if (!submitted.length) return;
  const sent = new Set(submitted.map(composerFiles.key));
  updateFileReferences(taskId, fileReferences(taskId).filter(ref => !sent.has(composerFiles.key(ref))));
}

function mountMainFilePicker(taskId, input) {
  if (mainFilePicker?.input === input && mainFilePicker.taskId === taskId) return;
  mainFilePicker?.destroy(); mainFilePicker = null;
  const menu = document.querySelector("#composerFileMenu");
  if (!input?.addEventListener || menu?.id !== "composerFileMenu") return;
  mainFilePicker = composerFiles.bind({ input, menu,
    search: query => api(`/desktop/api/tasks/${encodeURIComponent(taskId)}/files/search?q=${encodeURIComponent(query)}`),
    onSelect: ref => addFileReference(ref, taskId),
  });
  Object.assign(mainFilePicker, { input, taskId });
}
const conversationReference = window.FocusConversationReference;
const referenceDrafts = conversationReference.createDrafts(localStorage);
const referenceSelection = conversationReference.mount({
  document, window,
  getTaskId: () => state.view === "focus" && document.querySelector("#mainInput") ? state.activeTaskId : null,
  onQuote: setMainReference,
  onStatus: setStatus,
});

function mainReference(taskId) {
  const recovery = taskRunOperations?.recovery(taskId);
  if (recovery && !recovery.editing) {
    const body = JSON.parse(recovery.body);
    return (body.run || body).message_reference || null;
  }
  return referenceDrafts.get(taskId, state.details.get(taskId)?.ui_state?.message_reference);
}

function setMainReference(reference) {
  const taskId = state.activeTaskId;
  const recovery = taskRunOperations?.recovery(taskId);
  if (recovery && !recovery.editing) return setStatus("请先选择「编辑后重新发送」以修改引用", true);
  referenceDrafts.set(taskId, reference);
  const detail = state.details.get(taskId);
  if (detail) detail.ui_state = { ...(detail.ui_state || {}), message_reference: reference };
  const node = document.querySelector("#composerReference");
  if (node) node.innerHTML = conversationReference.render(reference);
  void persistFocusState();
  document.querySelector("#mainInput")?.focus();
}

function jumpToReference(reference) {
  if (reference.task_id !== state.activeTaskId || state.view !== "focus") return setStatus("请在来源会话中查看引用", true);
  const find = () => [...document.querySelectorAll("#conversation [data-message-id]")]
    .find(node => node.dataset.messageId === reference.message_id);
  let article = find();
  // Earlier messages are already in the snapshot but rendered in bounded windows.
  while (!article && document.querySelector('[data-action="load-earlier-conversation"]')) {
    const previous = conversationWindowLimit(reference.task_id);
    loadEarlierConversation();
    article = find();
    if (conversationWindowLimit(reference.task_id) === previous) break;
  }
  if (!article) return setStatus("原消息已不在当前历史中，引用原文仍已保留", true);
  article.scrollIntoView({ block: "center", behavior: "smooth" });
  article.classList.add("is-reference-source");
  setTimeout(() => article.classList.remove("is-reference-source"), 2000);
  const content = article.querySelector(".message-content");
  if (content) conversationReference.selectRange(content, reference, window);
}
const composingMainInputs = new WeakSet();
const taskRunOperations = window.FocusTaskRunOperations?.create();
const loopApi = window.FocusLoopApi?.create(runtime);
const runStreams = window.FocusRunStreamSubscriptions?.create(runtime);
const desktopPatrol = window.focusDesktop?.patrol?.enabled ? window.focusDesktop.patrol : null;
let workspacePatrolIntake = null;
async function createWorkspacePatrolIntake() {
  const recovery = window.focusDesktop?.workspacePatrolRecovery || await window.FocusPatrolBrowserRecovery.open("workbench");
  return window.FocusWorkspacePatrolIntake.create({ runtime, api: loopApi, recovery,
    onSaveError: error => setStatus(`Patrol 草稿保存失败：${error.message}`, true),
    selectWorkspace: window.focusDesktop?.selectWorkspace, accessMode: () => accessMode.readNewSessionDefault(window.localStorage) });
}
let workspacePatrolController = null;
let workspacePatrolMount = Promise.resolve();
let workspacePatrolNavigation = 0;
let workspacePatrolPicker = null;
let patrolWorkspaceBeforePick = null;
let mapPortfolio = { workspaceId: null, loading: false, snapshot: null, manifest: null, error: null };
let mapPortfolioSequence = 0;
let mapContextInspector = null;
let mapRelatedOnly = false;
let mapLive = null;
const mapRunPreviews = createRunPreviews((values, structural) => {
  if (state.view !== "map" || state.mapViewMode !== "graph") return;
  if (structural) scheduleMapGraphPatch();
  else window.FocusPortfolioMapView.patchPreviews(app.querySelector("[data-map-graph]"), values);
});
const loopWaitDrafts = window.FocusLoopWaitRequestView?.createDraftStore();
const loopWaitUi = new Map();
const loopLiveSelectors = window.FocusLoopLiveSelectors;
const loopLiveStore = window.FocusLoopLiveStore?.create();
const loopStore = window.FocusLoopStore?.create();
const loopView = window.FocusLoopView;
const loopMissionEditor = window.FocusLoopMissionEditor;
const loopConsoleView = window.FocusLoopConsoleView;
const loopConsoleStore = window.FocusLoopConsoleStore?.create();
const loopConsoleController = loopApi && loopConsoleStore && window.FocusLoopConsoleController
  ? window.FocusLoopConsoleController.create({
      api: loopApi,
      store: loopConsoleStore,
      onChange: () => { if (state.view === "loop") patchLoopConsole(); },
    })
  : null;
const loopLiveConnection = loopApi && loopLiveStore && window.FocusLoopLiveConnection
  ? window.FocusLoopLiveConnection.create({ api: loopApi, store: loopLiveStore })
  : null;
const loopLegacyConnection = loopApi && loopStore && window.FocusLoopLegacyConnection
  ? window.FocusLoopLegacyConnection.create({ api: loopApi, loopStore, consoleController: loopConsoleController, onChange: scheduleLoopLifecyclePatch })
  : null;
const loopConnection = runtime.features?.liveLoopProjection === false ? loopLegacyConnection : loopLiveConnection;
let loopLifecycleFrame = null;
let projectedLoopState = null;
loopLiveStore?.subscribe(liveState => {
  loopStore?.projectHistory(liveState.history);
  loopConsoleStore?.projectHistory(liveState.history);
  if (!liveState.projection) {
    projectedLoopState = null;
    loopStore?.clearLive(liveState.connection);
    scheduleLoopLifecyclePatch();
    return;
  }
  if (projectedLoopState === liveState.projection) {
    loopStore?.projectConnection(liveState.connection);
    scheduleLoopLifecyclePatch();
    return;
  }
  projectedLoopState = liveState.projection;
  loopStore?.projectLive(liveState.projection, liveState.connection);
  const previousConversation = loopConsoleStore?.get().conversation;
  const previousAuthority = loopConsoleStore?.get().authorityRevision;
  const nextConsole = loopConsoleStore?.projectLive(liveState.projection, loopLiveSelectors);
  if (previousAuthority != null && previousAuthority !== nextConsole?.authorityRevision) {
    void loopConsoleController?.reloadFacts();
  }
  if (previousConversation && nextConsole?.selectedContextId === previousConversation.context_id && !nextConsole.conversation) {
    void loopConsoleController?.selectContext(nextConsole.selectedContextId, { preserveSelection: true });
  }
  scheduleLoopLifecyclePatch();
});
const accessApproval = window.FocusAccessApproval;
interfaceI18n.apply(document);
// f18 插件视图宿主:插件前端脚本加载后经此注册视图与材料打开器
window.__focusPluginViews = window.__focusPluginViews || {};
const pluginViews = window.__focusPluginViews;
let contextUiSequence = 0;
let contextRequestSequence = 0;
let contextPointerDrag = null;
let contextUndoTimer = null;
let agentDetailsRequestSequence = 0;
let taskSwitchSequence = 0;
// 各资源独立提交；导航复用在途请求，事务刷新仍以最新请求为准。
const taskResourceLoads = new Map();
let compressionRequestSequence = 0;
let draftOpenRequestSequence = 0;
let pluginHydrationSequence = 0;
let pluginViewRequestSequence = 0;
let memoryViewRequestSequence = 0;
let memoryHydrationSequence = 0;
let memoryCompressionRequestSequence = 0;
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

function shellLayoutDefaults() {
  return {
    navWidth: 204,
    inspectorWidth: Math.round(Math.min(SHELL_LAYOUT_BOUNDS.inspectorMax, Math.max(SHELL_LAYOUT_BOUNDS.inspectorMin, window.innerWidth * 0.22))),
    navCollapsed: false,
    // 用户是否手动拖拽/折叠过；未定制时壳层完全由 CSS 断点默认驱动，不写内联宽度。
    customized: false,
  };
}

function readShellLayout() {
  try {
    const raw = preferenceStorage.getItem(SHELL_LAYOUT_KEY);
    if (!raw) return shellLayoutDefaults();
    const parsed = JSON.parse(raw);
    return {
      navWidth: Number.isFinite(Number(parsed.navWidth)) ? Number(parsed.navWidth) : shellLayoutDefaults().navWidth,
      inspectorWidth: Number.isFinite(Number(parsed.inspectorWidth)) ? Number(parsed.inspectorWidth) : shellLayoutDefaults().inspectorWidth,
      navCollapsed: Boolean(parsed.navCollapsed),
      customized: true,
    };
  } catch {
    return shellLayoutDefaults();
  }
}

// 钳制三栏宽度：保证每栏在各自边界内，且三栏始终能容纳工作区保底宽度。
function normalizeShellLayout(layout, viewportWidth = window.innerWidth) {
  const bounds = SHELL_LAYOUT_BOUNDS;
  const width = Number.isFinite(Number(viewportWidth)) && Number(viewportWidth) > 0 ? Number(viewportWidth) : 1200;
  const navCollapsed = Boolean(layout?.navCollapsed);
  let navWidth = Number.isFinite(Number(layout?.navWidth)) ? Number(layout.navWidth) : bounds.navMin;
  let inspectorWidth = Number.isFinite(Number(layout?.inspectorWidth)) ? Number(layout.inspectorWidth) : bounds.inspectorMin;
  navWidth = clamp(navWidth, navCollapsed ? 0 : bounds.navMin, bounds.navMax);
  inspectorWidth = clamp(inspectorWidth, bounds.inspectorMin, bounds.inspectorMax);
  // 三栏总和不能超出可用宽度，为工作区保留最小宽度；超出时优先挤压左、右栏。
  const maxSides = Math.max(bounds.navMax + bounds.inspectorMax - 1, width - bounds.workspaceMin);
  if (navWidth + inspectorWidth > maxSides) {
    const scale = maxSides / (navWidth + inspectorWidth);
    navWidth = Math.round(clamp(navWidth * scale, navCollapsed ? 0 : bounds.navMin, bounds.navMax));
    inspectorWidth = Math.round(clamp(inspectorWidth * scale, bounds.inspectorMin, bounds.inspectorMax));
    // 二次钳制：缩放到边界后若仍超出，回退到收缩侧的最小各自值。
    if (navWidth + inspectorWidth > maxSides) {
      const overflow = navWidth + inspectorWidth - maxSides;
      if (navCollapsed) inspectorWidth -= overflow;
      else inspectorWidth = Math.max(bounds.inspectorMin, inspectorWidth - overflow);
    }
  }
  return { navWidth: Math.round(navWidth), inspectorWidth: Math.round(inspectorWidth), navCollapsed, customized: Boolean(layout?.customized) };
}

function clamp(value, min, max) {
  return Math.round(Math.min(max, Math.max(min, value)));
}

// 把当前布局写回 .app-shell 的内联自定义属性（唯一尺寸来源）。不触发 render，避免重绘卡顿。
// 未定制（用户从未拖拽/折叠）时不写内联值，让 CSS 断点默认值独立驱动，避免覆盖 1100px 自动收窄。
function applyShellLayout(layout) {
  const shell = document.querySelector(".app-shell");
  if (!shell?.style) return;
  if (layout.customized) {
    const navWidth = layout.navCollapsed ? 0 : layout.navWidth;
    shell.style.setProperty("--shell-nav-width", `${navWidth}px`);
    shell.style.setProperty("--inspector-width", `${layout.inspectorWidth}px`);
  }
  shell.classList?.toggle("is-nav-collapsed", layout.navCollapsed);
  syncShellResizerVisibility();
  syncNavToggleButton(layout.navCollapsed);
}

function persistShellLayout() {
  try {
    preferenceStorage.setItem(SHELL_LAYOUT_KEY, JSON.stringify(state.shellLayout));
  } catch { /* 只读存储忽略 */ }
}

// 同步两个 resizer 手柄的可见性：导航折叠到 0 时隐藏导航侧手柄；
// 检查器隐藏或窄屏覆盖态时隐藏检查器侧手柄（样式表也已用媒体查询覆盖窄屏）。
function syncShellResizerVisibility() {
  const shell = document.querySelector(".app-shell");
  if (!shell?.querySelector) return;
  const navResizer = shell.querySelector(".shell-resizer-nav");
  if (navResizer?.setAttribute) navResizer.setAttribute("aria-hidden", String(state.shellLayout.navCollapsed));
  const inspectorResizer = shell.querySelector(".shell-resizer-inspector");
  if (inspectorResizer?.setAttribute) inspectorResizer.setAttribute("aria-hidden", String(appInspector.hidden));
}

function syncNavToggleButton(navCollapsed) {
  // 折叠触发器是左上角 logo（.app-mark）。它保持 logo 图片不变（无痕），
  // 仅用 aria-expanded / aria-label 反映展开/收起状态。
  const toggle = document.querySelector(".app-mark");
  if (!toggle?.setAttribute) return;
  toggle.setAttribute("aria-expanded", String(!navCollapsed));
  toggle.setAttribute("aria-label", navCollapsed ? uiText("nav.expand", "展开导航") : uiText("nav.collapse", "收起导航"));
}

function setShellNavCollapsed(navCollapsed) {
  if (Boolean(state.shellLayout.navCollapsed) === navCollapsed) return;
  state.shellLayout.navCollapsed = navCollapsed;
  state.shellLayout.customized = true;
  // 折叠态把 nav 宽度覆盖为 0，展开则恢复到 navWidth。折叠态同时记下当前 navWidth 便于恢复。
  applyShellLayout(state.shellLayout);
  persistShellLayout();
}

// 给两个 resizer 手柄绑定指针拖拽：把相邻栏宽度写回 .app-shell 内联自定义属性。
function bindShellResizers() {
  const shell = document.querySelector(".app-shell");
  if (!shell?.querySelector) return;
  const resizers = [
    { node: shell.querySelector(".shell-resizer-nav"), target: "nav" },
    { node: shell.querySelector(".shell-resizer-inspector"), target: "inspector" },
  ];
  for (const { node, target } of resizers) {
    if (!node || node.dataset.shellResizerBound) continue;
    node.dataset.shellResizerBound = "true";
    let frame = null;
    node.addEventListener("pointerdown", event => {
      if (event.button !== 0) return;
      event.preventDefault();
      node.setPointerCapture?.(event.pointerId);
      const startX = event.clientX;
      const startNav = state.shellLayout.navWidth;
      const startInspector = state.shellLayout.inspectorWidth;
      const move = moveEvent => {
        if (frame != null) cancelAnimationFrame(frame);
        frame = requestAnimationFrame(() => {
          frame = null;
          const delta = moveEvent.clientX - startX;
          const draft = { ...state.shellLayout, navCollapsed: false, customized: true };
          // 导航↔工作区：向右拖(+)放大导航；工作区↔检查器：向左拖(-)放大检查器。
          if (target === "nav") draft.navWidth = startNav + delta;
          else draft.inspectorWidth = startInspector - delta;
          state.shellLayout = normalizeShellLayout(draft, shell.clientWidth || window.innerWidth);
          applyShellLayout(state.shellLayout);
        });
      };
      const finish = () => {
        if (frame != null) cancelAnimationFrame(frame);
        frame = null;
        try { node.releasePointerCapture?.(event.pointerId); } catch { /* ignore */ }
        node.removeEventListener("pointermove", move);
        node.removeEventListener("pointerup", finish);
        node.removeEventListener("pointercancel", finish);
        node.removeEventListener("lostpointercapture", finish);
        persistShellLayout();
      };
      node.addEventListener("pointermove", move);
      node.addEventListener("pointerup", finish);
      node.addEventListener("pointercancel", finish);
      node.addEventListener("lostpointercapture", finish);
    });
  }
}

// 恢复路径：缺失/非法/超出边界一律回落 100%（级别 0），避免脏数据破坏界面。
function normalizeZoomLevel(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0;
  const rounded = Math.round(numeric);
  return rounded < ZOOM_LEVEL_MIN || rounded > ZOOM_LEVEL_MAX ? 0 : rounded;
}

// 步进路径：钳制到边界，使边界处继续按同方向快捷键不再改变界面。
function clampZoomLevel(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0;
  return Math.min(ZOOM_LEVEL_MAX, Math.max(ZOOM_LEVEL_MIN, Math.round(numeric)));
}

function zoomPercent(level) {
  return Math.round(100 * (1.2 ** level));
}

// 不显示视觉百分比：仅把当前级别写入无障碍实时区域。
function announceZoom(level) {
  const node = document.querySelector("#zoomAnnouncement");
  if (node) node.textContent = uiText("zoom.announce", "缩放 {percent}%", { percent: zoomPercent(level) });
}

// 应用缩放并持久化；主进程应用（唯一权威），渲染器只发起与记录。
function applyZoomLevel(level, options = {}) {
  const next = normalizeZoomLevel(level);
  state.zoomLevel = next;
  try { preferenceStorage.setItem(ZOOM_LEVEL_KEY, String(next)); } catch { /* 只读存储忽略 */ }
  try { window.focusDesktop?.setZoomLevel?.(next); } catch { /* 无桥环境忽略 */ }
  if (options.announce !== false) announceZoom(next);
  return next;
}

function changeZoomLevel(delta) {
  const current = clampZoomLevel(state.zoomLevel ?? 0);
  const next = clampZoomLevel(current + delta);
  if (next === current) return current; // 已达边界：不发起无效调用
  return applyZoomLevel(next);
}

// 启动恢复：读回持久化级别（非法/越界回落 0），启动时不播报以免打断。
function restoreZoomLevel() {
  let stored = null;
  try { stored = preferenceStorage.getItem(ZOOM_LEVEL_KEY); } catch { /* 只读存储忽略 */ }
  return applyZoomLevel(normalizeZoomLevel(stored), { announce: false });
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
  try {
    return await window.FocusHttpResponse.decodeResponse(response, { operation: `${options.method || "GET"} ${path}` });
  } catch (error) {
    if (error instanceof window.FocusHttpResponse.DesktopApiError) {
      const detail = error.detail;
      const fallback = `HTTP ${error.status}${error.statusText ? ` ${error.statusText}` : ""}`;
      error.message = typeof detail === "string"
        ? (detail.trim() || fallback)
        : detail && typeof detail === "object"
          ? JSON.stringify(detail)
          : (error.bodyExcerpt.trim() || fallback);
    }
    throw error;
  }
}

function loadPluginScript(src) {
  if (pluginScriptAssets.has(src)) return pluginScriptAssets.get(src);
  const loading = new Promise(resolve => {
    const script = document.createElement("script");
    script.src = src;
    script.onload = () => resolve(true);
    script.onerror = () => {
      pluginScriptAssets.delete(src);
      script.remove();
      console.error("插件脚本加载失败:", src);
      resolve(false);
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
  memoryViewRequestSequence += 1;
}

async function hydratePluginAssets(bootstrapPlugins = null) {
  // f18: 按启用插件清单注入前端资源(css 并行、js 串行;entry.js 固定最后执行,
  // 保证插件视图/打开器注册时其依赖模块已加载);失败不阻塞桌面
  try {
    const data = bootstrapPlugins == null
      ? await api("/desktop/api/plugins") : { plugins: bootstrapPlugins };
    const active = (data.plugins || []).filter(plugin => plugin.status === "active");
    const failed = [];
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
        if (!await loadPluginScript(`/plugins/${encodeURIComponent(plugin.name)}/desktop/${encodeURIComponent(file)}`)) {
          failed.push(plugin.name);
          break;
        }
      }
    }
    if (failed.length) throw new Error(`无法加载插件: ${failed.join("、")}`);
    return true;
  } catch (error) {
    console.error("插件前端资源注入失败:", error);
    if (["focus", "plugins"].includes(state.view)) setStatus(`插件资源注入失败: ${error.message}`, true);
    return false;
  }
}

let pluginAssetsLoading = null;
function ensurePluginAssets(plugins = null) {
  if (!pluginAssetsLoading) {
    pluginAssetsLoading = hydratePluginAssets(plugins).then(loaded => {
      if (!loaded) pluginAssetsLoading = null;
      if (state.view === "focus") render();
    });
  }
  return pluginAssetsLoading;
}

// f18: 插件视图返回 Focus 对话页的统一钩子
window.__focusBackToFocus = () => {
  state.view = "focus";
  render();
};

// f18: 文件面板关闭钩子(viewer 面板内关闭按钮调用)
window.__focusCloseFilePanel = () => {
  releaseFileLinkPreview();
  state.filesPanel = null;
  render();
};

function pluginViewForMaterial(material) {
  return Object.values(pluginViews).find(view =>
    typeof view.supportsMaterial === "function" && view.supportsMaterial(material)
  ) || (window.FocusFilePreview ? {
    mountPanel(container, file, appState) {
      const name = file.relative_path || file.path;
      container.innerHTML = `<header class="file-panel-head"><strong>${escapeHtml(name)}</strong><button type="button" data-file-close>关闭</button></header><div data-generic-file></div>`;
      container.querySelector("[data-file-close]").addEventListener("click", () => window.__focusCloseFilePanel());
      const taskId = appState.activeTaskId;
      void window.FocusFilePreview.mount(container.querySelector("[data-generic-file]"), { name, target: { task_id: taskId, content_ref: name }, onReference: value => addTaskFileReference(value, taskId) });
    },
  } : null);
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
  return messageReading.renderContent(value);
}

async function copyMessageText(button, text) {
  return messageReading.copyText(button, text, { onStatus: (label, danger) => setStatus(label, danger) });
}

function conversationMarkdown(detail, task) {
  const title = String(task.title || uiText("conversation.title", "对话")).replace(/[\r\n]+/g, " ");
  const sections = [`# ${title}`];
  // 从完整快照展开压缩来源，不受会话 DOM 的窗口上限影响。
  for (const message of compressionPanel.expandForConversation(detail.messages || [])) {
    if (message.divider || compressionPanel.degradedParts(message)) continue;
    const kind = contextCuratorPresentation.messageSemantics(message).kind;
    if (kind !== "human" && kind !== "ai") continue;
    const text = messageBodyText(message);
    if (!text.trim()) continue;
    const role = kind === "human" ? uiText("conversation.user", "用户") : uiText("conversation.assistant", "助手");
    sections.push(`## ${role}\n\n${text}`);
  }
  return sections.join("\n\n---\n\n") + "\n";
}

function exportConversation() {
  const task = activeTask();
  const detail = task && state.details.get(task.task_id);
  if (!detail) return;
  const markdown = conversationMarkdown(detail, task);
  const filename = String(task.title || "conversation").replace(/[<>:"/\\|?*\x00-\x1f]/g, "_").slice(0, 100).replace(/[. ]+$/, "") || "conversation";
  const url = URL.createObjectURL(new Blob([markdown], { type: "text/markdown;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `${filename}.md`;
  try {
    document.body.append(link);
    link.click();
  } finally {
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

// 生成中的正文按「顶层块」切分后逐块渲染。
// 依据 markdown-it 的 token 契约:level 0 的 token 是成对(open/close)或单个块
// (fence / hr / code_block / html_block),因此 level 0 即块边界。块一旦闭合,
// 其渲染结果不再受后续增量影响(稳定段);只有最后一个尚未闭合的块可能被重新
// 解释为另一种构造(暂定尾部)——markdown 并非前缀稳定格式,这是其本质限制。
function streamMarkdownBlocks(text) {
  return conversationRender.markdownBlocks(mdRenderer, text);
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
  for (const taskId of taskResourceLoads.keys()) {
    if (!state.tasks.some(task => task.task_id === taskId)) taskResourceLoads.delete(taskId);
  }
  const selected = state.tasks.find(task =>
    task.task_id === state.activeTaskId && sessionLifecycle(task) === "active"
  );
  state.activeTaskId = selected?.task_id
    || state.tasks.find(task => sessionLifecycle(task) === "active")?.task_id
    || null;
  return activeTask();
}

async function bootstrap() {
  if (!workspacePatrolIntake) {
    workspacePatrolIntake = await createWorkspacePatrolIntake();
    state.patrolWorkspace = workspacePatrolIntake.readBinding();
    window.focusDesktop?.workspacePatrolRecovery?.onFlush(async token => {
      let error = null;
      try { await workspacePatrolIntake.flush(); }
      catch (failure) { error = failure.message; setStatus(`Patrol 草稿保存失败：${error}`, true); }
      await window.focusDesktop.workspacePatrolRecovery.flushed({ token, error });
    });
  }
  setStatus("正在连接…");
  performance.mark("focus:bootstrap-start");
  render();
  void ensureModelSetup().catch(error => setStatus(error.message, true));
  try {
    const data = await api("/desktop/api/bootstrap");
    replaceTasks(data.tasks);
    state.equipment = equipmentLoading ? { ...data.equipment, tools_status: "loading" } : data.equipment;
    performance.mark("focus:bootstrap-ready");
    setStatus("");
  } catch (error) {
    setStatus(error.message, true);
    // Patrol 有独立的数据入口，摘要读取失败不能卸载已挂载的页面。
    statusNode.insertAdjacentHTML("beforeend", ' <button data-action="reload">重试连接</button>');
    return false;
  }
  render();
  try {
    if (state.view === "focus") await hydrateActive();
    else if (state.view === "map") await hydrateContextTrees();
    else if (state.view === "draft") void hydrateEquipment();
    if (state.view !== "patrol") render();
  } catch (error) { setStatus(error.message, true); }
  await window.focusDesktop?.notifications?.ready?.();
  return true;
}

const TASK_RESOURCES = {
  detail: { suffix: "", label: "任务消息与运行状态", store: "details" },
  materials: { suffix: "/materials", label: "材料", store: "materials" },
  agents: { suffix: "/agents", label: "Agents", store: "agents" },
  skills: { suffix: "/skills", label: "Skills", store: "skillCatalogs" },
  groups: { suffix: "/material-groups", label: "材料分组", store: "materialGroups" },
  history: { suffix: "/material-history", label: "材料历史", store: "materialHistory" },
};

function renderTaskResourceStatus(taskId, resources) {
  const loads = taskResourceLoads.get(taskId);
  return resources.map(resource => {
    const load = loads?.[resource];
    const source = TASK_RESOURCES[resource];
    if (load?.status === "loading") return `<p class="muted tiny" role="status">${source.label}${state[source.store].has(taskId) ? "正在刷新" : "加载中"}…</p>`;
    if (load?.status !== "error") return "";
    return `<p class="ui-notice is-warning" role="status"><span>${source.label}加载失败：${escapeHtml(load.error)}</span><button type="button" data-action="retry-task-resource" data-task-id="${escapeHtml(taskId)}" data-resource="${resource}">重试</button></p>`;
  }).join("");
}

function refreshTaskResourceView(taskId, resource, { preserveMount = false } = {}) {
  if (state.activeTaskId !== taskId || state.view !== "focus") return;
  const load = taskResourceLoads.get(taskId)?.[resource];
  if (resource === "detail") {
    if (load?.status === "loaded" && !preserveMount) render();
    else {
      const status = document.querySelector("#taskDetailLoadStatus");
      if (status) status.innerHTML = renderTaskResourceStatus(taskId, ["detail"]);
    }
  } else if (resource === "skills") {
    const status = document.querySelector("#mainSkillLoadStatus");
    if (status) status.innerHTML = renderTaskResourceStatus(taskId, ["skills"]);
    const input = document.querySelector('#mainInput');
    if (input && document.activeElement === input) updateSkillMenu(input);
  } else {
    if (resource === "agents") updatePatrolAvatarLayer(taskId);
    if (resource === "materials" && load?.status === "loaded") refreshComposerAttachments(taskId);
    if (resource === "history" && load?.status === "loaded") refreshConversation(activeTask());
    if (state.inspector.tab === (resource === "agents" ? "agents" : "materials")) renderInspector();
  }
}

function hydrateTaskResource(taskId, resource, { reusePending = false, preserveMount = false } = {}) {
  const source = TASK_RESOURCES[resource];
  if (!taskId || !Object.hasOwn(TASK_RESOURCES, resource)) return Promise.resolve();
  let loads = taskResourceLoads.get(taskId);
  if (!loads) taskResourceLoads.set(taskId, loads = {});
  if (reusePending && loads[resource]?.status === "loading") return loads[resource].promise;
  const load = { status: "loading", error: "", promise: null };
  loads[resource] = load;
  load.promise = (async () => {
    let result;
    try { result = await api(`/desktop/api/tasks/${taskId}${source.suffix}`); }
    catch (error) {
      if (taskResourceLoads.get(taskId)?.[resource] !== load) return;
      load.status = "error";
      load.error = error.message;
      refreshTaskResourceView(taskId, resource, { preserveMount });
      return;
    }
    if (taskResourceLoads.get(taskId)?.[resource] !== load) return;
    state[source.store].set(taskId, resource === "skills" ? result.skills : result);
    load.status = "loaded";
    if (resource === "detail") applyTaskDetail(taskId, result, { preserveMount });
    if (resource === "agents") (result || []).forEach(agent => {
      if (["pending", "running"].includes(agent.latest_run?.status)) listenToRun(agent.latest_run);
    });
    refreshTaskResourceView(taskId, resource, { preserveMount });
    return result;
  })().finally(() => { load.promise = null; });
  refreshTaskResourceView(taskId, resource, { preserveMount });
  return load.promise;
}

function hydrateActive(taskId = state.activeTaskId, options = {}) {
  if (!taskId) return Promise.resolve();
  const detail = hydrateTaskResource(taskId, "detail", options);
  for (const resource of ["materials", "agents", "skills", "groups", "history"]) {
    void hydrateTaskResource(taskId, resource, options);
  }
  // 调用方只等待主体；附属资源自行更新对应区域和重试状态。
  return detail;
}

function applyTaskDetail(taskId, detail, { preserveMount = false } = {}) {
  const mainRecovery = taskRunOperations?.recovery(taskId);
  if (mainRecovery) {
    if (!mainRecovery.editing || !detail.ui_state?.input) detail.ui_state = { ...(detail.ui_state || {}), input: mainRecovery.input };
    if (state.mainRecoveryRestored.get(taskId) !== mainRecovery.request_id) {
      state.materialSelections.set(taskId, JSON.parse(mainRecovery.selectionSignature));
      state.mainRecoveryRestored.set(taskId, mainRecovery.request_id);
    }
  }
  if (state.activeTaskId === taskId && ["focus", "compress"].includes(state.view)) {
    reconcileCommitmentRecovery(detail);
    reconcileCompressionRecovery(detail);
    reconcileAccessReview(detail);
  }
  if (detail.active_run?.status === "pending" || detail.active_run?.status === "running") {
    listenToRun(detail.active_run);
  }
  updatePatrolAvatarLayer(taskId);
  if (!preserveMount && state.view === "focus" && state.activeTaskId === taskId) {
    void ensurePluginAssets();
    const workspaceId = state.tasks.find(task => task.task_id === taskId)?.workspace_id;
    if (workspaceId && !state.contextTrees.has(workspaceId)) void hydrateContextTrees([workspaceId]).then(() => {
      if (state.view === "focus" && state.activeTaskId === taskId) render();
    }).catch(error => {
      if (state.view === "focus" && state.activeTaskId === taskId) setStatus(`Context 目录加载失败: ${error.message}`, true);
    });
  }
}

function render() {
  if (state.view !== "focus" && appInspector.parentElement !== document.querySelector(".app-shell")) document.querySelector(".app-shell").append(appInspector);
  if (state.view !== "map") { stopMapLive(); mapContextInspector?.dispose(); mapContextInspector = null; mapRelatedOnly = false; }
  preferenceStorage.setItem("focus-interaction-entry", state.view === "patrol" ? "patrol" : "focus");
  if (state.view !== "patrol") {
    workspacePatrolController?.leave();
    workspacePatrolPicker?.dispose();
    workspacePatrolPicker = null;
  }
  const shownDraft = document.querySelector('#patrolWorkbench')?.dataset?.draftId;
  if (shownDraft && (state.view !== 'draft' || state.drafts.get(state.activeTaskId)?.draft_id !== shownDraft)) window.FocusPatrolWorkbench?.leave(shownDraft);
  if (state.view !== "loop") loopConnection?.stop();
  document.body.dataset.view = state.view;
  renderShellChrome();
  if (!state.tasks.length && ["draft", "compress", "context", "loop"].includes(state.view)) {
    app.replaceChildren(document.querySelector("#emptyTemplate").content.cloneNode(true));
    interfaceI18n.apply(app);
    return;
  }
  if (state.view === "patrol") renderWorkspacePatrol();
  else if (state.view === "focus") {
    const task = activeTask();
    if (!task) renderNoActiveTask();
    else renderFocus(task);
  }
  else if (state.view === "map") renderMap();
  else if (state.view === "history") renderHistoryReader();
  else if (state.view === "draft") renderDraft();
  else if (state.view === "compress") renderCompress();
  else if (state.view === "plugins") renderPlugins();
  else if (state.view === "memory") renderMemory();
  else if (state.view === "loop") renderLoop();
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
  interfaceI18n.apply(app);
  materialContentLoader.bindAll(app);
}

function renderNoActiveTask() {
  if (appInspector.parentElement !== document.querySelector(".app-shell")) document.querySelector(".app-shell").append(appInspector);
  appInspector.hidden = true;
  const english = interfaceI18n.locale() === "en-US";
  app.innerHTML = `<section class="empty-state"><h1>${english ? "No active Context" : "暂无活动 Context"}</h1><p>${english ? "There is no active Context. Create a task or restore an archived Context from Settings." : "当前没有可进入的活动 Context。可以新建任务，或从设置中恢复已归档的 Context。"}</p><div class="ui-toolbar"><button class="primary" data-action="new-task">${uiText("header.new_task", "新增任务")}</button><button class="text-button" data-action="open-settings">${english ? "View Archived Contexts" : "查看已归档 Context"}</button></div></section>`;
}

async function renderWorkspacePatrol() {
  if (workspacePatrolController?.isMounted(app, state.patrolWorkspace?.workspace_id)) return workspacePatrolMount;
  workspacePatrolController?.dispose();
  workspacePatrolController = null;
  if (state.patrolWorkspace) {
    workspacePatrolPicker?.dispose();
    workspacePatrolPicker = null;
    const workspace = state.patrolWorkspace;
    const store = window.FocusLoopLiveStore.create();
    const connection = window.FocusLoopLiveConnection.create({ api: loopApi, store });
    workspacePatrolController = window.FocusWorkspacePatrolController.create({
      api: loopApi, inputs: workspacePatrolIntake.store(workspace.workspace_id), intake: workspacePatrolIntake,
      ...(desktopPatrol ? { onCompose:target => desktopPatrol.openPanel(target) } : {}),
      liveStore: store, connection,
      createRunPreviews,
      onOpenTask: async contextId => {
        if (!state.tasks.some(item => item.task_id === contextId)) await refreshTasks();
        if (state.tasks.some(item => item.task_id === contextId)) await switchTask(contextId);
        else setStatus("此 Context 暂不可从任务入口打开，请刷新工作区", true);
      },
    });
    workspacePatrolMount = workspacePatrolController.mount(app, workspace);
    performance.mark("focus:patrol-mounted");
    await workspacePatrolMount;
    return;
  }
  if (workspacePatrolPicker) return;
  const picker = window.FocusWorkspacePatrolPicker.create({
    async onEnter(path) {
      const workspace = await workspacePatrolIntake.bind(path);
      if (state.view !== "patrol" || workspacePatrolPicker !== picker) return;
      state.patrolWorkspace = workspace;
      workspacePatrolIntake.remember(workspace);
      patrolWorkspaceBeforePick = null;
      render();
    },
    async chooseFolder() {
      return workspacePatrolIntake.choose();
    },
    onCancel() {
      if (state.view !== "patrol" || workspacePatrolPicker !== picker) return;
      state.patrolWorkspace = patrolWorkspaceBeforePick;
      patrolWorkspaceBeforePick = null;
      if (state.patrolWorkspace) render();
    },
  });
  workspacePatrolPicker = picker;
  picker.mount(app);
  performance.mark("focus:patrol-mounted");
}

function observeLoopConnection(loopId) {
  if (!loopConnection) return;
  void loopConnection.start(loopId).catch(error => {
    if (state.view !== "loop" || state.loop.loopId !== loopId) return;
    if (loopConnection === loopLiveConnection) {
      loopLiveStore.setConnection("unavailable", { error: String(error?.message || error) });
    } else {
      loopStore?.fail(error);
    }
  });
}

async function openLoopView() {
  if (state.view === "focus") persistFocusState();
  state.view = "loop";
  state.loop.revisionSnapshot = null;
  state.inspector.open = false;
  const task = activeTask();
  const previousLoopId = state.loop.loopId;
  state.loop.loopId = task ? localStorage.getItem(`focus-agent-loop:${task.task_id}`) : null;
  if (previousLoopId && previousLoopId !== state.loop.loopId) {
    loopConnection?.stop();
    loopStore?.load(null);
    loopConsoleStore?.reset();
  }
  if (!state.loop.loopId) {
    loopStore?.load(null);
    loopConsoleStore?.reset();
  }
  render();
  if (!task || !loopApi) return;
  state.loop.loading = true;
  try {
    if (!state.loop.loopId) {
      const found = await loopApi.findByContext(task.task_id);
      if (found?.loop_id) {
        state.loop.loopId = found.loop_id;
        localStorage.setItem(`focus-agent-loop:${task.task_id}`, found.loop_id);
        loopStore.load(found);
      }
    }
    if (!state.loop.loopId) {
      const detail = state.details.get(task.task_id) || {};
      detail.loop_activation = await loopApi.activationEligibility(task.task_id);
      state.details.set(task.task_id, detail);
      return;
    }
    const snapshot = await loopApi.get(state.loop.loopId);
    if (snapshot.interaction_mode === "workspace_patrol") {
      state.patrolWorkspace = { workspace_id: snapshot.workspace_id, display_name: task.workspace_name || "工作区" };
      state.view = "patrol";
      return render();
    }
    loopStore.reconcile(snapshot);
    loopStore.reconcileRelated(await loopApi.related(snapshot));
    await loopConsoleController?.load(state.loop.loopId);
    observeLoopConnection(state.loop.loopId);
  } catch (error) {
    loopStore.fail(error);
  } finally {
    state.loop.loading = false;
    if (state.view === "loop") renderLoop();
  }
}

function renderLoop() {
  if (!loopView || !loopStore) {
    app.innerHTML = '<section class="empty-state"><h1>Agent Loop</h1><p>Loop 视图模块不可用。</p></section>';
    return;
  }
  const priorTranscript = app.querySelector?.("[data-loop-transcript]");
  const priorMap = app.querySelector?.(".portfolio-map-scroll");
  const priorSearch = app.querySelector?.("[data-loop-message-search]");
  const priorComposer = app.querySelector?.("#loopInterventionForm textarea");
  const priorWaitForm = app.querySelector?.("[data-loop-wait-response]");
  const priorWaitId = priorWaitForm?.closest?.("[data-wait-request-id]")?.dataset.waitRequestId;
  if (priorWaitId && loopWaitDrafts) loopWaitDrafts.set(priorWaitId, serializeWaitDraft(priorWaitForm));
  const focusedLoopField = document.activeElement === priorSearch ? "search" : document.activeElement === priorComposer ? "composer" : null;
  const viewport = {
    transcriptContextId: priorTranscript?.dataset.contextId || null,
    transcriptTop: priorTranscript?.scrollTop || 0,
    transcriptHeight: priorTranscript?.scrollHeight || 0,
    transcriptRangeStart: Number(priorTranscript?.dataset.rangeStart || 0),
    transcriptNearBottom: priorTranscript ? priorTranscript.scrollHeight - priorTranscript.scrollTop - priorTranscript.clientHeight < 48 : true,
    mapTop: priorMap?.scrollTop || 0,
    mapLeft: priorMap?.scrollLeft || 0,
    search: priorSearch?.value || "",
    composer: priorComposer?.value || "",
    focusedLoopField,
    selectionStart: focusedLoopField ? document.activeElement.selectionStart : null,
    selectionEnd: focusedLoopField ? document.activeElement.selectionEnd : null,
  };
  const task = activeTask();
  const detail = task ? state.details.get(task.task_id) : null;
  const consoleState = currentLoopConsoleState();
  const loopState = loopStore.get();
  const waitRequest = loopState.snapshot?.wait_request;
  const waitRequestId = waitRequest?.request_id;
  const waitUi = waitRequestId ? { ...(loopWaitUi.get(waitRequestId) || {}), draft: loopWaitDrafts?.get(waitRequestId) || "" } : {};
  app.innerHTML = loopView.render(loopState, task ? {
    ...task,
    active_run: detail?.active_run || task.active_run,
    latest_direct_user_run: detail?.latest_direct_user_run,
    loop_activation: detail?.loop_activation,
  } : {}, consoleState, waitUi);
  const nextTranscript = app.querySelector?.("[data-loop-transcript]");
  const nextMap = app.querySelector?.(".portfolio-map-scroll");
  const nextSearch = app.querySelector?.("[data-loop-message-search]");
  const nextComposer = app.querySelector?.("#loopInterventionForm textarea");
  if (nextSearch && viewport.search) {
    nextSearch.value = viewport.search;
    const query = viewport.search.trim().toLowerCase();
    app.querySelectorAll("[data-loop-transcript] .loop-message").forEach(message => {
      message.hidden = !message.textContent.toLowerCase().includes(query);
    });
  }
  if (nextComposer && viewport.composer) nextComposer.value = viewport.composer;
  const nextFocused = viewport.focusedLoopField === "search" ? nextSearch : viewport.focusedLoopField === "composer" ? nextComposer : null;
  if (nextFocused) {
    nextFocused.focus({ preventScroll: true });
    nextFocused.setSelectionRange(viewport.selectionStart, viewport.selectionEnd);
  }
  if (nextTranscript) {
    const contextChanged = Boolean(viewport.transcriptContextId && viewport.transcriptContextId !== nextTranscript.dataset.contextId);
    const prepended = Number(nextTranscript.dataset.rangeStart || 0) < viewport.transcriptRangeStart;
    const restoredTop = Number(consoleState?.conversationViewport?.scrollTop);
    nextTranscript.scrollTop = contextChanged && consoleState?.conversationViewport && Number.isFinite(restoredTop)
      ? restoredTop
      : viewport.transcriptNearBottom
        ? nextTranscript.scrollHeight
        : viewport.transcriptTop + (prepended ? Math.max(0, nextTranscript.scrollHeight - viewport.transcriptHeight) : 0);
  }
  if (nextMap) {
    nextMap.scrollTop = viewport.mapTop;
    nextMap.scrollLeft = viewport.mapLeft;
  }
  loopConsoleController?.bind(app);
  if (state.loop.revisionSnapshot) {
    const revision = state.loop.revisionSnapshot;
    const messages = revision.messages || revision.authored_messages || revision.execution_messages || [];
    app.insertAdjacentHTML("beforeend", `<aside class="loop-revision-snapshot" aria-label="Context revision snapshot"><header><div><span class="loop-kicker">Historical snapshot</span><h3>${escapeHtml(revision.ref?.context_id || revision.context_id || "Context")} · R${escapeHtml(revision.ref?.generation || revision.generation || "—")}</h3></div><button type="button" data-action="close-loop-revision" aria-label="关闭历史快照">×</button></header><dl><div><dt>Revision</dt><dd>${escapeHtml(revision.ref?.revision_id || revision.revision_id || "—")}</dd></div><div><dt>Projection</dt><dd>${escapeHtml(revision.projection_status || "—")}</dd></div></dl><ol>${messages.map(message => `<li><strong>${escapeHtml(message.type || message.role || "message")}</strong><pre>${escapeHtml(typeof message.content === "string" ? message.content : JSON.stringify(message.content ?? message, null, 2))}</pre></li>`).join("") || "<li>该历史视图没有内联消息。</li>"}</ol></aside>`);
  }
  const error = loopStore.get().error;
  if (error) app.insertAdjacentHTML("beforeend", `<p class="loop-error" role="alert">${escapeHtml(error)}</p>`);
}

function patchLoopConsole() {
  if (state.view !== "loop") return;
  if (!loopConsoleView?.patch?.(app, currentLoopConsoleState())) {
    renderLoop();
    return;
  }
  loopConsoleController?.bind(app);
}

function currentLoopConsoleState() {
  const consoleState = loopConsoleStore?.get() || {};
  const loopStatus = loopStore?.get().snapshot?.status || null;
  return { ...consoleState, loopStatus, terminal: ["completed", "stopped", "failed"].includes(loopStatus) };
}

function scheduleLoopLifecyclePatch() {
  if (loopLifecycleFrame !== null) return;
  loopLifecycleFrame = requestAnimationFrame(() => {
    loopLifecycleFrame = null;
    if (state.view !== "loop") return;
    if (!loopView?.patchLifecycle?.(app, loopStore?.get())) renderLoop();
  });
}

setInterval(() => {
  if (state.view !== "loop") return;
  const runs = Object.values(loopStore?.get().live?.runs || {});
  if (runs.some(item => ["pending", "running"].includes(item.state?.status))) scheduleLoopLifecyclePatch();
}, 60000);

async function openLoopRevision(revisionId) {
  if (!loopApi || !revisionId) return;
  try {
    state.loop.revisionSnapshot = await loopApi.revision(revisionId);
  } catch (error) {
    loopStore.fail(error);
  }
  renderLoop();
}

async function exitAgentLoop(prepareNew = false) {
  const task = activeTask();
  const exitedLoopId = state.loop.loopId;
  loopConnection?.stop();
  if (loopLifecycleFrame !== null) cancelAnimationFrame(loopLifecycleFrame);
  loopLifecycleFrame = null;
  if (task) localStorage.removeItem(`focus-agent-loop:${task.task_id}`);
  state.loop.loopId = null;
  state.loop.revisionSnapshot = null;
  loopStore?.load(null);
  loopLiveStore?.reset();
  loopConsoleStore?.reset();
  if (task) {
    const cached = state.details.get(task.task_id);
    if (cached) {
      for (const key of ["active_run", "latest_direct_user_run"]) {
        if (cached[key]?.run_id && !cached[key].loop_id) cached[key] = { ...cached[key], loop_id: exitedLoopId };
      }
      state.details.set(task.task_id, cached);
    }
    try {
      state.details.set(task.task_id, await api(`/desktop/api/tasks/${task.task_id}`));
    } catch (error) {
      setStatus(`已退出 Loop；刷新 Context 状态失败：${error.message}`, true);
    }
  }
  if (prepareNew) {
    state.view = "focus";
    render();
    setStatus("请在当前 Context 发送一条新的直接用户消息，再进入 Agent Loop 创建后继 Loop。", false);
    return;
  }
  renderLoop();
}

async function startAgentLoop(form) {
  const task = activeTask();
  if (!task || !loopApi) return;
  const values = new FormData(form);
  const mission = loopMissionEditor.read(form);
  const taskDetail = state.details.get(task.task_id);
  const submit = form.querySelector('button[type="submit"]');
  const status = form.querySelector("[data-loop-start-status]");
  if (submit?.disabled) return;
  if (submit) submit.disabled = true;
  if (status) status.textContent = "正在复核最新直接用户 Run…";
  let eligibility;
  try {
    eligibility = await loopApi.activationEligibility(task.task_id);
  } catch (error) {
    if (submit) submit.disabled = false;
    if (status) status.textContent = `复核失败：${error.message}`;
    return;
  }
  if (taskDetail) taskDetail.loop_activation = eligibility;
  if (!eligibility.eligible || !eligibility.candidate_run_id) {
    if (submit) submit.disabled = false;
    if (status) status.textContent = `当前不能启动：${eligibility.reason || "没有可用 Run"}`;
    return;
  }
  const initialRunId = eligibility.candidate_run_id;
  const loopId = form.dataset.loopId || crypto.randomUUID().replaceAll("-", "");
  const activationKey = form.dataset.activationKey || `loop-activation:${initialRunId}`;
  form.dataset.loopId = loopId;
  form.dataset.activationKey = activationKey;
  const capabilities = ["continue_context", "create_lane", "update_lane", "merge_contexts", "pause_lane", "discard_membership", "request_lane_curator", "request_completion_verifier", "request_completion", "wait_for_user", "stop_loop"];
  const autonomousCompression = values.get("autonomousCompression") === "on";
  if (autonomousCompression) capabilities.push("apply_context_compression");
  if (values.get("isolatedWrites") === "on") {
    capabilities.push("isolate_workspace", "adopt_workspace_result");
  }
  const body = {
    loop_id: loopId,
    workspace_id: task.workspace_id,
    initial_context_id: task.task_id,
    initial_run_id: initialRunId,
    readiness_token: eligibility.consistency_token,
    activation_key: activationKey,
    holder_id: `patrol:${loopId}`,
    mission,
    capabilities,
    context_scope: [task.task_id],
    permission_scope: [],
    inherit_initial_equipment: true,
    delegable_gates: autonomousCompression ? ["compression"] : [],
    compression_policy: autonomousCompression ? {
      version: 1,
      allow_replace: true,
      allow_delete: false,
      protected_anchors: ["mission", "task_contract", "acceptance_criteria", "latest_direct_user_message", "unconsumed_material", "active_tool_protocol", "system_safety"],
      max_source_messages: 64,
      max_source_tokens: 60000,
      max_attempts_per_gate: 3,
      candidate_ttl_seconds: 900,
      min_reduction_tokens: 256,
    } : null,
    budgets: loopBudgetPayload(values, true),
    equipment: {},
  };
  state.loop.loading = true;
  if (status) status.textContent = `正在授权 Run ${initialRunId}…`;
  try {
    const snapshot = await loopApi.start(body);
    state.loop.loopId = snapshot.loop_id;
    localStorage.setItem(`focus-agent-loop:${task.task_id}`, snapshot.loop_id);
    if (taskDetail) {
      for (const key of ["active_run", "latest_direct_user_run"]) {
        if (taskDetail[key]?.run_id === initialRunId) taskDetail[key] = { ...taskDetail[key], loop_id: snapshot.loop_id };
      }
      state.details.set(task.task_id, taskDetail);
    }
    loopStore.load(snapshot);
    loopStore.reconcileRelated(await loopApi.related(snapshot));
    await loopConsoleController?.load(snapshot.loop_id);
    renderLoop();
    observeLoopConnection(snapshot.loop_id);
  } catch (error) {
    loopStore.fail(error);
    if (status) status.textContent = `授权失败：${error.message}`;
  } finally {
    state.loop.loading = false;
    if (submit?.isConnected) submit.disabled = false;
  }
}

async function respondToLoopWait(request, answer) {
  const loopId = state.loop.loopId;
  if (!loopId || !request?.request_id) return;
  const requestId = request.request_id;
  loopWaitUi.set(requestId, { pending: true, error: null });
  renderLoop();
  try {
    await loopApi.resolveWait(loopId, requestId, {
      request_revision: request.revision,
      idempotency_key: crypto.randomUUID(),
      answer,
    });
    loopWaitDrafts?.delete(requestId);
    loopWaitUi.delete(requestId);
    loopStore.reconcile(await loopApi.get(loopId));
    renderLoop();
  } catch (error) {
    loopWaitUi.set(requestId, { pending: false, error: error.message, draft: loopWaitDrafts?.get(requestId) || "" });
    renderLoop();
  }
}

async function resumeLoopWithCurrentMission(request) {
  const loopId = state.loop.loopId;
  try {
    const resumed = await window.FocusLoopWaitRecovery.confirmAndResume({
      request,
      loopId,
      confirm: prompt => window.confirm(prompt),
      submit: (id, requestId, body) => loopApi.resumeWithCurrentMission(id, requestId, body),
      beforeSubmit: () => {
        loopWaitUi.set(request.request_id, { pending: true, error: null });
        renderLoop();
      },
    });
    if (!resumed) return;
    loopWaitDrafts?.delete(request.request_id);
    loopWaitUi.delete(request.request_id);
    loopStore.reconcile(await loopApi.get(loopId));
    renderLoop();
  } catch (error) {
    loopWaitUi.set(request.request_id, { pending: false, error: error.message, draft: loopWaitDrafts?.get(request.request_id) || "" });
    renderLoop();
  }
}

function loopBudgetPayload(values, starting = false) {
  return window.FocusLoopExpansionBudget.readLoop(values, starting);
}

async function mutateAgentLoopGrant(body) {
  const loopId = state.loop.loopId;
  if (!loopId || !loopApi) return;
  loopStore.beginControl(body.command);
  renderLoop();
  try {
    const snapshot = await loopApi.mutateGrant(loopId, body);
    loopStore.reconcile(snapshot);
    loopStore.reconcileRelated(await loopApi.related(snapshot));
  } catch (error) {
    loopStore.fail(error);
  }
  renderLoop();
}

function narrowAgentLoopGrant(form) {
  return mutateAgentLoopGrant(loopView.readNarrowGrant(form, state.loop.snapshot?.grant));
}

function adjustAgentLoopBudgets(form) {
  return mutateAgentLoopGrant({ command: "adjust_budgets", budgets: loopBudgetPayload(new FormData(form)) });
}

async function controlLoop(command) {
  const loopId = state.loop.loopId;
  if (!loopId || !loopApi) return;
  loopStore.beginControl(command);
  renderLoop();
  try {
    loopStore.reconcile(await loopApi.control(loopId, command));
  } catch (error) {
    loopStore.fail(error);
  }
  renderLoop();
}

async function overrideAgentLoop(form) {
  const loopId = state.loop.loopId;
  if (!loopId || !loopApi) return;
  const mission = loopMissionEditor.read(form);
  loopStore.beginControl("override");
  renderLoop();
  try {
    const snapshot = await loopApi.reviseMission(loopId, mission);
    loopStore.reconcile(snapshot);
    loopStore.reconcileRelated(await loopApi.related(snapshot));
  } catch (error) {
    loopStore.fail(error);
  }
  renderLoop();
}

function activeNavigationKey() {
  if (state.view === "history") return "map";
  if (state.view === "patrol") return "patrol";
  if (state.view === "map") return "map";
  if (state.view === "plugins") return "plugins";
  if (state.view === "memory") return "memory";
  if (state.view === "loop") return "focus";
  if (activeTask()?.harness_mode === "assembly") return "assembly";
  return "focus";
}

function renderShellChrome() {
  const task = activeTask();
  const workspaceName = document.querySelector("#shellWorkspaceName");
  if (workspaceName) workspaceName.textContent = state.patrolWorkspace?.display_name || (task?.harness_mode !== "assembly" && task?.workspace_name) || "选择工作区";
  if (shellTaskTitle) shellTaskTitle.textContent = state.view === "patrol" ? `Patrol${state.patrolWorkspace?.display_name ? ` · ${state.patrolWorkspace.display_name}` : ""}` : task?.title || uiText("header.no_task", "尚未选择任务");
  if (shellTaskMeta) shellTaskMeta.textContent = state.view === "patrol" ? "工作区统一输入" : task
    ? `${task.workspace_name || uiText("common.local_workspace", "本地工作区")} · ${task.task_id.slice(0, 8)}`
    : uiText("header.workbench", "本地 Agent 工作台");
  if (state.view === "history") {
    if (shellTaskTitle) shellTaskTitle.textContent = state.historyReader.detail?.title || "历史记录";
    if (shellTaskMeta) shellTaskMeta.textContent = "只读查看";
  }
  const current = activeNavigationKey();
  document.querySelectorAll?.("[data-nav-key]").forEach(button => {
    const label = button.querySelector(".app-nav-label")?.textContent || button.dataset.navKey;
    button.setAttribute("aria-label", label); button.title = label;
    if (!button.querySelector(".ui-icon")) {
      const names = { patrol: "brain-circuit", focus: "file-text", map: "package", plugins: "wrench", assembly: "folder", memory: "file-text" };
      button.insertAdjacentHTML("afterbegin", `<span class="ui-icon icon-${names[button.dataset.navKey]}" aria-hidden="true"></span>`);
    }
    if (button.dataset.navKey === current) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
    button.disabled = false;
  });
  renderInspector();
}

const RUN_STATUS_PRESENTATION = Object.freeze({
  pending: { key: "status.pending", fallback: "排队中", tone: "active" },
  running: { key: "status.running", fallback: "运行中", tone: "active" },
  success: { key: "status.success", fallback: "已完成", tone: "success" },
  interrupted: { key: "status.interrupted", fallback: "已中断", tone: "warning" },
  error: { key: "status.error", fallback: "运行失败", tone: "danger" },
  cancelled: { key: "status.cancelled", fallback: "已取消", tone: "warning" },
  ready: { key: "status.ready", fallback: "就绪", tone: "neutral" },
});

function presentRunStatus(status) {
  const item = RUN_STATUS_PRESENTATION[status] || RUN_STATUS_PRESENTATION.ready;
  return { label: status && !RUN_STATUS_PRESENTATION[status] ? status : uiText(item.key, item.fallback), tone: item.tone };
}

function presentTaskStatus(task) {
  const attention = task.attention || [];
  if (attention.length) {
    return { label: attention.some(item => item.kind === "access_approval") ? "等待授权" : "需要你处理", tone: "warning" };
  }
  if (task.attention_unavailable) return { label: "待处理状态待刷新", tone: "warning" };
  if (["pending", "running"].includes(task.active_run?.status)) return presentRunStatus(task.active_run.status);
  const loopStatus = task.loop?.status;
  if (["running", "pausing", "completing"].includes(loopStatus)) return presentRunStatus("running");
  if (loopStatus === "waiting_user") return { label: "需要你处理", tone: "warning" };
  if (loopStatus === "paused") return { label: "已暂停", tone: "warning" };
  if (loopStatus === "failed") return presentRunStatus("error");
  if (loopStatus === "completed") return presentRunStatus("success");
  if (loopStatus === "stopped") return { label: "已停止", tone: "warning" };
  if (task.latest_run) return presentRunStatus(task.latest_run.status);
  return { label: "未开始", tone: "neutral" };
}

function materialGroupingProjection(task) {
  const detail = state.details.get(task.task_id) || {};
  const mode = detail.ui_state?.material_grouping_mode === "type" ? "type" : "run";
  const projection = materialGrouping.project(
    state.materials.get(task.task_id) || [],
    state.materialGroups.get(task.task_id) || [],
    mode,
    detail.ui_state?.collapsed_material_groups || [],
    materialSelection(task.task_id)
  );
  const loads = taskResourceLoads.get(task.task_id);
  const groupingReady = state.materials.has(task.task_id) && state.materialGroups.has(task.task_id)
    && loads?.materials?.status !== "loading" && loads?.groups?.status !== "loading";
  detail.ui_state = { ...(detail.ui_state || {}), material_grouping_mode: mode,
    collapsed_material_groups: groupingReady ? projection.collapsedKeys : (detail.ui_state?.collapsed_material_groups || []) };
  return { ...projection, mode };
}

function renderMaterialGroups(task) {
  const projection = materialGroupingProjection(task);
  const groups = projection.groups.map(group => `<section class="material-group" data-material-group-key="${escapeHtml(group.key)}">
    <header class="material-group-header"><button class="text-button material-group-toggle" data-action="toggle-material-group" data-group-key="${escapeHtml(group.key)}" aria-expanded="${!group.collapsed}">${group.collapsed ? "▸" : "▾"} ${escapeHtml(group.label)}</button><span class="ui-badge">${group.materials.length}${group.selectedCount ? ` · 已选 ${group.selectedCount}` : ""}</span>${group.source === "custom" ? `<span class="material-group-actions"><button class="text-button" data-action="move-material-group-order" data-group-id="${escapeHtml(group.key.slice(7))}" data-direction="-1" title="上移">↑</button><button class="text-button" data-action="move-material-group-order" data-group-id="${escapeHtml(group.key.slice(7))}" data-direction="1" title="下移">↓</button><button class="text-button" data-action="rename-material-group" data-group-id="${escapeHtml(group.key.slice(7))}">改名</button><button class="text-button danger" data-action="delete-material-group" data-group-id="${escapeHtml(group.key.slice(7))}">删除</button></span>` : ""}</header>
    <div class="material-group-body"${group.collapsed ? " hidden" : ""} data-drop-group-id="${group.source === "custom" ? escapeHtml(group.key.slice(7)) : ""}">${group.materials.map(renderMaterial).join("") || '<p class="muted tiny">空分组</p>'}</div>
  </section>`).join("");
  return `<div class="material-group-toolbar"><span class="segmented" role="group" aria-label="材料自动分组"><button data-action="set-material-grouping" data-mode="run" class="${projection.mode === "run" ? "active" : ""}">按 Run</button><button data-action="set-material-grouping" data-mode="type" class="${projection.mode === "type" ? "active" : ""}">按类型</button></span><button class="text-button" data-action="create-material-group">新建分组</button></div>${groups}`;
}

function renderInspector() {
  if (!appInspector?.setAttribute || !inspectorContent) return;
  const open = state.inspector.open && state.view !== "patrol" && !(state.view === "focus" && state.filesPanel);
  if (!appInspector.hasAttribute("data-surface-closing")) appInspector.hidden = !open;
  appInspector.inert = !open;
  appInspector.setAttribute("aria-hidden", String(!open));
  syncShellResizerVisibility();
  if (!open) return;
  const tab = state.inspector.tab;
  const activeBranch = document.querySelector('#patrolBranchControls');
  if (tab === 'agents' && activeBranch?.dataset?.agentId === state.agentDetails.agentId && ['TEXTAREA','INPUT','SELECT'].includes(document.activeElement?.tagName) && activeBranch.parentElement?.contains(document.activeElement)) return;
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
    inspectorContent.innerHTML = `<section class="inspector-section materials-inspector"><header><div><span class="workspace-kicker">TASK SOURCES</span><h3>任务材料</h3></div><span class="ui-badge">${materials.length}</span></header><p class="inspector-description">每轮选择与长期策略互不覆盖；手动分组始终优先于自动分组。</p>${protectedCount ? `<div class="ui-notice is-success"><strong>${protectedCount} 份材料受版本保护</strong><span>不可遗失材料可置空或从历史版本恢复，但不会直接删除。</span></div>` : ""}<div class="inspector-list">${materials.length || (state.materialGroups.get(task.task_id) || []).length ? renderMaterialGroups(task) : '<section class="ui-empty-state"><h1>暂无材料</h1><p>从 Composer 添加文件后，可在这里配置阅读和保留策略。</p></section>'}</div></section>`;
    inspectorContent.insertAdjacentHTML("afterbegin", renderTaskResourceStatus(task.task_id, ["materials", "groups", "history"]));
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
        ${renderTaskResourceStatus(task.task_id, ["agents"])}
        <header><button class="text-button" type="button" data-action="agent-list"><span class="ui-icon is-sm icon-chevron-left" aria-hidden="true"></span>Agents</button><span class="ui-badge is-${presented.tone}">${escapeHtml(curator ? curatorStateLabel(agent) : presented.label)}</span></header>
        <div><h3>${agent ? `小兵 ${escapeHtml(agent.agent_id.slice(0, 8))}` : "Agent"}</h3><p class="ui-meta">${escapeHtml(agent?.checkpoint_ns || "")}</p></div>
        ${curator
          ? (state.agentDetails.busy ? '<p class="muted">加载中…</p>' : renderCuratorDetails(agent, state.agentDetails.curation))
          : `<div class="agent-detail-history">${state.agentDetails.busy ? '<p class="muted">加载中…</p>' : state.agentDetails.messages.length ? state.agentDetails.messages.map(renderMessage).join("") : '<p class="muted">暂无已提交消息</p>'}</div>
            <div class="ui-toolbar agent-detail-actions"><button class="text-button" data-action="refresh-agent-details">刷新</button><button class="text-button" data-action="retry-agent-details">重试</button><button class="text-button danger" data-action="cancel-agent-details">取消运行</button></div>
            <div id="patrolBranchControls"></div><form class="agent-inspector-continue" id="agentInspectorContinueForm"><label for="agentInspectorInput">继续对话</label><textarea id="agentInspectorInput" rows="3" placeholder="给这个 Agent 追加指令…"></textarea><button class="primary" type="submit">继续</button></form>`}
      </section>`;
      if (window.FocusPatrolBranches) window.FocusPatrolBranches.mount(document.querySelector('#patrolBranchControls'), {
        agent:agents.find(item => item.agent_id === state.agentDetails.agentId), api, onRun:listenToRun,
        onDraft:draft=>{state.drafts.set(draft.task_id,draft);state.activeTaskId=draft.task_id;state.view='draft';render();},
        onError:error=>setStatus(error.message,true),
      });
      return;
    }
    inspectorContent.innerHTML = `${renderTaskResourceStatus(task.task_id, ["agents"])}<section class="inspector-section"><header><h3>协作 Agents</h3><span class="ui-badge">${agents.length}</span></header><div class="inspector-list">${agents.length ? renderAgentStrip(task.task_id) : '<p class="muted">尚未投放小兵</p>'}</div></section>`;

    return;
  }
  const run = task.active_run || task.latest_run;
  const presented = presentTaskStatus(task);
  inspectorContent.innerHTML = `<section class="inspector-section"><header><h3>主 Agent</h3><span class="ui-badge is-${presented.tone}">${escapeHtml(presented.label)}</span></header><dl class="inspector-run-meta"><div><dt>任务</dt><dd>${escapeHtml(task.title)}</dd></div><div><dt>运行 ID</dt><dd>${escapeHtml(run?.run_id || "—")}</dd></div></dl>${state.commitment.stage ? `<section class="inspector-run-stage"><span class="workspace-kicker">COMMITMENT</span><strong>${state.commitment.stage}/9 · ${escapeHtml(COMMITMENT_STAGE_NAMES[state.commitment.stage] || "准备中")}</strong><span>${state.commitment.review ? "等待人工确认" : state.commitment.terminalStatus ? presentRunStatus(state.commitment.terminalStatus).label : "正在执行"}</span></section>` : ""}${renderInterruptButton(state.details.get(task.task_id))}</section>`;
}

function openInspector(tab, trigger) {
  const wasOpen = state.inspector.open;
  state.inspector.open = true;
  state.inspector.tab = tab;
  if (trigger && !trigger.matches?.('[role="tab"]')) state.inspector.returnFocus = trigger;
  else if (!wasOpen || !state.inspector.returnFocus) state.inspector.returnFocus = trigger || document.activeElement;
  const exiting = appInspector?.inert;
  if (!wasOpen || exiting) void window.FocusSurfaceTransition.visible(appInspector, true, { axis: "x", distance: 24 });
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
  appInspector.dataset.surfaceClosing = "";
  void window.FocusSurfaceTransition.visible(appInspector, false, { axis: "x", distance: 24 }).then(done => { if (done && !state.inspector.open) syncShellResizerVisibility(); });
  renderShellChrome();
  if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true }); else app.focus();
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
    ${kind === "main" ? `<div id="mainSkillLoadStatus">${renderTaskResourceStatus(state.activeTaskId, ["skills"])}</div>` : ""}
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
    if (clearQuery) {
      const input = document.querySelector("#mainInput");
      if (input) input.value = "";
      composerDraft.claim(state.activeTaskId, "");
      if (input) updateSkillMenu(input);
    }
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
  return `<button class="text-button danger" data-action="interrupt-main-run" data-run-id="${run.run_id}"${state.mainInterrupting ? " disabled" : ""}>${uiText("focus.interrupt", "中断")}</button>`;
}

function renderRunMaterialNote(materialId) {
  // 备注属于本轮材料绑定本身，只在此处渲染一次并由所属材料条目就近承载；未选材料返回空串，
  // 取消勾选只隐藏输入框，备注草稿仍由 run-material-picker 按材料 ID 保留。
  const binding = materialSelection().bindings.find(item => item.materialId === materialId);
  if (!binding) return "";
  return `<label class="run-material-note"><textarea data-field="run-material-note" rows="2" draggable="false" placeholder="给这份材料添加本轮备注，可留空">${escapeHtml(binding.note)}</textarea></label>`;
}

function renderMustViewRecovery(detail) {
  const report = detail?.pending_must_view_report;
  if (!report) return "";
  const rows = (Array.isArray(report.materials) ? report.materials : []).map(item => {
    const reason = item.reason === "unread" ? "模型声明未读到" : "模型缺少逐图表态";
    return `<li><strong>${escapeHtml(item.relative_path || item.material_id)}</strong><span>${reason}</span></li>`;
  }).join("");
  return `<section class="review-panel must-view-report" data-unit-key="must-view-recovery" aria-label="必看图片报告"><header><span class="review-kicker">必看图片</span><h3>需要人工处理</h3><span class="review-badge">已暂停</span></header><ul class="review-draft">${rows || "<li>必看图片报告不完整</li>"}</ul><div class="review-actions"><button class="primary" data-action="retry-must-view">重试</button><button class="text-button danger" data-action="cancel-must-view">取消本轮</button></div></section>`;
}

function composerFeedback(detail, projectionBlocked) {
  const recovery = taskRunOperations?.recovery(state.activeTaskId);
  if (recovery?.failure?.uncertain === false && !recovery.editing) return { kind: "danger", text: `发送未受理：${recovery.failure.message}` };
  if (recovery) return { kind: "warning", text: recovery.editing
    ? "原请求尚未确认是否受理；编辑后重新发送会创建新请求，原请求仍可能执行。"
    : "尚未确认是否受理。原消息、附件和请求身份已保留，可按原内容重试。" };
  const error = state.composerErrors.get(state.activeTaskId);
  if (error) return { kind: "danger", text: error.startsWith("消息已受理") ? error : `发送失败：${error}` };
  if (state.mainPreparations.has(state.activeTaskId)) return { kind: "warning", text: "正在准备本次附件，完成后将自动发送…" };
  if (projectionBlocked) return { kind: "warning", text: "该 Context 需要完成执行投影决断后才能继续。" };
  if (activeTaskHasCommitmentLock()) return { kind: "warning", text: "当前任务正在等待 Commitment 审批，请先处理上方审批区。" };
  if (detail?.pending_compression) return { kind: "warning", text: "存在待确认的压缩计划，请先确认或取消。" };
  if (detail?.pending_must_view_report) return { kind: "warning", text: "必看图片报告等待重试或取消。" };
  if (detail?.pending_access_review) return { kind: "warning", text: "存在待批准的本机资源访问请求，请先批准或拒绝。" };
  return null;
}

function renderMainRecovery(taskId) {
  const recovery = taskRunOperations?.recovery(taskId);
  if (!recovery) return "";
  const pending = taskRunOperations.get(taskId).submission?.status === "pending";
  return `<button type="button" class="text-button" data-action="retry-main" ${pending ? "disabled" : ""}>按原内容重试</button><button type="button" class="text-button" data-action="edit-main" ${pending ? "disabled" : ""}>编辑后重新发送</button>`;
}

function updateMainRecoveryControls() {
  const taskId = state.activeTaskId;
  const recovery = taskRunOperations?.recovery(taskId);
  const node = document.querySelector("#mainSubmissionRecovery");
  if (node) node.innerHTML = renderMainRecovery(taskId);
  const input = document.querySelector("#mainInput");
  const pending = taskRunOperations?.get(taskId)?.submission?.status === "pending";
  if (input) input.readOnly = Boolean(recovery && !recovery.editing && !pending
    && !composingMainInputs.has(input)
    && composerDraft.value(taskId, input.value).trim() === recovery.message);
  const send = document.querySelector('[data-action="send-main"]');
  if (send) send.textContent = state.mainPreparations.has(taskId) ? "等待附件…" : recovery ? (recovery.editing ? (recovery.queued_edit ? "排队下一轮" : "发送新请求") : "按原内容重试") : mainDeliveryRun(taskId) ? "补充当前轮" : uiText("focus.send", "发送");
}

function mainDeliveryRun(taskId = state.activeTaskId) {
  const run = state.details.get(taskId)?.active_run;
  return run && ["pending", "running"].includes(run.status) && !run.loop_id ? run : null;
}

function renderConversationInputs(taskId) {
  return window.FocusConversationInputs.render(state.details.get(taskId)?.conversation_inputs || [], escapeHtml);
}

let conversationInputPolling = false;
setInterval(async () => {
  const taskId = state.activeTaskId;
  const detail = state.details.get(taskId);
  if (conversationInputPolling || state.view !== "focus" || !detail) return;
  if (!mainDeliveryRun(taskId) && !(detail.conversation_inputs || []).some(row => ["accepted", "queued"].includes(row.status))) return;
  conversationInputPolling = true;
  try {
    const rows = await api(`/desktop/api/tasks/${taskId}/main/inputs`);
    const started = rows.some(row => row.status === "started" && !(detail.conversation_inputs || [])
      .some(old => old.input_id === row.input_id && old.status === "started"));
    const current = state.details.get(taskId);
    if (!current) return;
    const changed = JSON.stringify(current.conversation_inputs) !== JSON.stringify(rows);
    current.conversation_inputs = rows;
    if (state.activeTaskId === taskId) {
      const panel = document.querySelector("#conversationInputs");
      if (panel && changed) panel.innerHTML = renderConversationInputs(taskId);
    }
    if (started) { await hydrateActive(taskId); if (state.activeTaskId === taskId && state.view === "focus") renderFocus(); }
  } catch (error) {
    if (state.activeTaskId === taskId) setStatus(`消息交付状态暂时无法刷新：${error.message}`, true);
  } finally { conversationInputPolling = false; }
}, 1000);

async function withdrawQueuedMain(inputId, edit) {
  const taskId = state.activeTaskId;
  if (edit && (composerDraft.value(taskId, state.details.get(taskId)?.ui_state?.input || "").trim()
      || mainReference(taskId)
      || taskRunOperations?.recovery(taskId) || materialSelection(taskId).bindings.length
      || state.composerAttachments.get(taskId)?.size)) {
    return setStatus("请先发送或清空当前输入和附件，再撤回排队消息编辑", true);
  }
  try {
    const row = await api(`/desktop/api/tasks/${taskId}/main/inputs/${inputId}`, { method: "DELETE" });
    const detail = state.details.get(taskId);
    detail.conversation_inputs = (detail.conversation_inputs || []).filter(item => item.input_id !== inputId);
    if (edit) {
      const input = typeof row.run.message === "string" ? row.run.message : JSON.stringify(row.run.message);
      const selection = { bindings: (row.run.material_inputs || (row.run.attached_material_ids || []).map(material_id => ({ material_id, note: "" })))
        .map(item => ({ materialId: item.material_id, note: item.note })), requiredImageIds: row.run.must_view_material_ids || [] };
      state.materialSelections.set(taskId, selection);
      taskRunOperations.freeze(taskId, crypto.randomUUID(), { input, message: input, queued_edit: true,
        body: JSON.stringify(row.run), selectionSignature: JSON.stringify(selection) });
      taskRunOperations.editRecovery(taskId);
      referenceDrafts.set(taskId, row.run.message_reference || null);
      detail.ui_state = { ...(detail.ui_state || {}), input, skills: row.run.skills || [],
        message_reference: row.run.message_reference || null };
      if (row.run.access_mode) detail.ui_state = accessMode.writeMode(detail.ui_state, row.run.access_mode);
      composerDraft.claim(taskId, input);
      if (state.activeTaskId === taskId && state.view === "focus") {
        const editor = document.querySelector("#mainInput");
        if (editor) { editor.value = input; updateAtHighlight(editor); }
      }
      await persistUiState(taskId);
    }
    if (state.activeTaskId === taskId && state.view === "focus") { renderFocus(); document.querySelector("#mainInput")?.focus(); }
  } catch (error) { setStatus(error.message, true); }
}

function editMainRequest() {
  const taskId = state.activeTaskId;
  if (taskRunOperations?.get(taskId).submission?.status === "pending") return;
  taskRunOperations?.editRecovery(taskId);
  setComposerError();
  document.querySelector("#mainInput")?.focus();
}

function setComposerError(error = null, taskId = state.activeTaskId) {
  if (error) state.composerErrors.set(taskId, String(error));
  else state.composerErrors.delete(taskId);
  if (taskId !== state.activeTaskId) return;
  updateMainRecoveryControls();
  const node = document.querySelector("#composerFeedback");
  if (!node) return;
  const detail = state.details.get(taskId);
  const projectionStatus = detail?.context?.projection_status || "root";
  const projectionBlocked = !["root", "valid", "repaired", "approved"].includes(projectionStatus);
  const feedback = composerFeedback(detail, projectionBlocked);
  node.hidden = !feedback;
  node.className = feedback ? `composer-feedback is-${feedback.kind}` : "composer-feedback";
  node.textContent = feedback?.text || "";
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
          <span class="context-rail-meta">${depth ? uiText("focus.context_derived", "派生 Context") : uiText("focus.context_root", "根 Context")} · ${escapeHtml(item.task_id.slice(0, 8))} · ${tag}</span>
        </span>
      </div>`;
    }
    return `<div class="context-rail-item${node?.editable ? " is-editable" : ""}" style="--context-depth:${depth}" data-context-depth="${depth}">
      <button type="button" class="context-rail-card${item.task_id === task.task_id ? " is-current" : ""}${blocked ? " is-blocked" : ""}" data-action="context-rail-card" data-task-id="${escapeHtml(item.task_id)}" aria-current="${item.task_id === task.task_id ? "true" : "false"}">
        <span class="context-rail-title">${escapeHtml(item.title)}</span>
        <span class="context-rail-meta">${depth ? uiText("focus.context_derived", "派生 Context") : uiText("focus.context_root", "根 Context")} · ${escapeHtml(item.task_id.slice(0, 8))}${node?.managed_status ? ` · 受管 ${escapeHtml(curatorTrackingLabel(node.managed_status))}${node.managed_health && node.managed_health !== "idle" ? `/${escapeHtml(node.managed_health)}` : ""}` : ""}${blocked ? ` · ${escapeHtml(projectionStatus)}` : ""} · ${uiText("focus.cache", "缓存")} ${cacheRate}</span>
        ${otherParents ? `<span class="context-rail-parents">另含：${escapeHtml(otherParents)}</span>` : ""}
      </button>
      ${node?.editable ? `<button type="button" class="context-rail-edit" data-action="edit-context-definition" data-context-id="${escapeHtml(item.task_id)}">编辑</button>` : ""}
    </div>`;
  }).filter(Boolean);
  const cards = cardList.join("");
  return `<aside class="context-rail" aria-label="Context 树">
    <header class="context-rail-heading"><strong>${uiText("nav.contexts", "上下文")}</strong><span>${cardList.length}</span></header>
    <nav class="context-rail-list" aria-label="当前聊天派生的 Context">
      ${cards}
      <button type="button" class="context-rail-add" data-action="derive-context">${uiText("focus.context_add", "新增 Context")}</button>
    </nav>
  </aside>`;
}

function renderTaskRail(task) {
  const tasks = state.tasks.filter(item => item.harness_mode !== "assembly" && sessionLifecycle(item) === "active");
  return `<aside class="task-workspace-list"><header><strong>工作区任务</strong><button data-action="new-task" aria-label="新建任务">＋</button></header><nav>${tasks.map(item => `<button data-action="context-rail-card" data-task-id="${escapeHtml(item.task_id)}" aria-current="${item.task_id === task.task_id ? "true" : "false"}"><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.workspace_name || "本地工作区")} · ${escapeHtml(presentTaskStatus(item).label)}</small></button>`).join("")}</nav><footer>这里的输入只发送到当前选中的任务。</footer></aside>`;
}

function capabilityRefreshSummary(result) {
  return `已刷新 ${result.skills.length} 个技能、${result.mcp_tool_count} 个 MCP 工具、${result.custom_tool_count ?? 0} 个自定义工具`;
}

function mcpFailureDescription(item) {
  const scopes = { default: "程序默认", global: "全局", workspace: "工作区", provided: "传入配置" };
  return `${item.server_name}（${scopes[item.scope] || item.scope}${item.config_path ? ` · ${item.config_path}` : ""}）：${item.reason}；处理：${item.action}`;
}

function renderCapabilityRefreshResult(taskId) {
  const result = state.capabilityRefreshResults.get(taskId);
  if (!result) return "";
  const failures = result.mcp_failures || [];
  const hasFailures = failures.length > 0 || result.failed_servers.length > 0;
  const details = failures.length ? failures.map(mcpFailureDescription)
    : result.failed_servers.map(name => `${name}：请检查配置或连接后重试 MCP。`);
  return `<section class="capability-refresh-result${hasFailures ? " danger" : ""}" role="status" aria-label="能力刷新结果"><strong>${hasFailures ? "MCP 刷新存在失败；" : ""}${escapeHtml(capabilityRefreshSummary(result))}</strong>${details.length ? `<ul>${details.map(text => `<li>${escapeHtml(text)}</li>`).join("")}</ul>` : ""}</section>`;
}

function renderAssemblyTarget(task) {
  const target = task.assembly_target || { scope: "global", path: task.workspace_path };
  const workspaces = new Map(state.tasks.filter(item => item.harness_mode !== "assembly" || item.assembly_target?.scope === "workspace")
    .map(item => [item.workspace_id, { id: item.workspace_id, name: item.workspace_name, path: item.workspace_path }]));
  if (state.patrolWorkspace) workspaces.set(state.patrolWorkspace.workspace_id, {
    id: state.patrolWorkspace.workspace_id, name: state.patrolWorkspace.display_name, path: state.patrolWorkspace.path,
  });
  if (target.scope === "workspace") workspaces.set(target.workspace_id, { id: target.workspace_id, name: task.workspace_name, path: target.path });
  return `<div class="assembly-target"><label>装配范围 <select data-assembly-target aria-label="装配范围"><option value=""${target.scope === "global" ? " selected" : ""}>全局 · 所有工作区</option>${[...workspaces.values()].map(item => `<option value="${escapeHtml(item.id)}"${target.workspace_id === item.id ? " selected" : ""}>仅 ${escapeHtml(item.name || item.path)}</option>`).join("")}</select></label><button type="button" data-action="pick-assembly-workspace">选择其他工作区</button><button type="button" data-action="refresh-capabilities" title="刷新所选范围的技能和 MCP，并重载全局自定义工具；下次运行生效，正在运行的 Run 保持原快照">刷新技能 / 自定义工具 / 重试 MCP</button><small title="${escapeHtml(target.path || "")}">${escapeHtml(target.path || "~/.focus")}</small>${renderCapabilityRefreshResult(task.task_id)}</div>`;
}

async function showAssembly(workspaceId = "") {
  const task = await api(`/desktop/api/assembly/task${workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : ""}`);
  if (!task?.task_id) return;
  replaceTasks([...state.tasks.filter(item => item.task_id !== task.task_id), task]);
  return switchTask(task.task_id);
}

// Keep the editor, its entire ancestor chain, and the conversation mounted.
// Even moving the old textarea out and back would end an active IME session.
function patchFocusChrome(markup) {
  const template = document.createElement("template");
  template.innerHTML = markup;
  const next = template.content;
  for (const selector of [".task-page-heading", ".task-workspace-list", ".task-workspace-tools",
    ".composer-context", "#composerReference", "#composerAttachments", "#composerFileReferences", ".composer-actions", "#composerFeedback",
    "#mainSubmissionRecovery", "#conversationInputs", "#taskDetailLoadStatus", "#mainSkillLoadStatus",
    '[data-skill-picker="main"] .skill-tags']) {
    const current = app.querySelector(selector);
    const replacement = next.querySelector(selector);
    if (current && replacement && current.outerHTML !== replacement.outerHTML) current.replaceWith(replacement);
  }
  const view = app.querySelector(".focus-view");
  for (const selector of [".task-admission-receipt", ".context-block-banner"]) {
    const current = app.querySelector(selector);
    const replacement = next.querySelector(selector);
    if (!replacement) current?.remove();
    else if (!current) view.insertBefore(replacement, app.querySelector("#commitmentProgress"));
    else if (current.outerHTML !== replacement.outerHTML) current.replaceWith(replacement);
  }
  const shell = app.querySelector(".focus-shell");
  const style = next.querySelector(".focus-shell").getAttribute("style");
  if (shell.getAttribute("style") !== style) shell.setAttribute("style", style);
  const panel = app.querySelector("#filePanel");
  const nextPanel = next.querySelector("#filePanel");
  if (!nextPanel) panel?.remove();
  else if (!panel) shell.append(nextPanel);
  // Labels may change with the locale; value, selection and skill-menu state
  // belong to the live editing session and must never be reapplied here.
  const input = app.querySelector("#mainInput");
  const nextInput = next.querySelector("#mainInput");
  for (const name of ["aria-label", "placeholder"]) {
    if (input.getAttribute(name) !== nextInput.getAttribute(name)) input.setAttribute(name, nextInput.getAttribute(name));
  }
}

function renderFocus(task = activeTask()) {
  if (!task) { mainFilePicker?.destroy(); mainFilePicker = null; return renderNoActiveTask(); }
  referenceSelection.hide();
  const isAssembly = task.harness_mode === "assembly";
  const admission = taskRunOperations?.get(task.task_id)?.admission;
  const mainRecovery = taskRunOperations?.recovery(task.task_id);
  if (appInspector.parentElement !== document.querySelector(".app-shell")) document.querySelector(".app-shell").append(appInspector);
  const previousInput = document.querySelector("#mainInput");
  const inputFocused = previousInput && document.activeElement === previousInput;
  const sameTask = app.dataset.taskId === task.task_id;
  const inputSelection = inputFocused && sameTask
    ? { start: previousInput.selectionStart, end: previousInput.selectionEnd, direction: previousInput.selectionDirection }
    : null;
  const detail = state.details.get(task.task_id) || { messages: [] };
  const composerInput = composerDraft.value(task.task_id, mainRecovery && !mainRecovery.editing ? mainRecovery.input : detail.ui_state?.input || mainRecovery?.input || "");
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
  const keepEditor = app.dataset.taskId === task.task_id
    && app.querySelector?.(".focus-view")?.dataset?.taskId === task.task_id
    && !!app.querySelector?.("#mainInput");
  const markup = `
    <header class="task-page-heading"><div><small>${task.harness_mode === "assembly" ? "独立会话" : escapeHtml(task.workspace_name || "本地工作区")}</small><h1>${task.harness_mode === "assembly" ? "无工作区模式" : "任务"}</h1><p>${task.harness_mode === "assembly" ? "从一个问题或一份材料开始。" : "一条任务，一条持续会话。"}</p></div><div><button data-action="show-patrol">回到 Patrol</button><button data-action="new-task">＋ 新建任务</button></div></header>
    <section class="task-workspace-layout${task.harness_mode === "assembly" ? " is-assembly" : ""}">${task.harness_mode === "assembly" ? "" : renderTaskRail(task)}<section class="focus-shell" style="${shellStyle}">
      <section class="focus-view" data-task-id="${task.task_id}">
        <header class="task-workspace-tools"><strong>${escapeHtml(task.title)}</strong><details><summary>任务工具</summary><button type="button" data-action="export-conversation">${escapeHtml(uiText("conversation.export", "导出对话为 Markdown"))}</button><button data-action="show-contexts">上下文与派生</button><button data-action="organize-context">整理上下文</button><button data-action="show-agents">协作与 Session Patrol</button><button data-action="toggle-session-patrol">会话 Patrol 小兵</button><button data-action="show-loop">任务 Loop 控制台</button><button data-action="open-inspector-tab" data-inspector-tab="materials">材料</button><button data-action="open-inspector-tab" data-inspector-tab="run">运行与恢复</button></details>${isAssembly ? renderAssemblyTarget(task) : ""}</header>
        ${admission ? `<section class="task-admission-receipt" role="status"><span>消息受理回执 · ${escapeHtml(admission.intent_id)}。交付与执行以实际状态为准。</span><button data-action="show-loop">查看任务 Loop 的交付状态</button></section>` : ""}
        ${contextBlock}
        <div class="commitment-progress" id="commitmentProgress" hidden>
          <div class="progress-heading"><strong>任务合同</strong><span id="progressLabel"></span></div>
          <ol id="progressSteps"></ol>
        </div>
        <div class="conversation" id="conversation"></div>
        <div class="patrol-avatar-layer" id="patrolAvatarLayer" aria-label="会话 Patrol 小兵"${detail.ui_state?.show_session_patrol === false ? " hidden" : ""}></div>
        <div class="focus-bottom">
          <div id="roundChanges"></div>
          <div id="taskDetailLoadStatus">${renderTaskResourceStatus(task.task_id, ["detail"])}</div>
          <div class="composer-shell glass-surface">
            <div class="composer-context"><span class="ui-badge is-active">${isAssembly ? uiText("focus.free_session", "自由会话") : uiText("focus.current_task", "当前任务")}</span><span>${escapeHtml(task.title)}</span><button class="text-button" type="button" data-action="open-inspector-tab" data-inspector-tab="run">${uiText("focus.run_details", "运行详情")}</button></div>
            <div class="composer" data-main-composer>
              <div id="composerReference">${conversationReference.render(mainReference(task.task_id))}</div>
              <div id="composerAttachments" class="composer-attachments" aria-label="本次消息附件">${renderComposerAttachments(task.task_id)}</div>
              <div id="composerFileReferences" class="composer-file-references" aria-label="本次问题引用的文件">${composerFiles.cards(fileReferences(task.task_id))}</div>
              ${renderSkillPicker("main", `<textarea id="mainInput" aria-label="${uiText("focus.input_label", "任务输入")}" placeholder="${uiText("focus.input_placeholder", "描述下一步，输入 @ 引用文件，或 / 选择技能…")}">${escapeHtml(composerInput)}</textarea>`, true)}
              <div id="composerFileMenu" class="composer-file-menu" role="listbox" aria-label="工作区文件" hidden></div>
              <div class="composer-actions"><div class="composer-actions-left">${renderAccessModePicker("main")}<span class="access-mode-impact" title="${escapeHtml(uiText("access.risk_standing_acl", "首次准备工作区会留下权限调整；退出 Focus 后不会自动恢复。"))}"><span class="access-mode-impact-icon" aria-hidden="true">i</span>${escapeHtml(isAssembly && task.assembly_target?.scope !== "workspace" ? uiText("access.internal_scope", "模式作用于 Focus 全局目录") : uiText("access.persistent_impact", "首次受限运行会持久调整工作区权限"))}</span></div><div class="composer-actions-right"><label class="attach-button">${uiText("focus.add_file", "添加文件")}<input id="fileInput" type="file" multiple hidden></label>${renderInterruptButton(detail)}${mainDeliveryRun(task.task_id) ? `<button type="button" data-action="queue-main">排队下一轮</button>` : ""}<button class="send-button" data-action="send-main">${uiText("focus.send", "发送")}</button></div></div>
            </div>
            <p id="composerFeedback" class="composer-feedback${feedback ? ` is-${feedback.kind}` : ""}" role="status"${feedback ? "" : " hidden"}>${feedback ? escapeHtml(feedback.text) : ""}</p>
            <div id="mainSubmissionRecovery" class="main-submission-recovery">${renderMainRecovery(task.task_id)}</div>
            <section id="conversationInputs" class="conversation-inputs" aria-label="补充与排队消息" aria-live="polite">${renderConversationInputs(task.task_id)}</section>
          </div>
        </div>
      </section>
      ${panelOpen ? `<aside class="file-panel" id="filePanel"><div class="panel-resizer" id="panelResizer" title="拖拽调整面板宽度"></div><div class="file-panel-inner"></div></aside>` : ""}
    </section></section>`;
  if (keepEditor) {
    patchFocusChrome(markup);
    // 首次详情可以填入已保存的草稿；真实编辑（包括尚未产生 input 的组字）始终优先。
    if (!composingMainInputs.has(previousInput) && composerDraft.value(task.task_id, null) === null
        && previousInput.value === "" && composerInput) previousInput.value = composerInput;
  }
  else app.innerHTML = markup;
  app.querySelector(".task-workspace-layout").append(appInspector);
  app.dataset.taskId = task.task_id;
  const freshConversation = document.querySelector("#conversation");
  if (!keepEditor && previousConversation && freshConversation && typeof freshConversation.replaceWith === "function") {
    freshConversation.replaceWith(previousConversation);
  }
  const conversation = document.querySelector("#conversation");
  reconcileConversationMarkup(conversation, `${renderConversation(detail, task)}${renderMustViewRecovery(detail)}`);
  roundChanges?.mount(document.querySelector("#roundChanges"), task.task_id, detail.active_run?.run_id);
  mountCommitmentRecovery(detail);
  restoreCommitmentPanels(conversation);
  restoreAccessReviewPanels(conversation);
  const commitmentBlocked = activeTaskHasCommitmentLock() || projectionBlocked
    || !!detail.pending_compression || !!detail.pending_must_view_report
    || !!detail.pending_access_review;
  const mainInput = document.querySelector("#mainInput");
  const sendButton = document.querySelector('[data-action="send-main"]');
  if (mainInput) mainInput.disabled = commitmentBlocked;
  mountMainFilePicker(task.task_id, mainInput);
  if (sendButton) sendButton.disabled = commitmentBlocked;
  // 原身份重试只核对/重放既有受理，不受随后出现的执行审批阻断。
  if (sendButton && taskRunOperations?.recovery(task.task_id)?.editing === false) sendButton.disabled = false;
  updateMainRecoveryControls();
  updateAtHighlight(mainInput);
  if (!keepEditor && inputFocused && mainInput && !mainInput.disabled) {
    mainInput.focus({ preventScroll: true });
    if (inputSelection) mainInput.setSelectionRange(inputSelection.start, inputSelection.end, inputSelection.direction);
  }
  conversation.scrollTop = previousConversation
    ? (wasPinned ? conversation.scrollHeight : previousScrollTop)
    : (detail.ui_state?.scrollTop ?? conversation.scrollHeight);
  conversationView.rememberScroll(conversation);
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
  container.classList.toggle("file-link-panel", !!state.filesPanel.file_link);
  if (state.filesPanel.file_link) return mountFileLinkPanel(container, state.filesPanel);
  releaseFileLinkPreview();
  const view = pluginViewForMaterial(state.filesPanel);
  const taskId = state.activeTaskId;
  if (view?.mountPanel) view.mountPanel(container, state.filesPanel, state, { onReference: value => addTaskFileReference(value, taskId) });
}

// Link previews are read-only and bound to one complete reference, including the
// originating task. The plugin editor remains the material workbench entry.
const linkedFilePreview = FocusFileLinks.createPreview({ runtime,
  isCurrent: target => state.filesPanel === target,
  onClose: () => window.__focusCloseFilePanel(),
  onCurrent: (reference, taskId) => openFileLink(reference, taskId),
  onReference: (reference, taskId) => addFileReference(reference, taskId),
  getPreview: () => window.FocusFilePreview });

function releaseFileLinkPreview() { linkedFilePreview.release(); }

function fileLinkReference(value = {}) {
  return FocusFileLinks.reference(value);
}

function openFileLink(reference, taskId = state.activeTaskId) {
  const ref = fileLinkReference(reference);
  if (!ref.path) return setStatus("文件链接缺少准确路径，无法定位文件", true);
  const task = state.tasks.find(item => item.task_id === taskId);
  if (!task || (ref.workspace_id && ref.workspace_id !== task.workspace_id)) return setStatus("文件链接的工作区与来源任务不匹配", true);
  ref.workspace_id ||= task.workspace_id;
  releaseFileLinkPreview();
  state.filesPanel = { ...ref, relative_path: ref.path, task_id: taskId, file_link: true };
  render();
}

async function mountFileLinkPanel(container, target) {
  return linkedFilePreview.mount(container, target);
}

const boundPanelResizers = new WeakSet();
function bindPanelResizer() {
  const resizer = document.querySelector("#panelResizer");
  const shell = document.querySelector(".focus-shell");
  if (!resizer || !shell || boundPanelResizers.has(resizer)) return;
  boundPanelResizers.add(resizer);
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
      preferenceStorage.setItem("focus-panel-width", String(state.panelWidth));
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

// 壳层三栏：窗口尺寸变化时对持久化的宽度做钳制并重新应用（内联自定义属性仍是唯一来源）。
// 未定制用户不介入（让 CSS 断点默认值驱动），定制用户才跟随窗口重新钳制。
window.addEventListener("resize", () => {
  if (panelResizeFrame != null) cancelAnimationFrame(panelResizeFrame);
  panelResizeFrame = requestAnimationFrame(() => {
    panelResizeFrame = null;
    if (!state.shellLayout.customized) return;
    const next = normalizeShellLayout(state.shellLayout, window.innerWidth);
    if (next.navWidth !== state.shellLayout.navWidth
      || next.inspectorWidth !== state.shellLayout.inspectorWidth
      || Boolean(next.navCollapsed) !== Boolean(state.shellLayout.navCollapsed)) {
      state.shellLayout = next;
      applyShellLayout(state.shellLayout);
      persistShellLayout();
    }
  });
});

function materialImageMarkup(materialId, className, alt, action = "zoom-image") {
  const actionAttr = action ? ` data-action="${action}"` : "";
  return `<img class="${className}" alt="${escapeHtml(alt)}"${actionAttr} data-material-task-id="${escapeHtml(state.activeTaskId || "")}" data-material-content-id="${escapeHtml(materialId)}">`;
}

function contentPlainText(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map(block => (block && typeof block === "object" && typeof block.text === "string" ? block.text : ""))
      .join("");
  }
  return "";
}

function messageBodyText(message) {
  const content = Array.isArray(message.content)
    ? message.content.filter(block => block?.type === "text").map(block => block.text || "").join("\n")
    : typeof message.content === "string" ? message.content : "";
  return stripImageReferences(stripRunMaterialProtocol(content, message));
}

function collectMessageImages(content) {
  return imageMaterialPicker.materialRefIds(contentPlainText(content));
}

// Every artifact has a preview or explicit native file exits.
const FILE_VIEWABLE_RE = /\S/;

function filePathFromLinkHref(href) {
  return FocusFileLinks.pathFromHref(href);
}


function renderFileCards(message) {
  const files = message.files;
  if (!Array.isArray(files) || !files.length) return "";
  const cards = files.map(file => {
    const name = String(file?.relative_path || file?.path || file?.filename || file?.name || "");
    if (!name) return "";
    const reference = fileLinkReference({ ...file, path: file.path || file.relative_path || name });
    const viewable = FILE_VIEWABLE_RE.test(reference.path);
    const icon = /\.(png|jpe?g|webp|bmp|gif)$/i.test(name) ? "file-image" : /\.(pdf|docx?|md|txt)$/i.test(name) ? "file-text" : "file";
    const size = file?.size ? ` · ${formatBytes(file.size)}` : "";
    const iconMarkup = `<span class="ui-icon is-sm icon-${icon}" aria-hidden="true"></span>`;
    return viewable
      ? `<button class="file-card" data-action="open-file-panel" data-file-reference="${escapeHtml(JSON.stringify(reference))}" title="${escapeHtml(reference.path)}">${iconMarkup}<span>${escapeHtml(name)}${size}</span></button>`
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

function renderMessageImages(images) {
  if (!images.length) return "";
  return `<div class="message-images">${images
    .map(materialId => materialImageMarkup(materialId, "message-image", "图片材料"))
    .join("")}</div>`;
}

function stripImageReferences(text) {
  return imageMaterialPicker.stripMaterialRefs(text);
}

function messageMaterialHistory(message) {
  const messageId = message?.id || message?.message_id;
  if (!messageId) return [];
  const history = (state.materialHistory.get(state.activeTaskId) || [])
    .filter(item => item.message_id === messageId)
    .sort((a, b) => Number(a.ordinal) - Number(b.ordinal));
  if (history.length) return history;
  // Inherited questions retain their manifest, while binding rows remain with
  // the original task. Keep their cards and prose readable in the new branch.
  try { return FocusMessageRetry.materialBindings(message); } catch { return []; }
}

function stripRunMaterialProtocol(text, message) {
  if (!messageMaterialHistory(message).length) return text;
  return String(text).replace(/(?:\n\n)?<focus_run_materials>\n[\s\S]*?\n<\/focus_run_materials>/g, "");
}

function renderRunMaterialCards(message) {
  const rows = messageMaterialHistory(message);
  if (!rows.length) return "";
  return `<section class="message-materials"><header><strong>本轮材料</strong><span class="ui-badge">${rows.length}</span></header>${rows.map(item => `<article class="message-material-card">${item.material_kind === "image" && item.current_available ? materialImageMarkup(item.material_id, "message-material-thumb", item.relative_path) : `<span class="ui-icon is-sm icon-${item.material_kind === "image" ? "file-image" : "file"}" aria-hidden="true"></span>`}<span><strong>${escapeHtml(item.relative_path)}</strong>${item.note ? `<p>${escapeHtml(item.note)}</p>` : '<small class="muted">无备注</small>'}${item.must_view_requested ? '<small class="ui-badge is-warning">必须看</small>' : ""}${item.current_available === false ? '<small class="muted">当前内容不可用</small>' : ""}</span></article>`).join("")}</section>`;
}

function renderContentBlock(block, message = null) {
  if (!block || typeof block !== "object") return escapeHtml(String(block ?? ""));
  if (block.type === "image_url") {
    return ""; // 图片已由 collectMessageImages 提取到消息框上方,气泡内不输出
  }
  // text 块:移除图片引用(图片已提取到上方行),其余转义
  return escapeHtml(stripImageReferences(stripRunMaterialProtocol(conversationReference.stripProtocol(String(block.text ?? ""), message?.additional_kwargs?.message_reference), message)));
}

function messageKeyOf(message, fallbackIndex) {
  return message?.id || message?.message_id || `${message?.role || "message"}:${fallbackIndex}`;
}

function renderMessage(message, { showRoleHeader = false, fallbackKey = 0, historyActions = false } = {}) {
  // 后端兜底降级消息按工具结果样式渲染，不泄露原始 XML 标签
  const degraded = compressionPanel.degradedParts(message);
  const messageKey = messageKeyOf(message, fallbackKey);
  if (degraded) {
    return `<article class="work-record message tool" data-message-key="${escapeHtml(messageKey)}"><header class="work-record-header"><span class="work-record-kicker">TOOL</span><span class="message-role">工具 · ${escapeHtml(degraded.name || "tool")}</span></header><div class="message-content">${escapeHtml(degraded.content)}</div></article>`;
  }
  const semantics = contextCuratorPresentation.messageSemantics(message);
  const kind = semantics.kind;
  const reference = message.additional_kwargs?.message_reference;
  // 安全:字符串 human 消息 MUST 转义(否则消息内 HTML 会注入 DOM,如 <style> 覆盖主题变量)。
  const messageImages = collectMessageImages(message.content);
  let content;
  if (typeof message.content === "string") {
    content = kind === "ai" ? message.content : escapeHtml(stripImageReferences(stripRunMaterialProtocol(conversationReference.stripProtocol(message.content, reference), message)));
  } else if (Array.isArray(message.content)) {
    content = message.content.map(block => renderContentBlock(block, message)).join("\n");
  } else {
    content = escapeHtml(JSON.stringify(message.content, null, 2));
  }
  const answerText = kind === "ai" ? messageBodyText(message) : "";
  const renderedContent = kind === "ai" ? renderAssistantContent(answerText) : content;
  const actions = renderHistoryMessageActions(message, kind, historyActions);
  const associations = semantics.associations.map(item => `<span class="message-association">${escapeHtml(item.direction)} ${escapeHtml(item.name)}${item.tool_call_id ? ` · ${escapeHtml(item.tool_call_id)}` : ""}</span>`).join("");
  let header = "";
  if (showRoleHeader) {
    header = `<header class="work-record-header"><span class="work-record-kicker">${escapeHtml(semantics.roleLabel)}</span>${associations}</header>`;
  } else if (kind === "system") {
    header = `<header class="work-record-header"><span class="work-record-kicker">CONTEXT</span><span class="message-role">系统上下文</span>${associations}</header>`;
  }
  const fileContext = fileLinkReference(message.file_reference || message.file_context || {
    workspace_id: message.workspace_id, run_id: message.run_id, slot_id: message.slot_id,
  });
  return `<article class="work-record message ${kind}" data-message-key="${escapeHtml(messageKey)}"${message.id || message.message_id ? ` data-message-id="${escapeHtml(message.id || message.message_id)}"` : ""} data-file-task-id="${escapeHtml(message.task_id || state.activeTaskId || "")}" data-file-context="${escapeHtml(JSON.stringify(fileContext))}">
    ${header}
    ${renderMessageImages(messageImages)}${renderFileCards(message)}${renderRunMaterialCards(message)}
    ${conversationReference.render(reference, { editable: false })}
    <div class="message-content">${renderedContent}</div>
    ${actions}
  </article>`;
}

function renderHistoryMessageActions(message, kind, enabled) {
  const source = messageBodyText(message);
  const copy = kind === "ai" ? messageReading.renderActions(source)
    : kind === "human" ? messageReading.renderActions(source, uiText("conversation.copy_question", "复制提问")).replace('data-action="copy-answer"', 'data-action="copy-question"') : "";
  if (!enabled || message.curation_synthetic || !(message.id || message.message_id)) return copy;
  const action = (name, key, label) => `<button type="button" class="text-button" data-action="${name}">${escapeHtml(uiText(key, label))}</button>`;
  const extra = kind === "human"
    ? action("edit-history-message", "conversation.edit_resend", "编辑后重新发送") + action("retry-history-message", "conversation.retry_here", "从这里重新尝试")
    : kind === "ai" && source.trim()
      ? action("quote-history-message", "conversation.quote", "引用追问") + action("retry-history-message", "conversation.regenerate", "重新回答") : "";
  if (!extra) return copy;
  return copy ? copy.replace("</div>", `${extra}</div>`) : `<div class="message-actions">${extra}</div>`;
}

let messageRetryDraft = null;
let messageRetryLoading = false;

function closeMessageRetry() {
  if (messageRetryDraft?.pending) return;
  const draft = messageRetryDraft;
  messageRetryDraft = null;
  document.querySelector("#messageRetryDialog")?.remove();
  draft?.trigger?.focus();
}

function quoteHistoryMessage(button) {
  const key = button.closest("[data-message-key]")?.dataset.messageKey;
  const messages = compressionPanel.expandForConversation(state.details.get(state.activeTaskId)?.messages || []);
  const message = messages.find(item => messageKeyOf(item) === key);
  const source = message && messageBodyText(message);
  const input = document.querySelector("#mainInput");
  if (!source?.trim() || !input) return;
  const taskId = state.activeTaskId;
  const current = composerDraft.value(taskId, input.value || "");
  input.value = `${current}${current ? "\n\n" : ""}${source.split("\n").map(line => `> ${line}`).join("\n")}\n\n`;
  composerDraft.claim(taskId, input.value);
  updateAtHighlight(input);
  void persistFocusState();
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

async function openMessageRetry(button, edit) {
  if (messageRetryLoading || messageRetryDraft) return;
  const task = activeTask();
  const messageId = button.closest("[data-message-key]")?.dataset.messageKey;
  if (!task || !messageId || task.loop || state.details.get(task.task_id)?.context?.managed_status) return;
  const navigation = taskSwitchSequence;
  messageRetryLoading = true;
  try {
    const snapshot = await api(`/desktop/api/contexts/${task.task_id}/snapshot`);
    if (navigation !== taskSwitchSequence || state.view !== "focus") return;
    if (!snapshot.checkpoint_id) throw new Error("这条消息尚无已提交的会话快照，请稍后重试");
    const turn = FocusMessageRetry.selectTurn(snapshot.messages, messageId);
    messageRetryDraft = {
      task, snapshot, turn, edit, navigation, trigger: button, pending: false, context: null, materials: new Map(),
      options: { skills: selectedSkills("main"), ...accessMode.requestBody(accessModeOf("main")) },
    };
    const title = edit ? uiText("conversation.edit_resend", "编辑后重新发送") : uiText("conversation.retry_here", "从这里重新尝试");
    document.querySelector("#overlayRoot").innerHTML = `<dialog id="messageRetryDialog" class="message-retry-dialog" aria-labelledby="messageRetryTitle"><div class="dialog-form">
      <h2 id="messageRetryTitle">${escapeHtml(title)}</h2><p>${escapeHtml(uiText("conversation.branch_notice", "将从这条提问之前创建新分支。原会话及之后的记录会保留，文件改动不会回退。"))}</p>
      <label>${escapeHtml(uiText("conversation.question", "提问"))}<textarea id="messageRetryInput" ${edit ? "autofocus" : "readonly"}>${escapeHtml(FocusMessageRetry.editableText(turn.message))}</textarea></label>
      <p id="messageRetryError" class="danger" role="alert"></p><div class="dialog-actions"><button type="button" data-action="cancel-message-retry">${escapeHtml(uiText("conversation.cancel", "取消"))}</button><button type="button" class="primary" data-action="submit-message-retry">${escapeHtml(uiText("conversation.send_branch", "在新分支重新发送"))}</button></div>
    </div></dialog>`;
    const dialog = document.querySelector("#messageRetryDialog");
    dialog.addEventListener("cancel", event => { event.preventDefault(); closeMessageRetry(); });
    dialog.showModal();
    if (!edit) await submitMessageRetry();
  } catch (error) { setStatus(error.message, true); }
  finally { messageRetryLoading = false; }
}

async function submitMessageRetry() {
  const draft = messageRetryDraft;
  if (!draft || draft.pending) return;
  const input = document.querySelector("#messageRetryInput");
  const editedText = draft.edit ? input.value.trim() : null;
  const errorNode = document.querySelector("#messageRetryError");
  if (draft.edit && !editedText) { errorNode.textContent = uiText("conversation.question_required", "提问不能为空"); return; }
  const dialog = document.querySelector("#messageRetryDialog");
  draft.pending = true;
  dialog.querySelectorAll("button, textarea").forEach(node => { node.disabled = true; });
  errorNode.textContent = "";
  try {
    // Freeze the source by checkpoint, never slice the visible/windowed DOM history.
    if (!draft.context) draft.context = await api("/desktop/api/contexts/derive", { method: "POST", body: JSON.stringify({
      title: `${draft.task.title.slice(0, 180)} · 重试`,
      sources: [{ context_id: draft.task.task_id, checkpoint_id: draft.snapshot.checkpoint_id }], messages: draft.turn.messages,
    }) });
    const context = draft.context;
    if (!["valid", "repaired", "approved"].includes(context.projection_status)) throw new Error("该历史上下文需要先处理安全执行投影，请在上下文与派生中查看");
    const [history, materials] = await Promise.all([
      api(`/desktop/api/tasks/${draft.task.task_id}/material-history`),
      api(`/desktop/api/tasks/${draft.task.task_id}/materials`),
    ]);
    const localBindings = history.filter(item => item.message_id === (draft.turn.message.id || draft.turn.message.message_id));
    const inheritedBindings = FocusMessageRetry.materialBindings(draft.turn.message);
    const bindings = (inheritedBindings.length ? inheritedBindings.map(item => localBindings.find(local => local.material_id === item.material_id) || item) : localBindings).sort((a, b) => Number(a.ordinal) - Number(b.ordinal));
    // Material IDs belong to a task. Re-enroll their paths in the branch and carry
    // notes/must-view intent; never silently drop an unavailable attachment.
    for (const binding of bindings) {
      if (binding.current_available === false) throw new Error(`原提问的材料已不可用：${binding.relative_path}`);
      const original = materials.find(item => item.material_id === binding.material_id);
      if (!draft.materials.has(binding.material_id)) draft.materials.set(binding.material_id, await api(`/desktop/api/tasks/${context.context_id}/materials`, { method: "POST", body: JSON.stringify({ path: binding.relative_path, reading_mode: original?.reading_mode || "full", instruction_mode: original?.instruction_mode || "reference" }) }));
    }
    let content = FocusMessageRetry.replayContent(draft.turn.message, editedText);
    const remap = text => {
      for (const [oldId, material] of draft.materials) text = text.replaceAll(`material_id=${oldId}`, `material_id=${material.material_id}`);
      return text;
    };
    content = typeof content === "string" ? remap(content) : content.map(block => typeof block.text === "string" ? { ...block, text: remap(block.text) } : block);
    const taskId = context.context_id;
    const message = editedText ?? FocusMessageRetry.editableText(draft.turn.message);
    await api(`/desktop/api/tasks/${taskId}/access-mode`, { method: "PUT", body: JSON.stringify({ access_mode: draft.options.access_mode }) });
    const requestId = crypto.randomUUID();
    const replayBindings = bindings.map(item => ({ materialId: draft.materials.get(item.material_id).material_id, note: item.note || "" }));
    const requiredImageIds = bindings.filter(item => item.must_view_requested).map(item => draft.materials.get(item.material_id).material_id);
    taskRunOperations.freeze(taskId, requestId, { input: message, message, selectionSignature: JSON.stringify({ bindings: replayBindings, requiredImageIds, notes: Object.fromEntries(replayBindings.map(item => [item.materialId, item.note])) }), body: JSON.stringify({
      message: content, ...draft.options,
      ...(draft.turn.message.additional_kwargs?.message_reference ? { message_reference: draft.turn.message.additional_kwargs.message_reference } : {}),
      material_inputs: replayBindings.map(item => ({ material_id: item.materialId, note: item.note })),
      must_view_material_ids: requiredImageIds,
    }) });
    await refreshTasks();
    await hydrateContextTrees();
    draft.pending = false;
    closeMessageRetry();
    if (draft.navigation !== taskSwitchSequence || state.view !== "focus" || state.activeTaskId !== draft.task.task_id) return;
    await switchTask(taskId);
    // A newer navigation owns the screen. Its composer must never submit this retry.
    if (state.activeTaskId === taskId && state.view === "focus") await sendMain(true);
  } catch (error) {
    errorNode.textContent = error.message;
    setStatus(error.message, true);
  } finally {
    draft.pending = false;
    dialog.querySelectorAll("button, textarea").forEach(node => { node.disabled = false; });
  }
}

function collectImagesFromMessages(messages, depth = 0) {
  if (depth > 8) return [];
  const urls = [];
  for (const message of Array.isArray(messages) ? messages : []) {
    if (!message || typeof message !== "object") continue;
    for (const url of collectMessageImages(message.content)) {
      if (!urls.includes(url)) urls.push(url);
    }
    if (message.compression && Array.isArray(message.compression.source)) {
      for (const url of collectImagesFromMessages(message.compression.source, depth + 1)) {
        if (!urls.includes(url)) urls.push(url);
      }
    }
  }
  return urls;
}

function renderCompressionBlockImages(images) {
  if (!images.length) return "";
  return `<div class="compression-block-images">${images
    .map(materialId => materialImageMarkup(materialId, "compression-block-image", "被压缩的图片材料"))
    .join("")}</div>`;
}

function renderCompressionDivider(item) {
  // 压缩块/删除墓碑分界标记：原文已在其后原位展开显示；删除无摘要，仅提示
  const dividerKey = item.deleted ? `deleted:${item.block_id || item.count}` : `block:${item.block_id || item.count}`;
  if (item.deleted) {
    return `<div class="compression-block-divider is-deleted" data-divider-key="${escapeHtml(dividerKey)}"><span><span class="ui-icon is-sm icon-trash-2" aria-hidden="true"></span>已删除 · 来源 ${item.count} 条（模型不可见，可在压缩面板中恢复）</span></div>`;
  }
  return `<div class="compression-block-divider" data-divider-key="${escapeHtml(dividerKey)}">
    <details class="compression-block-summary"><summary><span class="ui-icon is-sm icon-package" aria-hidden="true"></span>压缩块 · 来源 ${item.count} 条</summary><div class="compression-block-summary-body">${escapeHtml(item.summary)}</div></details>
    ${renderCompressionBlockImages(collectImagesFromMessages(item.source))}
  </div>`;
}

function _conversationRenderInput(detail, task) {
  return {
    detail,
    task,
    locale: interfaceI18n.locale(),
    state: {
      activeTaskId: state.activeTaskId,
      streamBuffers: state.streamBuffers,
      materialHistory: state.materialHistory,
      pluginViewCount: Object.keys(pluginViews).length,
      historyActionsEnabled: !detail.context?.managed_status && !task.loop,
    },
    markdown: mdRenderer,
    events: conversationEvents,
    expandMessages: compressionPanel.expandForConversation,
    renderMessage: (message, options) => renderMessage(message, { ...options, historyActions: !detail.context?.managed_status && !task.loop }),
    renderDivider: renderCompressionDivider,
    escapeHtml,
    windowLimit: conversationView.windowLimit(task.task_id),
    windowRows: conversationView.windowRows(task.task_id),
  };
}

function renderConversation(detail, task) {
  const { units, eventIndex } = conversationRender.buildUnits(_conversationRenderInput(detail, task));
  for (const [key, value] of eventIndex) conversationEventIndex.set(key, value);
  return units.map(unit => unit.html).join("");
}

function conversationRenderStats() {
  return conversationRender.stats();
}

function loadEarlierConversation() {
  const task = activeTask();
  if (!task) return;
  const conversation = document.querySelector("#conversation");
  const detail = state.details.get(task.task_id);
  conversationView.loadEarlier(conversation, task.task_id, () => {
    if (detail) conversationView.reconcile(conversation, renderConversation(detail, task));
  }, detail?.messages);
}

function conversationWindowLimit(taskId) {
  return conversationView.windowLimit(taskId);
}

function reconcileConversationMarkup(conversation, html) {
  conversationView.reconcile(conversation, html);
}

function replaceConversation(task, messages, snapshotRunId) {
  finalizeStreamingOnSnapshot(task.task_id, messages, snapshotRunId);
  const detail = state.details.get(task.task_id) || {};
  detail.messages = messages;
  state.details.set(task.task_id, detail);
  refreshConversation(task);
}

function refreshConversation(task) {
  if (!task) return;
  const detail = state.details.get(task.task_id);
  if (!detail) return;
  if (state.view !== "focus" || state.activeTaskId !== task.task_id) return;
  const conversation = document.querySelector("#conversation");
  if (!conversation) return;
  conversationView.trackScroll(conversation);
  reconcileConversationMarkup(conversation, `${renderConversation(detail, task)}${renderMustViewRecovery(detail)}`);
  restoreCommitmentPanels(conversation);
  restoreAccessReviewPanels(conversation);
  materialContentLoader.bindAll(conversation);
  conversationView.syncScrollAfterWrite(conversation);
  roundChanges?.schedule();
}

const roundChanges = window.FocusRoundChanges?.create({
  api,
  diff: window.FocusLoopFactsView,
  onReview: (text, taskId) => {
    if (state.activeTaskId !== taskId || state.view !== "focus") return false;
    const input = document.querySelector("#mainInput");
    if (!input || input.disabled) return false;
    input.value = input.value.trim() ? `${input.value}\n\n${text}` : text;
    composerDraft.claim(taskId, input.value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.focus();
    persistFocusState();
    return true;
  },
});

conversationView.configure({
  reconciler: conversationReconciler,
  events: conversationEvents,
  render: conversationRender,
  markdown: mdRenderer,
  eventIndex: conversationEventIndex,
});

function renderStreamingContent(buffer) {
  // 生成中的正文与完成态共用同一富文本容器与同一渲染器,
  // 使完整快照到达时的对账成为无操作,消除完成瞬间的整块替换与跳变。
  return conversationRender.streamingContent(mdRenderer, conversationEvents, buffer);
}

function appendStreamDelta(envelope, field) {
  const task = state.tasks.find(item => item.thread_id === envelope.thread_id && item.workspace_id === envelope.workspace_id);
  const content = envelope.data?.content;
  const messageId = envelope.data?.message_id;
  // 防御：承诺子图冒泡消息（commitment-stage-*）只进轨迹面板，不进 lead 对话流
  if (typeof messageId === "string" && messageId.startsWith("commitment-stage-")) return;
  if (!task || !content || !envelope.agent_id.startsWith("main:")) return;
  beginLeadExecution(task.task_id);
  const buffer = state.streamBuffers.get(envelope.run_id) || { taskId: task.task_id, text: "", reasoning: "", messageId: null };
  buffer.runId = envelope.run_id;
  if (typeof messageId === "string" && messageId && messageId !== buffer.messageId) {
    buffer.messageId = messageId;
    buffer.text = "";
    buffer.reasoning = "";
    buffer.blocks = [];
    buffer.blockEntries = [];
    buffer.streamCheckpoint = null;
    buffer.streamPreview = null;
    buffer.streamExpandedVersion = 0;
    buffer.reasoningRendered = undefined;
  }
  const accumulated = buffer[field] || "";
  buffer[field] = accumulated + content;
  state.streamBuffers.set(envelope.run_id, buffer);
  if (state.streamFrames.has(envelope.run_id)) return;
  state.streamFrames.set(envelope.run_id, requestAnimationFrame(() => {
    state.streamFrames.delete(envelope.run_id);
    if (state.view !== "focus" || state.activeTaskId !== task.task_id) return;
    const conversation = document.querySelector("#conversation");
    if (!conversation) return;
    conversationView.syncStreamingPlaceholder(conversation, envelope.run_id, buffer);
    conversationView.syncScrollAfterWrite(conversation);
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

function finalizeStreamingOnSnapshot(taskId, messages, snapshotRunId) {
  // 快照是该 run 的权威状态：命中缓冲记录的消息 id，或快照里确实出现了该缓冲的正文（信封不带
  // message_id 时的判据）时，流式占位已由完成态取代，缓冲不再需要。中途快照不得回收缓冲——
  // 否则占位会先消失、再被下一条增量重建，流式正文只剩尾巴。
  const ids = new Set((messages || []).map(message => message && (message.id || message.message_id)).filter(Boolean));
  for (const [runId, buffer] of state.streamBuffers) {
    if (buffer.taskId !== taskId) continue;
    if (buffer.messageId && ids.has(buffer.messageId)) { clearStreamBuffer(runId); continue; }
    if (snapshotRunId && runId === snapshotRunId && _snapshotCarriesStreamedText(messages, buffer)) clearStreamBuffer(runId);
  }
}

function _streamedPrefix(value, limit = 40) {
  const compact = String(value || "").trim();
  return compact.length <= limit ? compact : compact.slice(0, limit);
}

function _snapshotCarriesStreamedText(messages, buffer) {
  const text = _streamedPrefix(buffer.text);
  const reasoning = _streamedPrefix(buffer.reasoning);
  if (!text && !reasoning) return false;
  return (messages || []).some(message => {
    if (!message || (message.role !== "ai" && message.role !== "assistant")) return false;
    const content = String(message.content || "");
    const think = String(message.reasoning_content || "");
    return (!text || content.includes(text)) && (!reasoning || think.includes(reasoning));
  });
}

function renderImageMaterial(material) {
  const open = state.openMaterial === material.material_id;
  const selection = materialSelection();
  const attached = selection.bindings.some(item => item.materialId === material.material_id);
  const required = selection.requiredImageIds.includes(material.material_id);
  const reason = material.size_bytes ? "" : "该材料内容为空，无法用于本轮";
  return `<article class="material-row is-image" draggable="true" data-material-id="${material.material_id}">
    <header class="material-summary">
      <button class="material-title-button" data-action="preview-image-material" title="放大查看">${materialImageMarkup(material.material_id, "material-thumb", "", null)}<span><strong class="material-name">${escapeHtml(material.relative_path)}</strong><small>图片材料 · ${formatBytes(material.size_bytes)}</small></span></button>
      <button class="text-button" data-action="toggle-material" aria-expanded="${open}">${open ? "收起" : "管理"}</button>
    </header>
    <div class="material-policy-row"><button class="text-button must-view-toggle" data-action="toggle-run-material" aria-pressed="${attached}"${reason ? " disabled" : ""}>${attached ? "☑" : "☐"} 本轮使用</button><button class="text-button must-view-toggle" data-action="toggle-image-required" aria-pressed="${required}"${!attached || reason ? " disabled" : ""}>${required ? "☑" : "☐"} 必须看</button>${reason ? `<span class="muted tiny">${escapeHtml(reason)}</span>` : ""}</div>
    ${renderRunMaterialNote(material.material_id)}
    ${open ? `<div class="material-editor">
      <div class="material-source-meta"><span>来源</span><code>${escapeHtml(material.relative_path)}</code></div>
      <span class="material-actions"><button class="text-button" data-action="move-material-group">移动分组</button>${material.custom_group_id ? '<button class="text-button" data-action="restore-auto-group">恢复自动归类</button>' : ""}<button class="text-button danger" data-action="delete-material">删除</button></span>${renderMaterialUsageHistory(material.material_id)}</div>` : ""}
  </article>`;
}

function renderMaterial(material) {
  if (material.is_image) return renderImageMaterial(material);
  const open = state.openMaterial === material.material_id;
  const viewable = !!pluginViewForMaterial(material);
  const reading = material.reading_mode === "full" ? "完整阅读" : "粗略阅读";
  const instruction = material.instruction_mode === "strict" ? "严格约束" : "参考材料";
  const retention = material.retention === "irreplaceable" ? "版本保护" : "可移除";
  const selected = materialSelection().bindings.some(item => item.materialId === material.material_id);
  const unavailable = !material.size_bytes;
  return `<article class="material-row" draggable="true" data-material-id="${material.material_id}">
    <header class="material-summary">
      <button class="material-title-button" data-action="${viewable ? "open-material" : "toggle-material"}" title="${viewable ? "在文件工作台打开" : "查看材料规则"}"><span class="material-file-icon" aria-hidden="true"><span class="ui-icon is-sm icon-file-text"></span></span><span><strong class="material-name">${escapeHtml(material.relative_path)}</strong><small>${viewable ? "可在文件工作台查看" : "未启用匹配的查看器"}</small></span></button>
      <button class="text-button" data-action="toggle-material" aria-expanded="${open}">${open ? "收起" : "管理"}</button>
    </header>
    <div class="material-policy-row"><button class="text-button must-view-toggle" data-action="toggle-run-material" aria-pressed="${selected}"${unavailable ? " disabled" : ""}>${selected ? "☑" : "☐"} 本轮使用</button><span class="ui-badge">${reading}</span><span class="ui-badge${material.instruction_mode === "strict" ? " is-warning" : ""}">${instruction}</span><span class="ui-badge${material.retention === "irreplaceable" ? " is-success" : ""}">${retention}</span>${unavailable ? '<span class="muted tiny">内容不可用</span>' : ""}</div>
    ${renderRunMaterialNote(material.material_id)}
    ${material.needs_confirmation ? `<div class="ui-notice is-warning"><strong>检测到外部删除</strong><span>Focus 已恢复文件，请从版本记录确认内容。</span></div>` : ""}
    ${open ? `<div class="material-editor">
      <label>阅读方式<select data-field="reading_mode"><option value="full" ${material.reading_mode === "full" ? "selected" : ""}>完整阅读</option><option value="rough" ${material.reading_mode === "rough" ? "selected" : ""}>粗略阅读</option></select></label>
      <label>约束<select data-field="instruction_mode"><option value="reference" ${material.instruction_mode === "reference" ? "selected" : ""}>仅供参考</option><option value="strict" ${material.instruction_mode === "strict" ? "selected" : ""}>严格遵守</option></select></label>
      <label>文件规则<select data-field="retention"><option value="removable" ${material.retention === "removable" ? "selected" : ""}>可移除</option><option value="irreplaceable" ${material.retention === "irreplaceable" ? "selected" : ""}>不可遗失</option></select></label>
      <div class="material-source-meta"><span>来源</span><code>${escapeHtml(material.relative_path)}</code></div>
      <span class="material-actions"><button class="primary" data-action="save-material">保存规则</button><button class="text-button" data-action="move-material-group">移动分组</button>${material.custom_group_id ? '<button class="text-button" data-action="restore-auto-group">恢复自动归类</button>' : ""}
      ${material.retention === "irreplaceable" ? `<button class="text-button" data-action="clear-material">置空</button><button class="text-button" data-action="load-versions">版本</button>` : `<button class="text-button danger" data-action="delete-material">删除</button>`}</span>
      <ul class="version-list" data-versions></ul>${renderMaterialUsageHistory(material.material_id)}
    </div>` : ""}
  </article>`;
}

function renderMaterialUsageHistory(materialId) {
  const rows = (state.materialHistory.get(state.activeTaskId) || []).filter(item => item.material_id === materialId);
  if (!rows.length) return '<p class="muted tiny">线程内尚未使用</p>';
  return `<details class="material-usage-history"><summary>线程内使用记录 · ${rows.length}</summary><ol>${rows.map(item => `<li><span>Run ${escapeHtml(String(item.run_id).slice(0, 8))}${item.must_view_requested ? " · 必须看" : ""}</span>${item.note ? `<p>${escapeHtml(item.note)}</p>` : '<small class="muted">无备注</small>'}</li>`).join("")}</ol></details>`;
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
  root.hidden = detail.ui_state?.show_session_patrol === false;
}

function updatePatrolAvatarLayer(taskId = state.activeTaskId) {
  if (!patrolAvatarController || state.view !== "focus" || taskId !== state.activeTaskId) return;
  const task = activeTask();
  const detail = state.details.get(taskId);
  if (task && detail) {
    patrolAvatarController.update(patrolAvatarOptions(task, detail));
    const layer = document.querySelector("#patrolAvatarLayer");
    if (layer) layer.hidden = detail.ui_state?.show_session_patrol === false;
  }
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
    const run = await api(`/desktop/api/agents/${state.agentDetails.agentId}/continue`, { method: "POST", body: JSON.stringify({ message, ...(window.FocusPatrolBranches?.get(state.agentDetails.agentId) || {}) }) });
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
  app.innerHTML = pluginView.render(state.plugins.plugins, state.plugins.interfaces, state.plugins.traces, state.plugins);
  interfaceI18n.apply(app);
}

async function hydratePlugins() {
  const requestId = ++pluginHydrationSequence;
  state.plugins = { ...state.plugins, loading: true, error: null };
  if (state.view === "plugins") renderPlugins();
  try {
    const [data, traceData] = await Promise.all([
      api("/desktop/api/plugins"),
      api("/desktop/api/plugins/traces"),
    ]);
    const plugins = data.plugins || [];
    if (requestId !== pluginHydrationSequence) return false;
    const selectedName = plugins.some(item => item.name === state.plugins.selectedName)
      ? state.plugins.selectedName
      : null;
    state.plugins = { plugins, interfaces: data.interfaces || {}, traces: traceData.traces || [], filter: state.plugins.filter || "all", selectedName, query: state.plugins.query || "" };
    void ensurePluginAssets(plugins);
    return true;
  } catch (error) {
    if (requestId === pluginHydrationSequence) state.plugins = { ...state.plugins, error: error.message };
    return false;
  } finally {
    if (requestId === pluginHydrationSequence) {
      state.plugins.loading = false;
      if (state.view === "plugins") renderPlugins();
    }
  }
}

async function openPluginsView() {
  if (state.view === "focus") persistFocusState();
  cancelPendingViewRequests();
  const requestId = pluginViewRequestSequence;
  state.inspector.open = false;
  state.view = "plugins";
  render();
  await hydratePlugins();
  if (requestId !== pluginViewRequestSequence) return;
  state.view = "plugins";
  render();
}

function renderMemory() {
  const s = state.memory;
  app.innerHTML = memoryView.render(s.memories, {
    loading: s.loading,
    error: s.error,
    query: s.query,
    category: s.category,
    layout: s.layout,
    selectedId: s.selectedId,
    composing: s.composing,
    draft: s.draft,
    strategyImport: s.strategyImport,
    authoring: { ...s.strategyAuthoring, saving: Boolean(s.strategySaving) },
    workspaces: [...new Map([...s.memories, ...state.tasks, ...(state.patrolWorkspace ? [state.patrolWorkspace] : [])].filter(t => t.workspace_id).map(t => [t.workspace_id, { id: t.workspace_id, name: t.workspace_name || t.display_name || t.workspace_id }])).values()],
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
  interfaceI18n.apply(app);
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
  if (field.startsWith("strategy:")) draft.strategy = { ...draft.strategy, [field.slice(9)]: value };
  else if (field.startsWith("package:")) draft.strategy_package = { ...draft.strategy_package, [field.slice(8)]: value };
  else if (field.startsWith("scenario:")) draft.rehearsal_scenarios = { ...draft.rehearsal_scenarios, [field.slice(9)]: value };
  else if (field === "strategy-source") draft.strategy_source = value;
  else if (field === "workspace_id") draft.workspace_id = value || null;
  else if (field === "enabled") draft.enabled = Boolean(value);
  else if (field === "title") draft.title = value;
  else if (field === "content") { draft.content = value; draft.contentDirty = true; }
  else if (field === "contentMode") { draft.contentMode = value; state.memory = { ...state.memory, draft }; render(); return; }
  state.memory = { ...state.memory, draft };
  if (field.startsWith("strategy:") || field.startsWith("scenario:") || field === "title") {
    draft.rehearsal_result = null;
    document.querySelector("[data-strategy-rehearsal-results]")?.remove?.();
  }
}

// 编辑分段记忆的某个段（title 或 body）。
function editMemorySegment(index, field, value) {
  const draft = { ...(state.memory.draft || {}) };
  const segments = (draft.segments || []).map((seg, i) => i === index ? { ...seg, [field]: value } : seg);
  state.memory = { ...state.memory, draft: { ...draft, segments } };
}

async function hydrateMemory() {
  const requestId = ++memoryHydrationSequence;
  const viewId = memoryViewRequestSequence;
  const selection = state.memory.selectedId;
  const isCurrent = () => requestId === memoryHydrationSequence && viewId === memoryViewRequestSequence;
  state.memory = { ...state.memory, loading: true, error: null };
  if (state.view === "memory") renderMemory();
  try {
    const data = await api("/desktop/api/memory");
    if (!isCurrent()) return false;
    const memories = data.memories || [];
    const selectedId = state.memory.selectedId !== selection || memories.some(item => item.memory_id === selection)
      ? state.memory.selectedId
      : null;
    state.memory = { ...state.memory, memories, selectedId };
    return true;
  } catch (error) {
    if (isCurrent()) state.memory = { ...state.memory, error: error.message };
    return false;
  } finally {
    if (requestId === memoryHydrationSequence) {
      state.memory.loading = false;
      if (isCurrent() && state.view === "memory") renderMemory();
    }
  }
}

async function openMemoryView() {
  strategyImportRequestSequence++;
  strategyAuthoringRequestSequence++;
  state.memory.strategyAuthoring = null;
  if (state.view === "focus") persistFocusState();
  cancelPendingViewRequests();
  state.inspector.open = false;
  state.memory = { ...state.memory, strategyImport: null, composing: false, selectedId: state.memory.selectedId, sessions: [], activeSessionId: null, enabledMessages: [], selectedMessageIds: [], activeMessageId: null, collectedSources: [], draft: null };
  state.view = "memory";
  render();
  await hydrateMemory();
}

function memorySessionCandidates() {
  return state.tasks.filter(task =>
    task.harness_mode !== "assembly" && sessionLifecycle(task) === "active"
  );
}

function openMemoryCompose(editing = false) {
  strategyAuthoringRequestSequence++;
  strategyImportRequestSequence++;
  const existing = editing ? (state.memory.memories.find(m => m.memory_id === state.memory.selectedId) || null) : null;
  const fresh = {
    composing: true,
    strategyAuthoring: null,
    strategyImport: null,
    draft: {
      memory_id: existing?.memory_id, category: existing?.category || "memory",
      strategy: existing?.strategy || {}, enabled: existing?.enabled ?? true, workspace_id: existing?.workspace_id || null,
      strategy_package: existing?.strategy_package ? Object.fromEntries(Object.entries(existing.strategy_package).filter(([key]) => key !== "installed_hash")) : {},
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
  const draft = state.memory.draft;
  if (state.view !== "memory" || !state.memory.composing || !draft) return;
  const sources = state.memory.collectedSources || [];
  const mode = draft.contentMode || "complete";
  const payloadSources = memorySourcesForApi(sources, state.memory.activeSessionId);
  if (!payloadSources.length) { setStatus("请先添加至少一个记忆来源", true); return; }
  const requestId = ++memoryCompressionRequestSequence;
  const viewId = memoryViewRequestSequence;
  const { selectedId, category, activeSessionId } = state.memory;
  // 输入处理器以新对象保存草稿/来源；任何后续编辑都会使本次快照失效。
  const isCurrent = () => requestId === memoryCompressionRequestSequence
    && viewId === memoryViewRequestSequence && state.view === "memory"
    && state.memory.composing && state.memory.draft === draft
    && state.memory.collectedSources === sources && state.memory.selectedId === selectedId
    && state.memory.category === category && state.memory.activeSessionId === activeSessionId;
  try {
    const result = await api("/desktop/api/memory/summarize", { method: "POST", body: JSON.stringify({ selection: { sources: payloadSources }, mode }) });
    if (!isCurrent()) return;
    const segments = result.segments || [];
    state.memory = { ...state.memory, draft: { ...draft, content: result.content || "", segments, contentMode: mode, contentDirty: false } };
    render();
  } catch (error) { if (isCurrent()) setStatus(error.message, true); }
}

// 后端 MemorySource 仅接受的字段（前端展示字段 contextTitle/count/text 不发送）。
const MEMORY_SOURCE_FIELDS = new Set(["type", "context_id", "message_ids", "parts", "text"]);

function memorySourceForApi(source) {
  return Object.fromEntries(Object.entries(source).filter(([key]) => MEMORY_SOURCE_FIELDS.has(key)));
}

async function saveStrategy() {
  const draft = state.memory.draft;
  if (!draft || state.memory.strategySaving) return;
  const body = { title: draft.title, strategy: draft.strategy, enabled: draft.enabled, workspace_id: draft.workspace_id || null };
  body.strategy_package = { schema: "focus.strategy/v1", version: "1.0.0", description: "", ...draft.strategy_package,
    id: draft.strategy_package?.id?.trim() || `local/${crypto.randomUUID().replaceAll("-", "")}`, name: draft.title };
  const savingDraft = { ...draft, strategy_package: body.strategy_package };
  state.memory.draft = savingDraft;
  state.memory.strategySaving = true;
  renderMemory();
  if (!draft.memory_id) body.category = "evolution_strategy";
  try {
    await api(`/desktop/api/memory${draft.memory_id ? "/" + draft.memory_id : ""}`, { method: draft.memory_id ? "PUT" : "POST", body: JSON.stringify(body) });
    if (state.memory.draft === savingDraft) state.memory = { ...state.memory, composing: false, draft: null };
    await hydrateMemory();
    render();
  } catch (error) { setStatus(error.message, true); }
  finally { state.memory.strategySaving = false; if (state.view === "memory") renderMemory(); }
}

let strategyImportRequestSequence = 0;
let strategyAuthoringRequestSequence = 0;

function openStrategyImport() {
  strategyImportRequestSequence++;
  strategyAuthoringRequestSequence++;
  state.memory = { ...state.memory, category: "evolution_strategy", composing: false, draft: null,
    strategyImport: { markdown: "", workspace_id: state.patrolWorkspace?.workspace_id || null, enabled: true, preview: null, error: "", busy: false } };
  renderMemory();
}

function syncStrategyImportField(field, value) {
  const draft = state.memory.strategyImport;
  if (!draft || draft.busy) return;
  strategyImportRequestSequence++;
  state.memory.strategyImport = { ...draft, [field]: field === "workspace_id" ? value || null : value, preview: null, error: "" };
  document.querySelector("[data-strategy-import-preview]")?.remove?.();
  const error = document.querySelector("[data-strategy-import-error]");
  if (error) error.textContent = "内容或设置已更改，请预览后安装。";
}

async function readStrategyFile(file) {
  if (!file || !state.memory.strategyImport) return;
  if (file.size > 160000) return setStatus("策略文件不能超过 160 KB", true);
  const draft = state.memory.strategyImport;
  try {
    const markdown = await file.text();
    if (state.memory.strategyImport !== draft) return;
    syncStrategyImportField("markdown", markdown);
    renderMemory();
  } catch (error) { setStatus(`读取策略失败：${error.message}`, true); }
}

async function previewStrategyImport() {
  const draft = state.memory.strategyImport;
  if (!draft || draft.busy) return;
  const requestId = ++strategyImportRequestSequence;
  state.memory.strategyImport = { ...draft, busy: true, error: "", preview: null };
  renderMemory();
  try {
    const preview = await api("/desktop/api/strategies/preview", { method: "POST", body: JSON.stringify({ markdown: draft.markdown, workspace_id: draft.workspace_id, enabled: draft.enabled }) });
    if (requestId !== strategyImportRequestSequence || !state.memory.strategyImport) return;
    state.memory.strategyImport = { ...draft, preview, busy: false, error: "" };
  } catch (error) {
    if (requestId !== strategyImportRequestSequence || !state.memory.strategyImport) return;
    state.memory.strategyImport = { ...draft, preview: null, busy: false, error: error.message };
  }
  if (state.view === "memory") renderMemory();
}

async function installStrategyImport() {
  const draft = state.memory.strategyImport;
  if (!draft?.preview || draft.busy) return;
  const requestId = ++strategyImportRequestSequence;
  state.memory.strategyImport = { ...draft, busy: true, error: "" };
  renderMemory();
  const existing = draft.preview.existing;
  try {
    const result = await api("/desktop/api/strategies/install", { method: "POST", body: JSON.stringify({
      markdown: draft.markdown, workspace_id: draft.workspace_id, enabled: draft.enabled,
      replace_memory_id: existing?.memory_id || null, expected_hash: existing?.installation_hash || null,
    }) });
    if (requestId !== strategyImportRequestSequence) { await hydrateMemory(); return; }
    state.memory = { ...state.memory, strategyImport: null, selectedId: result.memory.memory_id, category: "evolution_strategy" };
    setStatus(result.status === "already_installed" ? "已安装相同策略，保留现有启用设置" : "策略已保存，将从后续新轮次生效");
    await hydrateMemory();
  } catch (error) {
    if (requestId === strategyImportRequestSequence && state.memory.strategyImport) state.memory.strategyImport = { ...draft, busy: false, preview: null, error: error.message };
  }
  if (state.view === "memory") renderMemory();
}

function forkImportedStrategy() {
  const draft = state.memory.strategyImport;
  if (!draft?.preview || draft.busy) return;
  const { metadata, strategy } = draft.preview;
  openMemoryCompose(false);
  state.memory.draft = { ...state.memory.draft, category: "evolution_strategy", title: metadata.name, strategy,
    strategy_package: { ...metadata, id: `local/${crypto.randomUUID().replaceAll("-", "")}` },
    workspace_id: draft.workspace_id, enabled: draft.enabled };
  renderMemory();
}

async function exportStrategy(memoryId) {
  try {
    const data = await api(`/desktop/api/strategies/${encodeURIComponent(memoryId)}/export`);
    const url = URL.createObjectURL(new Blob([data.markdown], { type: "text/markdown;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url; link.download = data.filename; document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { setStatus(error.message, true); }
}

async function generateStrategyDraft() {
  const draft = state.memory.draft;
  if (!draft || state.memory.strategyAuthoring?.busy) return;
  const sources = memorySourcesForApi(state.memory.collectedSources || [], state.memory.activeSessionId);
  const text = draft.strategy_source || draft.content || "";
  if (!sources.length && !text.trim()) return setStatus("请先填写经验或选择工作材料", true);
  const requestId = ++strategyAuthoringRequestSequence;
  state.memory.strategyAuthoring = { busy: true };
  renderMemory();
  try {
    const result = await api("/desktop/api/strategies/draft", { method: "POST", body: JSON.stringify({ selection: { sources: sources.length ? sources : [{ type: "manual", text }] } }) });
    if (requestId !== strategyAuthoringRequestSequence || state.memory.draft !== draft) return;
    if (draft.category !== "evolution_strategy") openMemoryCompose(false);
    state.memory.category = "evolution_strategy";
    state.memory.draft = { ...state.memory.draft, category: "evolution_strategy", title: result.title, strategy: result.strategy, rehearsal_result: null };
    setStatus("策略草稿已生成，请检查后保存");
  } catch (error) { if (requestId === strategyAuthoringRequestSequence) setStatus(error.message, true); }
  finally { if (requestId === strategyAuthoringRequestSequence) state.memory.strategyAuthoring = null; if (state.view === "memory") renderMemory(); }
}

async function rehearseStrategy() {
  const draft = state.memory.draft;
  if (!draft || state.memory.strategyAuthoring?.busy) return;
  const requestId = ++strategyAuthoringRequestSequence;
  state.memory.strategyAuthoring = { busy: true };
  renderMemory();
  try {
    const result = await api("/desktop/api/strategies/rehearse", { method: "POST", body: JSON.stringify({ title: draft.title, strategy: draft.strategy, scenarios: draft.rehearsal_scenarios || {} }) });
    if (requestId === strategyAuthoringRequestSequence && state.memory.draft === draft) state.memory.draft = { ...draft, rehearsal_result: result };
    if (requestId === strategyAuthoringRequestSequence) state.memory.strategyAuthoring = null;
  } catch (error) { if (requestId === strategyAuthoringRequestSequence) state.memory.strategyAuthoring = { busy: false, error: error.message }; }
  if (state.view === "memory") renderMemory();
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
    if (draft.memory_id) { delete body.source; delete body.source_kind; }
    await api(`/desktop/api/memory${draft.memory_id ? "/" + draft.memory_id : ""}`, { method: draft.memory_id ? "PUT" : "POST", body: JSON.stringify(body) });
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
    preferenceStorage.setItem(MAP_VIEW_PREFERENCES_KEY, JSON.stringify({
      mode: state.mapViewMode,
      workspaceIds: [...state.mapExpandedWorkspaceIds],
      contextIds: [...state.mapExpandedContextIds],
    }));
  } catch { /* 本机禁用持久化时仍保持当前会话状态 */ }
}

function setsEqual(left, right) {
  return left.size === right.size && [...left].every(value => right.has(value));
}

function prepareMapTreeModel(activeTasks) {
  const model = mapCollapsibleView.buildModel(activeTasks, state.contextTrees);
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

function renderMapTree(activeTasks) {
  const model = prepareMapTreeModel(activeTasks);
  return mapCollapsibleView.render(model, {
    tasks: activeTasks,
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
  const presented = presentTaskStatus(task);
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
      ${task.active_run && task.latest_result ? `<span class="task-last-result">上次执行：${escapeHtml(presentRunStatus(task.latest_result.status).label)}</span>` : ""}
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

/*
 * 输入仅为 active 任务（renderMap 已过滤）及其 Context tree，输出按工作区和根 Context 分组的卡片。
 * 工作区与根 Context 组都由任务派生，因此全图不会出现「0 个 Context」的空工作区，也不需要空态分支。
 */
function renderMapGroups(activeTasks) {
  const ordered = contextEditor.orderTasksByTree(activeTasks, state.contextTrees);
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
    const contextCount = [...workspace.roots.values()].reduce((sum, tasks) => sum + tasks.length, 0);
    return `<section class="map-workspace-group" data-workspace-id="${escapeHtml(workspaceId)}">
      <header><div><span class="workspace-kicker">WORKSPACE</span><h2>${escapeHtml(workspace.name)}</h2></div><span class="ui-badge">${contextCount} 个 Context</span></header>
      <div class="map-root-groups">${[...workspace.roots.entries()].map(([rootId, tasks]) => {
        const root = tasks.find(task => task.task_id === rootId) || tasks[0];
        const headerLabel = tasks.length === 1 ? "仅根 Context" : `${tasks.length - 1} 个派生`;
        return `<section class="map-root-group"><header><strong>${escapeHtml(root?.title || rootId)}</strong><span>${escapeHtml(headerLabel)}</span></header><div class="task-grid">${tasks.map(task => taskCardMarkup(task)).join("")}</div></section>`;
      }).join("")}</div>
    </section>`;
  }).join("");
}

function createRunPreviews(onChange) {
  return runStreams && window.FocusContextRunPreviews?.create({
    streams: runStreams,
    loadRun: (runId, signal) => api(`/desktop/api/runs/${encodeURIComponent(runId)}`, { signal }),
    onChange,
  });
}

function stopMapLive() {
  const previous = mapLive;
  mapLive = null;
  if (previous?.frame != null && typeof cancelAnimationFrame === "function") cancelAnimationFrame(previous.frame);
  previous?.unsubscribe();
  previous?.connection.stop();
  mapRunPreviews?.stop();
}

function scheduleMapGraphPatch() {
  const owner = mapLive;
  if (!owner || state.view !== "map" || state.mapViewMode !== "graph" || owner.frame !== null) return;
  owner.frame = requestAnimationFrame(() => {
    owner.frame = null;
    if (mapLive !== owner || state.view !== "map" || state.mapViewMode !== "graph") return;
    patchMapGraph();
    const projection = owner.store.get().projection;
    if (!projection) return;
    const roots = JSON.stringify([
      Object.values(projection.contexts).map(item => [item.entity_id, item.state.current_revision_id]),
      projection.portfolio?.revision,
      Object.values(projection.lineage).map(item => [item.entity_id, item.revision]),
    ]);
    if (roots === owner.roots) return;
    const previous = owner.roots;
    owner.roots = roots;
    if (previous !== null) void hydrateMapPortfolio(owner.workspaceId, true);
  });
}

function ensureMapLive() {
  const loopId = mapPortfolio.manifest?.loop_id, workspaceId = mapPortfolio.workspaceId;
  if (!loopId || !window.FocusLoopLiveStore || !window.FocusLoopLiveConnection) return;
  if (mapLive?.loopId === loopId && mapLive.workspaceId === workspaceId) return;
  stopMapLive();
  const store = window.FocusLoopLiveStore.create();
  const connection = window.FocusLoopLiveConnection.create({ api: loopApi, store });
  const owner = { loopId, workspaceId, store, connection, roots: null, frame: null, unsubscribe: () => {} };
  mapLive = owner;
  owner.unsubscribe = store.subscribe(() => {
    if (mapLive === owner) scheduleMapGraphPatch();
  });
  void connection.start(loopId).catch(error => {
    if (mapLive === owner) { mapPortfolio.error = error.message; patchMapGraph(); }
  });
}

async function hydrateMapPortfolio(workspaceId, force = false) {
  if (!force && mapPortfolio.workspaceId === workspaceId) return;
  const owner = ++mapPortfolioSequence;
  const sameWorkspace = mapPortfolio.workspaceId === workspaceId;
  if (!sameWorkspace) {
    stopMapLive();
    mapContextInspector?.dispose(); mapContextInspector = null; mapRelatedOnly = false;
    mapPortfolio = { workspaceId, loading: false, snapshot: null, manifest: null, error: null };
  }
  if (!workspaceId) return;
  mapPortfolio = { ...mapPortfolio, loading: true, error: null };
  patchMapGraph();
  try {
    const binding = await loopApi.workspacePatrol(workspaceId);
    const [snapshot, manifest] = binding ? await Promise.all([loopApi.committedLineage(binding.loop_id), loopApi.console(binding.loop_id)]) : [null, null];
    if (owner !== mapPortfolioSequence) return;
    if (snapshot && snapshot.complete !== true) throw new Error("来源关系尚未完整返回");
    mapPortfolio = { workspaceId, loading: false, snapshot, manifest, error: null };
  } catch (error) {
    if (owner !== mapPortfolioSequence) return;
    mapPortfolio = { ...mapPortfolio, loading: false, error: error.message };
  }
  if (state.view === "map") { renderMap(); mapContextInspector?.refresh(); }
}

function mapGraphManifest() {
  if (!mapPortfolio.snapshot) return { nodes: [], edges: [] };
  const contexts = Object.fromEntries((mapPortfolio.manifest?.nodes || []).map(node => [node.context_id, { entity_id: node.context_id, state: node }]));
  const live = mapLive?.store.get().projection;
  return window.FocusWorkspacePatrolView.lineageManifest(mapPortfolio.snapshot, live ? { ...live, contexts: { ...contexts, ...live.contexts } } : { contexts });
}

function patchMapGraph() {
  const surface = app.querySelector(".map-portfolio-surface");
  if (state.view !== "map" || state.mapViewMode !== "graph" || !surface || surface.dataset.workspaceId !== (mapPortfolio.workspaceId || "")) return;
  const raw = mapGraphManifest(), selection = mapContextInspector?.selection();
  mapRunPreviews?.sync({ workspaceId: mapPortfolio.workspaceId, loopId: mapPortfolio.manifest?.loop_id, nodes: raw.nodes });
  const data = mapRunPreviews?.decorate(raw) || raw;
  if (selection && mapPortfolio.snapshot && !data.nodes.some(node => node.context_id === selection.contextId)) mapContextInspector.close();
  const selected = mapContextInspector?.selection();
  const inspecting = !state.selectionMode && !state.soldierArmed;
  const host = surface.querySelector("[data-map-graph]");
  const options = { presentation: "workbench", selectedRevisionId: selected?.revisionId, relatedOnly: inspecting && mapRelatedOnly };
  const selectedId = inspecting ? selected?.contextId : null;
  const hadMap = host.querySelector(".portfolio-map");
  if (!window.FocusPortfolioMapView.reconcile(host, data, selectedId, [], options)) host.innerHTML = window.FocusPortfolioMapView.render(data, selectedId, [], options);
  window.FocusPortfolioMapView.bind(host);
  if (mapRunPreviews) window.FocusPortfolioMapView.patchPreviews(host, mapRunPreviews.get());
  if (!hadMap && host.querySelector(".portfolio-map")) requestAnimationFrame(() => { if (host.isConnected) host.querySelector('[data-portfolio-zoom="fit"]')?.click(); });
  for (const node of host.querySelectorAll(".portfolio-context-node")) {
    node.classList.toggle("is-search-muted", !!state.mapQuery?.trim() && !`${node.dataset.contextId} ${node.textContent}`.toLowerCase().includes(state.mapQuery.trim().toLowerCase()));
    node.classList.toggle("is-batch-selected", state.selectionMode && state.selectedContextIds.has(node.dataset.contextId));
    if (state.selectionMode) node.setAttribute("aria-pressed", String(state.selectedContextIds.has(node.dataset.contextId)));
  }
  const status = surface.querySelector("[data-map-status]");
  status.textContent = mapPortfolio.error ? `${mapPortfolio.snapshot ? "未更新，保留上次完整关系。" : "读取失败。"}${mapPortfolio.error}` : mapPortfolio.loading ? "正在读取已提交关系…" : !mapPortfolio.snapshot ? "此工作区尚无已提交 Patrol 关系，可使用折叠/卡片浏览已有任务。" : "";
  const connection = mapLive?.store.get().connection;
  if (!status.textContent && ["unavailable", "reconnecting", "resyncing"].includes(connection?.status)) status.textContent = "运行状态连接中断，正在保留上次状态；可刷新重试。";
  status.hidden = !status.textContent;
  const related = surface.querySelector('[data-action="map-related"]');
  related.disabled = !selected || !inspecting;
  related.textContent = mapRelatedOnly && inspecting ? "恢复全部关系" : "仅看所选关联";
  related.setAttribute("aria-pressed", String(mapRelatedOnly && inspecting));
  const refresh = surface.querySelector('[data-action="refresh-map-portfolio"]');
  refresh.disabled = mapPortfolio.loading || !mapPortfolio.workspaceId;
  refresh.textContent = mapPortfolio.error ? "重试" : "刷新";
}

async function inspectMapContext(contextId, trigger, revisionId) {
  const node = mapGraphManifest().nodes.find(item => item.context_id === contextId);
  if (!mapPortfolio.manifest?.loop_id || !mapContextInspector) return;
  await mapContextInspector.select(mapPortfolio.manifest.loop_id, contextId, node, trigger, revisionId || node?.current_revision_id);
}

function inspectMapRelationship(identity, trigger) {
  const workspaceId = mapPortfolio.workspaceId;
  const panel = document.createElement("dialog");
  panel.className = "map-relationship-dialog";
  panel.setAttribute("aria-label", "已提交来源关系");
  panel.innerHTML = `<header><h2>已提交来源关系</h2><form method="dialog"><button aria-label="关闭关系检查">×</button></form></header>${window.FocusPortfolioMapView.relationship(mapGraphManifest(), identity)}`;
  app.append(panel);
  panel.addEventListener("close", () => panel.remove(), { once: true });
  panel.addEventListener("click", async event => {
    const endpoint = event.target.closest("[data-patrol-edge-context]");
    if (!endpoint) return;
    event.stopPropagation(); panel.close();
    if (state.view === "map" && workspaceId === mapPortfolio.workspaceId) await inspectMapContext(endpoint.dataset.patrolEdgeContext, trigger, endpoint.dataset.revisionId);
  });
  void window.FocusSurfaceTransition.visible(panel, true, { modal: true, source: trigger });
}

function renderMap(focusKey = null) {
  if (String(state.mapQuery || "").trim()) {
    stopMapLive();
    mapContextInspector?.dispose(); mapContextInspector = null; mapRelatedOnly = false;
    return renderHistorySearch();
  }
  const sel = state.selectedContextIds.size;
  const treeMode = state.mapViewMode === "tree";
  const query = String(state.mapQuery || "").trim().toLowerCase();
  const activeTasks = state.tasks.filter(task => sessionLifecycle(task) === "active" && (!query || `${task.title} ${task.task_id} ${task.workspace_name}`.toLowerCase().includes(query)));
  const graphMode = state.mapViewMode === "graph";
  if (!graphMode) stopMapLive();
  const workspaceId = state.patrolWorkspace?.workspace_id || activeTask()?.workspace_id;
  if (graphMode && workspaceId !== mapPortfolio.workspaceId) void hydrateMapPortfolio(workspaceId);
  const presentationControls = `<div class="map-presentation-controls">
    <div class="map-presentation-switch" role="group" aria-label="全图展示方式">
      <button type="button" data-action="set-map-view" data-map-view="graph" aria-pressed="${graphMode}">Context 关系</button>
      <button type="button" data-action="set-map-view" data-map-view="tree" aria-pressed="${treeMode}">折叠视图</button>
      <button type="button" data-action="set-map-view" data-map-view="cards" aria-pressed="${state.mapViewMode === "cards"}">卡片与生命周期</button>
    </div>
    ${treeMode ? '<button type="button" class="text-button map-collapse-all" data-action="collapse-map-tree">全部折叠</button>' : ""}
  </div>`;
  const batchControls = state.selectionMode
    ? `<span class="map-selection-info">已选 ${sel} 个会话</span>
      <button class="text-button" data-action="batch-delete-selected" ${sel ? "" : "disabled"}>删除选中</button>
      <button class="text-button" data-action="batch-cascade-delete-selected" ${sel ? "" : "disabled"}>级联删除</button>
      <button class="text-button" data-action="toggle-selection-mode">取消</button>`
    : `<button class="text-button" data-action="toggle-selection-mode">批量删除</button>`;
  const toolbar = `<div class="map-toolbar glass-surface">${presentationControls}<button class="soldier-source" draggable="true" aria-pressed="${state.soldierArmed}" data-action="arm-soldier">${state.soldierArmed ? "已装备小兵 · 选择任务" : "装备小兵"}</button>${batchControls}</div>`;
  const mounted = graphMode && app.querySelector(".map-portfolio-surface")?.dataset.workspaceId === (workspaceId || "");
  if (!mounted) {
    mapContextInspector?.dispose(); mapContextInspector = null; mapRelatedOnly = false;
    app.innerHTML = `<section class="map-view">
      <header class="task-page-heading"><div><h1>全图</h1><p>沿精确来源关系检查工作。层次与卡片浏览保留所有工作区操作。</p></div><button data-action="show-patrol">回到 Patrol</button></header>
      ${toolbar}<label class="page-search">搜索历史 <input type="search" data-page-search="map" maxlength="200" value="${escapeHtml(state.mapQuery || "")}" placeholder="消息正文、任务或文件名…"></label>
      ${graphMode ? `<section class="map-portfolio-surface" data-workspace-id="${escapeHtml(workspaceId || "")}"><header><span>当前工作区 · 已提交来源</span><div><button data-action="map-related" disabled>仅看所选关联</button><button data-action="refresh-map-portfolio">刷新</button></div></header><p data-map-status role="status" hidden></p><div data-map-graph></div><footer class="workbench-legend"><span>已提交来源关系</span><span>所选 Context 的直接关联</span><span>Run 状态以节点为准</span><small>连线可查看来源与目标版本</small></footer></section><aside data-patrol-context class="patrol-context-drawer" aria-label="只读 Context 检查" hidden><div data-patrol-context-content></div></aside>` : `<div class="${treeMode ? "map-tree-host" : "map-groups"}">${activeTasks.length ? (treeMode ? renderMapTree(activeTasks) : renderMapGroups(activeTasks)) : '<p>暂无匹配的活动任务</p>'}</div>`}
    </section>`;
    if (graphMode) {
      mapContextInspector = window.FocusContextInspector.create({ api: loopApi, onOpenTask: switchTask, getManifest: mapGraphManifest, onSelectionChange: selected => { if (!selected) mapRelatedOnly = false; patchMapGraph(); } });
      mapContextInspector.mount(app.querySelector("[data-patrol-context]"));
      app.querySelector(".map-portfolio-surface").addEventListener("portfolio-clear-selection", () => { if (!state.selectionMode && !state.soldierArmed) mapContextInspector?.close(); });
    }
  } else {
    const currentToolbar = app.querySelector(".map-toolbar");
    if (currentToolbar.outerHTML !== toolbar) currentToolbar.outerHTML = toolbar;
  }
  if (graphMode) { ensureMapLive(); patchMapGraph(); }
  if (focusKey) {
    const target = [...app.querySelectorAll("[role='treeitem'][data-tree-key]")]
      .find(item => item.dataset.treeKey === focusKey)
      || [...app.querySelectorAll(".portfolio-context-node")].find(item => `context:${item.dataset.contextId}` === focusKey)
      || app.querySelector("[role='treeitem'][tabindex='0']");
    target?.focus();
  }
}

let historySearchTimer = null;

function historyHighlighted(text, query = state.mapQuery || "") {
  text = String(text || "");
  query = String(query).trim();
  if (!query) return escapeHtml(text);
  const index = text.toLowerCase().indexOf(query.toLowerCase());
  if (index < 0) return escapeHtml(text);
  return `${escapeHtml(text.slice(0, index))}<mark>${escapeHtml(text.slice(index, index + query.length))}</mark>${escapeHtml(text.slice(index + query.length))}`;
}

function renderHistorySearch() {
  const search = state.historySearch;
  app.innerHTML = `<section class="map-view history-search-view">
    <header class="task-page-heading"><div><h1>历史查找</h1><p>搜索消息正文、任务和文件名，包含已归档任务。</p></div><button data-action="clear-history-search">返回全图</button></header>
    <label class="page-search">搜索历史 <input type="search" data-page-search="map" maxlength="200" value="${escapeHtml(state.mapQuery || "")}" placeholder="消息正文、任务或文件名…"></label>
    <div role="status">${search.loading ? "正在搜索历史…" : search.error ? escapeHtml(search.error) : search.results.length ? `已显示 ${search.results.length} 条匹配${search.hasMore ? "，还有更多结果" : ""}` : "暂无匹配的历史记录"}${search.unavailable ? `<p>有 ${search.unavailable} 个任务正文暂时无法读取，可重试搜索。</p>` : ""}</div>
    ${search.error || search.unavailable ? '<button data-action="retry-history-search">重试搜索</button>' : ""}
    <div class="history-results">${search.results.map((hit, index) => `<button class="history-result" data-action="open-history-result" data-result-index="${index}"><span><strong>${escapeHtml(hit.title)}</strong><small>${escapeHtml(hit.workspace_name)} · ${hit.lifecycle === "archived" ? "已归档 · 只读" : "活动任务"} · ${{ task: "任务信息", message: "消息正文", file: "文件" }[hit.kind] || "历史"}</small></span><span class="history-snippet">${historyHighlighted(hit.snippet)}</span></button>`).join("")}</div>
    ${search.hasMore ? `<button data-action="more-history-results" ${search.loading ? "disabled" : ""}>加载更多结果</button>` : ""}
  </section>`;
}

function queueHistorySearch(query) {
  clearTimeout(historySearchTimer);
  const search = state.historySearch;
  search.sequence += 1;
  Object.assign(search, { query: query.trim(), results: [], loading: !!query.trim(), error: "", hasMore: false, unavailable: 0 });
  if (query.trim()) historySearchTimer = setTimeout(() => void loadHistorySearch(), 250);
}

async function loadHistorySearch(append = false) {
  const search = state.historySearch;
  const query = String(state.mapQuery || "").trim();
  if (!query) return;
  const sequence = ++search.sequence;
  search.loading = true; search.error = "";
  if (state.view === "map") refreshHistorySearch();
  try {
    const response = await api(`/desktop/api/history/search?q=${encodeURIComponent(query)}&offset=${append ? search.results.length : 0}`);
    if (sequence !== search.sequence || query !== String(state.mapQuery || "").trim()) return;
    search.results = append ? [...search.results, ...response.results] : response.results;
    search.hasMore = response.has_more;
    search.unavailable = response.unavailable_context_ids.length;
  } catch (error) {
    if (sequence !== search.sequence) return;
    search.error = `历史搜索失败：${error.message}`;
  } finally {
    if (sequence === search.sequence) {
      search.loading = false;
      if (state.view === "map") refreshHistorySearch();
    }
  }
}

function refreshHistorySearch() {
  const input = app.querySelector('[data-page-search="map"]');
  const focused = document.activeElement === input;
  const position = input?.selectionStart;
  const scrollTop = app.scrollTop;
  renderHistorySearch();
  app.scrollTop = scrollTop;
  if (focused) {
    const next = app.querySelector('[data-page-search="map"]');
    next?.focus();
    next?.setSelectionRange?.(position, position);
  }
}

async function openHistory(contextId, targetKey = "", messageId = null) {
  const reader = state.historyReader;
  const sequence = ++reader.sequence;
  if (state.view === "focus") persistFocusState();
  cancelPendingViewRequests();
  ++taskSwitchSequence;
  Object.assign(reader, { contextId, targetKey, messageId, detail: null, loading: true, error: "", start: 0 });
  state.inspector.open = false;
  state.view = "history";
  app.scrollTop = 0;
  if (settingsDialog?.open) void window.FocusSurfaceTransition.visible(settingsDialog, false, { modal: true });
  render();
  try {
    const detail = await api(`/desktop/api/history/${encodeURIComponent(contextId)}`);
    if (sequence !== reader.sequence || state.view !== "history") return;
    reader.detail = detail;
    const targetIndex = messageId ? detail.entries.findIndex(entry => entry.message_id === messageId) : detail.entries.findIndex(entry => entry.key === targetKey);
    if (targetIndex >= 0) {
      reader.targetKey = detail.entries[targetIndex].key;
      reader.start = Math.max(0, targetIndex - 20);
    } else if (targetKey && !detail.materials.some(item => item.key === targetKey)) {
      reader.error = "原命中消息已发生变化，正在显示当前保存的历史。";
    }
  } catch (error) {
    if (sequence === reader.sequence) reader.error = `读取历史失败：${error.message}`;
  } finally {
    if (sequence === reader.sequence && state.view === "history") {
      reader.loading = false;
      render();
      const target = app.querySelector(".history-target");
      target?.scrollIntoView?.({ block: "center" });
      target?.focus?.({ preventScroll: true });
    }
  }
}

function renderHistoryReader() {
  const reader = state.historyReader, detail = reader.detail;
  const entries = detail?.entries || [];
  app.innerHTML = `<section class="history-reader">
    <header class="task-page-heading"><div><h1>${escapeHtml(detail?.title || "历史记录")}</h1><p>${detail?.lifecycle === "archived" ? "已归档 · " : ""}只读查看${detail ? ` · ${escapeHtml(detail.workspace_name)}` : ""}</p></div><div><button data-action="back-history-search">返回历史查找</button>${detail?.lifecycle === "active" ? `<button data-action="history-continue" data-context-id="${escapeHtml(detail.context_id)}">继续任务</button>` : ""}</div></header>
    ${reader.loading ? '<p role="status">正在读取历史…</p>' : ""}
    ${reader.error ? `<p role="status">${escapeHtml(reader.error)}</p>${!detail ? '<button data-action="retry-history-reader">重试读取</button>' : ""}` : ""}
    ${detail?.materials.length ? `<section class="history-files" aria-label="相关输入与产物"><h2>相关输入与产物</h2>${detail.materials.map(item => `<div class="history-file ${reader.targetKey === item.key ? "history-target" : ""}" tabindex="-1"><small>${({ input: "输入文件", artifact: "产物", material: "材料" })[item.kind] || "文件"}</small><strong>${historyHighlighted(item.path)}</strong>${item.note ? `<p>${historyHighlighted(item.note)}</p>` : ""}</div>`).join("")}</section>` : ""}
    ${reader.start ? '<button data-action="history-earlier">查看更早消息</button>' : ""}
    <div class="history-messages">${entries.slice(reader.start, reader.start + 80).map(entry => `<article class="history-message ${reader.targetKey === entry.key ? "history-target" : ""}" tabindex="-1"><header>${({ human: "你", user: "你", ai: "助手", assistant: "助手", tool: "工具", system: "系统" })[entry.role] || "消息"}${entry.retained_source ? " · 压缩前原文" : ""}</header><div>${historyHighlighted(entry.text)}</div></article>`).join("")}${detail && !entries.length ? "<p>该任务暂无已保存的消息。</p>" : ""}</div>
    ${reader.start + 80 < entries.length ? '<button data-action="history-later">查看后续消息</button>' : ""}
  </section>`;
}

async function hydrateContextTrees(workspaceIds = null) {
  if (workspaceIds == null) {
    if (state.view === "map" && state.mapViewMode !== "graph") {
      workspaceIds = [...new Set(state.tasks.filter(task => sessionLifecycle(task) === "active").map(task => task.workspace_id))];
    } else if (["focus", "draft", "context"].includes(state.view) && activeTask()) {
      workspaceIds = [activeTask().workspace_id];
    } else return;
  }
  const requestId = ++contextTreeRequestSequence;
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
  return new Map([...document.querySelectorAll(".context-message-editor[data-context-ui-key]")].map(row => [row.dataset.contextUiKey, row.getBoundingClientRect().top]));
}

function animateContextReflow(previous) {
  if (!previous || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
  document.querySelectorAll(".context-message-editor[data-context-ui-key]").forEach(row => {
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
  const rows = [...list.querySelectorAll(".context-message-editor[data-context-ui-key]")];
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
  if (!rows.length && !list.querySelector(".context-empty-drop")) list.insertAdjacentHTML("beforeend", contextEditor.renderMessages([], []));
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
  const rows = [...list.querySelectorAll(".context-message-editor[data-context-ui-key]")];
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
  const rows = new Map([...document.querySelectorAll(".context-message-editor[data-context-ui-key]")].map(row => [row.dataset.contextUiKey, row]));
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

async function deleteContextMessage(uiKey) {
  const index = state.contextDraft?.uiKeys.indexOf(uiKey) ?? -1;
  if (index < 0) return;
  const row = document.querySelector(`.context-message-editor[data-context-ui-key="${uiKey}"]`);
  if (!row) return;
  const draft = syncContextDraft();
  const panel = document.querySelector(".context-definition-panel");
  const scrollTop = panel?.scrollTop || 0;
  const [message] = draft.messages.splice(index, 1);
  const [key] = draft.uiKeys.splice(index, 1);
  if (draft.expandedKey === key) draft.expandedKey = null;
  draft.undo = { message, key, index };
  // Retire the row synchronously; only its visual shell survives the exit animation.
  row.setAttribute("inert", "");
  row.setAttribute("aria-hidden", "true");
  row.removeAttribute("data-context-ui-key");
  row.removeAttribute("data-context-message-index");
  row.querySelectorAll("button, textarea").forEach(input => { input.disabled = true; });
  row.querySelector("[data-context-message-json]")?.removeAttribute("data-context-message-json");
  updateContextMessageIndices();
  showContextUndo();
  try {
    if (!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
      const animation = row.animate([{ opacity: 1, transform: "scale(1)", maxHeight: `${row.offsetHeight}px` }, { opacity: 0, transform: "scale(.98)", maxHeight: "0px" }], { duration: 150, easing: "ease-in", fill: "forwards" });
      await Promise.race([animation.finished.catch(() => {}), new Promise(resolve => setTimeout(resolve, 180))]);
    }
  } finally {
    const previous = contextListPositions();
    row.remove();
    if (state.contextDraft === draft && panel?.isConnected) {
      panel.scrollTop = Math.min(scrollTop, Math.max(0, panel.scrollHeight - panel.clientHeight));
      animateContextReflow(previous);
    }
  }
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
  const currentWorkbench = document.querySelector('#patrolWorkbench');
  if (curator && currentWorkbench?.dataset?.draftId) window.FocusPatrolWorkbench?.leave(currentWorkbench.dataset.draftId);
  if (!curator && currentWorkbench?.dataset?.draftId === draft.draft_id && window.FocusPatrolWorkbench) {
    window.FocusPatrolWorkbench.get(draft.draft_id).rebind({draft,api,onStale:updateTokenState,onPreview:updateTokenState,onReload:()=>renderDraft(),onEquipment:()=>{const panel=document.querySelector(".draft-equipment");if(panel)panel.open=true;}});
    const equipment = document.querySelector('.draft-step[data-step="equipment"] .draft-step-body');
    const signature = JSON.stringify([draft.equipment.model_name,draft.equipment.skills,draft.equipment.permissions,draft.equipment.access_mode,state.equipment.tools_status]);
    if (equipment && equipment.dataset.signature !== signature) { equipment.innerHTML = renderEquipment(draft); equipment.dataset.signature = signature; }
    updateTokenState(); return;
  }
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
  if (!curator && window.FocusPatrolWorkbench && draft.authoring_document) {
    window.FocusPatrolWorkbench.mount(document.querySelector('#patrolWorkbench'), {
      draft, api, onStale:updateTokenState, onPreview:updateTokenState, onReload:()=>renderDraft(),onEquipment:()=>{const panel=document.querySelector(".draft-equipment");if(panel)panel.open=true;},
    });
  }
  updateTokenState();
}

function renderStandardDraftSteps(draft) {
  return `<div id="patrolWorkbench"></div><details class="draft-equipment draft-step" data-step="equipment"><summary>模型与权限 · ${escapeHtml(draft.equipment?.model_name || "默认模型")}</summary><div class="draft-step-body">${renderEquipment(draft)}</div></details>`;
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

const sourceMeta = {
  builtin: { label: "内置", className: "tool-source builtin" },
  custom: { label: "自定义", className: "tool-source custom" },
  mcp: { label: "MCP", className: "tool-source mcp" },
  plugin: { label: "插件", className: "tool-source plugin" },
};

function renderEquipmentTools() {
  if (state.equipment.tools_status === "loading") return '<p role="status">正在加载工具目录…</p>';
  if (state.equipment.tools_status === "error") return `<p role="status">工具目录加载失败：${escapeHtml(state.equipment.tools_error)} <button data-action="retry-equipment">重试</button></p>`;
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
  return `<div class="equipment-tools"><span class="tiny muted">工具目录（按来源，执行时校验）</span>${blocks}</div>`;
}

let equipmentLoading = null;
async function hydrateEquipment() {
  if (equipmentLoading) return equipmentLoading;
  if (state.equipment.tools_status === "loaded") return;
  state.equipment = { ...state.equipment, tools_status: "loading" };
  equipmentLoading = (async () => {
    try {
      const equipment = await api("/desktop/api/equipment");
      if (!Array.isArray(equipment.tools)) throw new Error("工具目录响应无效");
      state.equipment = { ...state.equipment, tools: equipment.tools, tools_status: "loaded", tools_error: null };
      performance.mark("focus:equipment-loaded");
    } catch (error) {
      state.equipment = { ...state.equipment, tools_status: "error", tools_error: error.message };
    } finally {
      equipmentLoading = null;
      if (state.view === "draft") renderDraft();
    }
  })();
  if (state.view === "draft") renderDraft();
  return equipmentLoading;
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
    <div><span class="tiny muted">访问模式</span>${renderAccessModePicker("draft")}</div>
    ${renderEquipmentTools()}
    <p class="tiny muted">${escapeHtml(uiText("access.ability_boundary", "工具是否可用与进程文件边界分别生效"))}</p>
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
    void hydrateEquipment();
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
  if (draft.mode === "standard" && draft.authoring_document) draft.authoring_document.standard_equipment = structuredClone(draft.equipment);
  draft.mode = mode;
  if (mode === "standard" && draft.authoring_document?.standard_equipment) draft.equipment = structuredClone(draft.authoring_document.standard_equipment);
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
    // 策展小兵的访问模式由服务端钉在工作区保护，界面不给选择，这里如实写回同一取值
    draft.equipment = accessMode.writeMode(draft.equipment, accessMode.DEFAULT_MODE);
  } else {
    draft.equipment.permissions = [...document.querySelectorAll("[data-permission]:checked")].map(input => input.dataset.permission);
    draft.equipment.skills = normalizeSkillNames(draft.equipment.skills);
    const mode = readAccessModeFromDom("draft");
    if (mode) draft.equipment = accessMode.writeMode(draft.equipment, mode);
  }
  return draft;
}

function scheduleDraftSave() {
  const current = state.drafts.get(state.activeTaskId);
  const workbench = current?.mode === "standard" && window.FocusPatrolWorkbench?.get(current.draft_id);
  if (workbench) { syncDraftFromDom(); workbench.configurationChanged(); updateTokenState(); return; }
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
  const activeDraft = state.drafts.get(taskId);
  const workbench = activeDraft?.mode === "standard" && window.FocusPatrolWorkbench?.get(activeDraft.draft_id);
  if (workbench) { try { if (taskId === state.activeTaskId) syncDraftFromDom(); await workbench.flush(); return true; } catch (error) { setStatus(error.message, true); return false; } }
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
    ...(draft.authoring_document ? {authoring_document:draft.authoring_document, draft_revision:draft.draft_revision} : {}),
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
  const workbench = draft?.mode === "standard" && window.FocusPatrolWorkbench?.get(draft.draft_id);
  if (deploy) deploy.disabled = workbench ? state.deploying || !workbench.previewPlan?.executable : Boolean(over) || (draft.mode !== "context_curator" && !draft.final_human_message?.trim());
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
  const workbench = draft.mode === "standard" && window.FocusPatrolWorkbench?.get(draft.draft_id);
  if (!workbench && draft.mode !== "context_curator" && !draft.final_human_message.trim()) {
    state.deploying = false;
    updateTokenState();
    return setStatus("最后一条 HumanMessage 不能为空", true);
  }
  try {
    draft.deployment_id ||= crypto.randomUUID();
    const run = await api(`/desktop/api/drafts/${draft.draft_id}/deploy`, { method: "POST", body: JSON.stringify({ deployment_id: draft.deployment_id, ...(workbench ? {preview_token:workbench.previewPlan?.preview_token} : {}) }) });
    listenToRun(run);
    state.view = "map";
    state.drafts.delete(state.activeTaskId);
    await refreshTasks();
    render();
  } catch (error) { setStatus(error.message, true); }
  finally { state.deploying = false; updateTokenState(); }
}

// 上传条目在输入事件内同步创建，不能等到异步 UI action 或上传回执后才登记。
function composerAttachments(taskId) {
  if (!state.composerAttachments.has(taskId)) state.composerAttachments.set(taskId, new Map());
  return state.composerAttachments.get(taskId);
}

function createComposerAttachment(file, taskId) {
  let previewUrl = null;
  if (file.type?.startsWith("image/") && window.URL?.createObjectURL) {
    try { previewUrl = URL.createObjectURL(file); } catch { /* 无本地预览时仍可上传。 */ }
  }
  const entry = { id: crypto.randomUUID(), file, taskId, previewUrl, status: "preparing", error: "", removed: false };
  entry.done = new Promise(resolve => { entry.finish = resolve; });
  entry.cancelled = new Promise(resolve => { entry.cancel = resolve; });
  composerAttachments(taskId).set(entry.id, entry);
  refreshComposerAttachments(taskId);
  return entry;
}

function releaseComposerAttachment(entry) {
  entry.removed = true;
  entry.cancel();
  if (entry.previewUrl) URL.revokeObjectURL(entry.previewUrl);
  entry.previewUrl = null;
  entry.file = null;
  state.composerAttachments.get(entry.taskId)?.delete(entry.id);
  if (!state.composerAttachments.get(entry.taskId)?.size) state.composerAttachments.delete(entry.taskId);
}

function renderComposerAttachments(taskId) {
  const uploads = [...(state.composerAttachments.get(taskId)?.values() || [])];
  const materials = new Map((state.materials.get(taskId) || []).map(item => [item.material_id, item]));
  const cards = uploads.filter(entry => entry.status !== "ready").map(entry => ({ entry }));
  for (const binding of materialSelection(taskId).bindings) {
    cards.push({ materialId: binding.materialId, material: materials.get(binding.materialId), entry: uploads.find(entry => entry.materialId === binding.materialId) });
  }
  if (!cards.length) return "";
  return `<span class="composer-attachments-label">本次附件 · ${cards.length}</span><div class="composer-attachment-list">${cards.map(({ entry, material, materialId }) => {
    const name = material?.relative_path || entry?.file?.name || "附件";
    const status = material ? "ready" : entry?.status || "preparing";
    const label = status === "ready" ? "已就绪" : status === "failed" ? "失败" : "准备中";
    const image = entry?.previewUrl ? `<img src="${escapeHtml(entry.previewUrl)}" alt="">` : material?.is_image ? materialImageMarkup(material.material_id, "", "", null) : '<span class="ui-icon icon-file-text" aria-hidden="true"></span>';
    return `<article class="composer-attachment is-${status}" data-composer-task="${escapeHtml(taskId)}"${entry ? ` data-composer-upload="${entry.id}"` : ""}${materialId ? ` data-composer-material="${escapeHtml(materialId)}"` : ""}>
      <button type="button" class="composer-attachment-preview" data-action="composer-preview-attachment" aria-label="预览 ${escapeHtml(name)}"${!material && !entry?.previewUrl ? " disabled" : ""}>${image}</button>
      <span class="composer-attachment-info"><strong title="${escapeHtml(name)}">${escapeHtml(name)}</strong><small role="status">${label}${entry?.error ? `：${escapeHtml(entry.error)}` : ""}</small></span>
      ${status === "failed" ? '<button type="button" class="text-button" data-action="composer-retry-attachment">重试</button>' : ""}
      <button type="button" class="text-button" data-action="composer-remove-attachment" aria-label="移除 ${escapeHtml(name)}">移除</button></article>`;
  }).join("")}</div>`;
}

function refreshComposerAttachments(taskId) {
  if (state.activeTaskId !== taskId) return;
  const root = document.querySelector("#composerAttachments");
  if (root) {
    root.innerHTML = renderComposerAttachments(taskId);
    materialContentLoader.bindAll(root);
  }
}

function clearAttachmentPreparationError(taskId) {
  if (!state.composerErrors.get(taskId)?.startsWith("附件准备失败：")) return;
  if ([...(state.composerAttachments.get(taskId)?.values() || [])].some(entry => entry.status === "failed")) return;
  setComposerError(null, taskId);
  if (state.activeTaskId === taskId && !state.mainPreparations.has(taskId)) setStatus("");
}

async function handleComposerAttachmentAction(button) {
  const card = button.closest("[data-composer-task]");
  if (!card) return;
  const taskId = card.dataset.composerTask;
  const entry = state.composerAttachments.get(taskId)?.get(card.dataset.composerUpload);
  const materialId = card.dataset.composerMaterial;
  if (button.dataset.action === "composer-remove-attachment") {
    if (entry) releaseComposerAttachment(entry);
    const selection = materialSelection(taskId);
    if (selection.bindings.some(item => item.materialId === materialId)) {
      state.materialSelections.set(taskId, runMaterialPicker.toggleMaterial(selection, materialId));
    }
    const preparing = state.mainPreparations.get(taskId)?.selection;
    if (preparing) {
      preparing.bindings = preparing.bindings.filter(item => item.materialId !== materialId);
      preparing.requiredImageIds = preparing.requiredImageIds.filter(id => id !== materialId);
    }
    refreshComposerAttachments(taskId);
    clearAttachmentPreparationError(taskId);
    if (state.activeTaskId === taskId) renderInspector();
    return;
  }
  if (button.dataset.action === "composer-retry-attachment") {
    if (!entry || entry.status !== "failed") return;
    entry.status = "preparing";
    entry.error = "";
    entry.done = new Promise(resolve => { entry.finish = resolve; });
    refreshComposerAttachments(taskId);
    return uploadMaterialFile(entry.file, taskId, entry);
  }
  if (entry?.previewUrl) return openImageLightbox(entry.previewUrl, entry.file.name);
  const material = (state.materials.get(taskId) || []).find(item => item.material_id === materialId);
  if (material?.is_image) return openImageLightbox(await materialContentLoader.load(taskId, materialId), material.relative_path);
  if (material) return openFileLink({ path: material.relative_path }, taskId);
}

async function prepareMainAttachments(taskId, selection, entries) {
  if (entries.some(entry => !entry.removed && entry.status === "preparing")) {
    state.mainPreparations.set(taskId, { selection });
    if (state.activeTaskId === taskId) {
      setStatus("正在准备本次附件，完成后将自动发送…");
      setComposerError(null, taskId);
    }
    try { await Promise.all(entries.map(entry => Promise.race([entry.done, entry.cancelled]))); }
    finally {
      state.mainPreparations.delete(taskId);
      if (state.activeTaskId === taskId) setComposerError(null, taskId);
    }
  }
  const failed = entries.filter(entry => !entry.removed && entry.status !== "ready");
  if (failed.length) throw new Error(`附件准备失败：${failed.map(entry => entry.file?.name || "附件").join("、")}。请重试或移除后发送。`);
  for (const entry of entries) {
    if (!entry.removed && !selection.bindings.some(item => item.materialId === entry.materialId)) {
      selection = runMaterialPicker.toggleMaterial(selection, entry.materialId);
    }
  }
  return selection;
}

function consumeMainAttachments(taskId, outgoing, entries) {
  const sent = new Map(outgoing.materialInputs.map(item => [item.material_id, item.note]));
  const selection = materialSelection(taskId);
  const consumed = new Set(selection.bindings.filter(item => sent.has(item.materialId)
    && sent.get(item.materialId) === item.note
    && selection.requiredImageIds.includes(item.materialId) === outgoing.requiredImageIds.includes(item.materialId)).map(item => item.materialId));
  state.materialSelections.set(taskId, { ...selection, bindings: selection.bindings.filter(item => !consumed.has(item.materialId)), requiredImageIds: selection.requiredImageIds.filter(id => !consumed.has(id)) });
  for (const entry of [...(state.composerAttachments.get(taskId)?.values() || [])]) {
    if (consumed.has(entry.materialId) || entries.includes(entry)) releaseComposerAttachment(entry);
  }
  refreshComposerAttachments(taskId);
}

function materialSelection(taskId = state.activeTaskId) {
  if (!state.materialSelections.has(taskId)) {
    state.materialSelections.set(taskId, runMaterialPicker.empty());
  }
  // 未加载/刷新失败不等于材料已删除，恢复中的附件与必看标记须等权威列表就绪再清理。
  if (!state.materials.has(taskId) || ["loading", "error"].includes(taskResourceLoads.get(taskId)?.materials?.status)) {
    return state.materialSelections.get(taskId);
  }
  const materials = state.materials.get(taskId) || [];
  const normalized = runMaterialPicker.normalizeSelection(
    state.materialSelections.get(taskId), materials
  );
  state.materialSelections.set(taskId, normalized);
  return normalized;
}

function toggleRunMaterial(materialId) {
  const selection = materialSelection();
  state.materialSelections.set(
    state.activeTaskId,
    runMaterialPicker.toggleMaterial(selection, materialId)
  );
}

async function toggleImageRequired(materialId) {
  const taskId = state.activeTaskId;
  const selection = materialSelection(taskId);
  if (!selection.bindings.some(item => item.materialId === materialId)) {
    return setStatus("请先把图片选为本轮材料", true);
  }
  if (!selection.requiredImageIds.includes(materialId)) {
    await api(`/desktop/api/tasks/${taskId}/materials/${materialId}/image-validity`);
  }
  state.materialSelections.set(
    taskId,
    runMaterialPicker.toggleRequired(
      materialSelection(taskId),
      materialId,
      state.materials.get(taskId) || []
    )
  );
}

function updateRunMaterialNote(materialId, note) {
  state.materialSelections.set(
    state.activeTaskId,
    runMaterialPicker.setNote(materialSelection(), materialId, note)
  );
}

function openImageLightbox(url, label) {
  const root = document.querySelector("#overlayRoot");
  if (!root) return;
  // 整层可点即关（含点图片本身），避免用户找不到出口
  root.innerHTML = `<div class="image-lightbox" role="dialog" aria-modal="true" aria-label="${escapeHtml(label)}" data-action="close-lightbox"><img src="${escapeHtml(url)}" alt="${escapeHtml(label)}"><button class="text-button" data-action="close-lightbox">关闭</button></div>`;
}

async function sendMainOnce(taskId, requestId, retry = null, delivery = null) {
  const task = state.tasks.find(item => item.task_id === taskId);
  if (!task) return setStatus("当前没有活动任务", true);
  adoptCommitmentContext();
  if (!retry && activeTaskHasCommitmentLock()) {
    return setStatus("存在尚未处理的承诺流程，请先处理审批面板", true);
  }
  if (!retry && state.details.get(taskId)?.pending_compression) {
    return setStatus("存在待确认的压缩请求，请先完成压缩或取消", true);
  }
  if (!retry && state.details.get(taskId)?.pending_must_view_report) {
    return setStatus("存在待处理的必看图片报告，请先重试或取消", true);
  }
  if (!retry && state.accessReviews.panel) {
    return setStatus("存在待批准的本机资源访问请求，请先批准或拒绝", true);
  }
  setStatus("");
  const input = document.querySelector("#mainInput");
  const message = retry ? retry.message : input.value.trim();
  const submittedFileRefs = retry?.fileReferences || (retry ? [] : JSON.parse(JSON.stringify(fileReferences(taskId))));
  let submittedSelection = retry ? null : JSON.parse(JSON.stringify(materialSelection(taskId)));
  const attachments = retry ? [] : [...(state.composerAttachments.get(taskId)?.values() || [])]
    .filter(entry => entry.status !== "ready");
  if (!retry && !message && !submittedFileRefs.length && !submittedSelection.bindings.length && !attachments.length) return;
  if (!retry) composerDraft.claim(taskId, input.value);
  // f35 仅保留规范命令；旧 @压缩 输入按普通用户消息发送。
  const quickKeyword = keywordCommand.parse(message);
  if (!retry && !submittedFileRefs.length && !submittedSelection.bindings.length && !attachments.length && quickKeyword !== null && !mainReference(taskId)) {
    const keyword = quickKeyword;
    input.value = "";
    composerDraft.release(taskId);
    return openCompressionView(task, null, keyword);
  }
  setComposerError();
  const submittedInput = retry?.input ?? input.value;
  const frozenBody = retry ? JSON.parse(retry.body) : null;
  const submittedReference = retry ? (frozenBody.run || frozenBody).message_reference || null : mainReference(taskId);
  const queuedEdit = !retry && taskRunOperations?.recovery(taskId)?.queued_edit
    ? JSON.parse(taskRunOperations.recovery(taskId).body) : null;
  const requestOptions = retry ? null : {
    ...(queuedEdit || {}),
    skills: selectedSkills("main"),
    ...accessMode.requestBody(accessModeOf("main")),
  };
  const spatialTarget = retry ? null : currentSpatialTarget();
  if (attachments.length) {
    try { submittedSelection = await prepareMainAttachments(taskId, submittedSelection, attachments); }
    catch (error) {
      if (state.activeTaskId === taskId) setStatus(error.message, true);
      setComposerError(error.message, taskId);
      return;
    }
  }
  // 等待期间可能移除了最后一个附件；只使用本次冻结的正文与附件判断，不能发送空消息。
  if (!retry && !message && !submittedFileRefs.length && !submittedSelection.bindings.length) return;
  if (!delivery && !retry && !submittedFileRefs.length && !submittedReference && !submittedSelection.bindings.length && spatialTarget?.focus.kind === "patrol"
      && typeof spatialTarget.view.sendFocusedMessage === "function") {
    try {
      if (await spatialTarget.view.sendFocusedMessage(message)) {
        input.value = "";
        composerDraft.release(taskId);
        updateAtHighlight(input);
        const focusedDetail = state.details.get(taskId);
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
  const selectionSignature = retry?.selectionSignature || JSON.stringify(submittedSelection);
  const retryBody = retry ? JSON.parse(retry.body) : null;
  const original = retryBody?.run || retryBody;
  const outgoing = original ? { message: original.message, materialInputs: original.material_inputs, requiredImageIds: original.must_view_material_ids }
    : runMaterialPicker.buildOutgoing(state.materials.get(taskId) || [], composerFiles.compose(message, submittedFileRefs), submittedSelection);
  if (!retry && !outgoing.message.trim() && !outgoing.materialInputs.length) return;
  const messagePayload = outgoing.message;
  let detail = state.details.get(taskId) || {};
  let confirmed = false;
  try {
    const runBody = {
        ...requestOptions,
        message: messagePayload,
        ...(submittedReference ? { message_reference: submittedReference } : {}),
        material_inputs: outgoing.materialInputs,
        must_view_material_ids: outgoing.requiredImageIds,
        spatial_focus: spatialTarget?.focus || null,
    };
    delete runBody.attached_material_ids;
    delete runBody.idempotency_key;
    if (!submittedReference) delete runBody.message_reference;
    const frozen = retry || taskRunOperations?.freeze(taskId, requestId, {
      input: submittedInput, message, selectionSignature, fileReferences: submittedFileRefs,
      endpoint: delivery ? "inputs" : "runs",
      body: JSON.stringify(delivery ? { ...delivery, run: runBody } : runBody),
    });
    state.mainRecoveryRestored.set(taskId, requestId);
    updateMainRecoveryControls();
    const run = await api(`/desktop/api/tasks/${taskId}/main/${frozen.endpoint || "runs"}`, {
      method: "POST",
      headers: { "Idempotency-Key": requestId },
      body: frozen.body,
    });
    if (!run || (!run.run_id && !run.input_id && !(run.intent_kind === "direct_message" && run.intent_id))) throw new Error("未收到有效受理回执");
    confirmed = true;
    consumeFileReferences(taskId, submittedFileRefs);
    taskRunOperations?.accept(taskId, requestId, run);
    taskRunOperations?.forgetRecovery(taskId, requestId);
    state.mainRecoveryRestored.delete(taskId);
    state.composerErrors.delete(taskId);
    detail = state.details.get(taskId) || detail;
    referenceDrafts.consume(taskId, submittedReference);
    detail.ui_state = { ...(detail.ui_state || {}), message_reference: referenceDrafts.get(taskId) };
    if (run.intent_kind === "direct_message" || run.input_id) {
      if (run.input_id) detail.conversation_inputs = [...(detail.conversation_inputs || []).filter(item => item.input_id !== run.input_id), run];
      consumeMainAttachments(taskId, outgoing, attachments);
      const unchanged = !composingMainInputs.has(input)
        && composerDraft.value(taskId, detail.ui_state?.input || "").trim() === message;
      if (unchanged) composerDraft.release(taskId);
      detail.ui_state = { ...(detail.ui_state || {}), input: unchanged ? "" : composerDraft.value(taskId),
        skills: unchanged ? [] : detail.ui_state?.skills || [] };
      state.details.set(taskId, detail);
      if (state.activeTaskId === taskId) {
        if (unchanged && document.querySelector("#mainInput") === input) input.value = "";
        persistFocusState();
        if (state.view === "focus") renderFocus();
        setStatus(run.input_id ? window.FocusConversationInputs.labels[run.status] : "消息已受理，交付与执行以实际工作状态为准");
      }
      return;
    }
    const contextNode = (state.contextTrees.get(task.workspace_id) || [])
      .find(item => item.context_id === taskId);
    if (contextNode) contextNode.editable = false;
    if (detail.context) detail.context.editable = false;
    // 运行已发起：立即暴露中断入口（否则运行中 active_run 仍为旧值，按钮不渲染）
    detail.active_run = run;
    const byMaterialId = new Map((state.materials.get(taskId) || []).map(item => [item.material_id, item]));
    const optimisticHistory = outgoing.materialInputs.map((item, ordinal) => ({
      binding_id: `optimistic:${run.run_id}:${ordinal}`,
      run_id: run.run_id,
      task_id: taskId,
      message_id: run.message_id,
      material_id: item.material_id,
      relative_path: byMaterialId.get(item.material_id)?.relative_path || item.material_id,
      material_kind: byMaterialId.get(item.material_id)?.material_kind || "other",
      note: item.note,
      ordinal,
      must_view_requested: outgoing.requiredImageIds.includes(item.material_id),
      current_available: true,
      run_status: run.status,
    }));
    state.materialHistory.set(taskId, [
      ...(state.materialHistory.get(taskId) || []),
      ...optimisticHistory,
    ]);
    consumeMainAttachments(taskId, outgoing, attachments);
    state.composerErrors.delete(taskId);
    if (!(detail.messages || []).some(item => item.id === run.message_id)) detail.messages = [...(detail.messages || []), { role: "human", content: messagePayload, id: run.message_id,
      ...(submittedReference ? { additional_kwargs: { message_reference: submittedReference } } : {}) }];
    const unchanged = !composingMainInputs.has(input)
      && composerDraft.value(taskId, detail.ui_state?.input || "").trim() === message;
    detail.ui_state = { ...(detail.ui_state || {}), input: unchanged ? "" : composerDraft.value(taskId), skills: unchanged ? [] : detail.ui_state?.skills || [] };
    if (unchanged) composerDraft.release(taskId);
    state.details.set(taskId, detail);
    if (state.activeTaskId === taskId && state.view === "focus") {
      if (unchanged && document.querySelector("#mainInput") === input) input.value = "";
      renderFocus();
      persistFocusState();
    }
    listenToRun(run);
  } catch (error) {
    if (!confirmed) taskRunOperations?.fail(taskId, requestId, error.message);
    let failed = null;
    if (!confirmed) {
      try { failed = taskRunOperations?.failRecovery(taskId, requestId, error); }
      catch { /* 投递前的冻结快照仍在；保存诊断失败不能丢失原身份。 */ }
    }
    const feedback = confirmed ? `消息已受理，但本地状态更新失败：${error.message}`
      : failed?.failure?.uncertain === false ? `发送未受理：${error.message}`
      : taskRunOperations?.recovery(taskId) ? `尚未确认是否受理：${error.message}` : error.message;
    if (state.activeTaskId === taskId) setStatus(feedback, true);
    setComposerError(feedback, taskId);
  }
}

function sendMain(retryOriginal = false, mode = null) {
  const taskId = state.activeTaskId;
  if (!taskId) return Promise.resolve();
  if (taskRunOperations?.get(taskId).submission?.status === "pending") {
    setStatus("这条消息正在提交，请稍候；其他 Context 仍可独立发送。", false);
    return Promise.resolve();
  }
  const saved = taskRunOperations?.recovery(taskId);
  const retry = saved && (retryOriginal || !saved.editing) ? saved : null;
  const requestId = retry?.request_id || crypto.randomUUID();
  taskRunOperations?.begin(taskId, requestId);
  const target = mainDeliveryRun(taskId);
  const delivery = mode === "queue" || saved?.queued_edit ? { mode: "queue" } : target ? { mode: "steer", target_run_id: target.run_id } : null;
  return sendMainOnce(taskId, requestId, retry, delivery).finally(() => {
    taskRunOperations?.finish(taskId, requestId);
    if (state.activeTaskId === taskId) updateMainRecoveryControls();
  });
}

const COMMITMENT_STAGE_NAMES = {
  1: "明确目标", 2: "要求与兼容性", 3: "优先级", 4: "必要输入",
  5: "技术版本", 6: "官方知识", 7: "合同落盘", 8: "产出合同", 9: "交接准备",
};
const TRACE_ACTORS = { supervisor: "Supervisor", worker: "Worker", evaluator: "Evaluator" };
const TERMINAL_RUN_STATUSES = new Set(["success", "error", "interrupted", "timeout", "cancelled", "stopped"]);

function renderRunUpdate(taskId) {
  if (state.view === "focus" && taskId && state.activeTaskId !== taskId) {
    // Another task's completion only changes the task list, never this editor.
    const rail = app.querySelector?.(".task-workspace-list");
    const task = activeTask();
    if (rail && task) {
      const markup = renderTaskRail(task);
      if (rail.outerHTML !== markup) {
        const template = document.createElement("template");
        template.innerHTML = markup;
        rail.replaceWith(template.content.firstElementChild);
      }
    }
    return;
  }
  scheduleRender();
}

async function settleRunStream(run, source, ownerTaskId, terminal) {
  if (state.streams.get(run.run_id) !== source) return;
  syncPatrolRunState(run, terminal.status, terminal.error || null);
  if (ownerTaskId) {
    taskRunOperations?.updateRun(ownerTaskId, run.run_id, {
      status: terminal.status,
      error: terminal.error || null,
    });
  }
  const wasMainInterrupted = run.kind === "main"
    && terminal.status === "interrupted"
    && !terminal.error;
  source.close();
  state.streams.delete(run.run_id);
  clearStreamBuffer(run.run_id);
  await refreshTaskAfterTxn(ownerTaskId);
  const task = run.task_id
    ? state.tasks.find(item => item.task_id === run.task_id)
    : state.tasks.find(item => item.thread_id === run.thread_id);
  if (task) settleCommitmentRun(task.task_id, terminal);
  renderRunUpdate(ownerTaskId);
  const detail = task ? state.details.get(task.task_id) : null;
  if (wasMainInterrupted
      && !state.commitment.review
      && !state.commitment.recovery
      && !detail?.pending_compression
      && !detail?.pending_must_view_report) {
    setStatus("主 Agent 已中断，可继续对话");
  } else if (terminal.status === "error" && terminal.error) {
    setStatus(terminal.error, true);
  }
}

async function reconcileDisconnectedRun(run, source, ownerTaskId) {
  try {
    const terminal = await api(`/desktop/api/runs/${run.run_id}`);
    if (!TERMINAL_RUN_STATUSES.has(terminal?.status)) return;
    await settleRunStream(run, source, ownerTaskId, terminal);
  } catch {
    // EventSource 会自行重连；服务暂时不可达时保留当前流，不用另一套轮询状态机。
  }
}

function listenToRun(run) {
  if (state.streams.has(run.run_id)) return;
  const ownerTaskId = run.task_id || state.activeTaskId;
  if (ownerTaskId) taskRunOperations?.updateRun(ownerTaskId, run.run_id, run);
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
  const handlers = new Map();
  const listen = (type, handler) => handlers.set(type, handler);
  let source;
  let resyncPending = null;
  let resyncAgain = false;
  listen("resync", () => {
    clearStreamBuffer(run.run_id);
    // 缺失的完整消息、工具结果和待审批状态从既有权威快照恢复。
    resyncAgain = true;
    if (!resyncPending) resyncPending = (async () => {
      do {
        resyncAgain = false;
        await refreshTaskAfterTxn(ownerTaskId);
      } while (resyncAgain && state.streams.get(run.run_id) === source);
      renderRunUpdate(ownerTaskId);
    })()
      .catch(error => setStatus(`运行内容补读失败：${error.message}`, true))
      .finally(() => { resyncPending = null; });
    return resyncPending;
  });
  listen("metadata", event => {
    const envelope = parseEvent(event);
    if (envelope?.data?.status) {
      syncPatrolRunState(run, envelope.data.status);
      if (ownerTaskId) taskRunOperations?.updateRun(ownerTaskId, run.run_id, { status: envelope.data.status });
    }
  });
  listen("tokens", event => {
    const token = parseEvent(event);
    if (token) appendToken(token);
  });
  listen("reasoning", event => {
    const reasoning = parseEvent(event);
    if (reasoning) appendReasoning(reasoning);
  });
  listen("events", event => {
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
      replaceConversation(task, messages, envelope.run_id);
    }
  });
  listen("interrupt", event => {
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
      return;
    }
    if (value.type === "must_view_report") {
      const task = state.tasks.find(item => item.thread_id === envelope.thread_id && item.workspace_id === envelope.workspace_id);
      if (!task) return;
      const detail = state.details.get(task.task_id) || {};
      detail.pending_must_view_report = value;
      detail.must_view_recovery = { status: "resumable", request: value };
      state.details.set(task.task_id, detail);
      if (state.activeTaskId === task.task_id) scheduleRender();
      return;
    }
    if (accessApproval.isAccessReview(value)) {
      const task = state.tasks.find(item => item.thread_id === envelope.thread_id && item.workspace_id === envelope.workspace_id);
      if (!task) return;
      showAccessReview(task, value, envelope.agent_id);
    }
  });
  listen("error", event => {
    const error = parseEvent(event)?.data?.error || "运行失败";
    runError = error;
    syncPatrolRunState(run, "error", error);
    if (ownerTaskId) taskRunOperations?.updateRun(ownerTaskId, run.run_id, { status: "error", error });
    setStatus(error, true);
  });
  listen("end", async event => {
    const terminal = parseEvent(event, { status: "error", error: "运行流异常结束" });
    if (!terminal.error && runError) terminal.error = runError;
    await settleRunStream(run, source, ownerTaskId, terminal);
  });
  source = runStreams.subscribe(run.run_id, {
    onFrame: frame => handlers.get(frame.type)?.(frame),
    onConnection: status => {
      if (status === "reconnecting") void reconcileDisconnectedRun(run, source, ownerTaskId);
    },
  });
  state.streams.set(run.run_id, source);
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

async function resumeMustView(decision) {
  const task = activeTask();
  if (!task) return setStatus("当前没有活动任务", true);
  try {
    const run = await api(`/desktop/api/threads/${task.thread_id}/runs/resume`, {
      method: "POST",
      body: JSON.stringify({ resume: { type: "must_view_report", decision } }),
    });
    const detail = state.details.get(task.task_id);
    if (detail) {
      detail.pending_must_view_report = null;
      detail.must_view_recovery = null;
      detail.active_run = run;
    }
    listenToRun(run);
    render();
  } catch (error) {
    setStatus(error.message, true);
  }
}

// === 本机资源访问模式 ===

const ACCESS_MODE_TARGETS = {
  main: {
    holder: taskId => state.details.get(taskId || state.activeTaskId)?.ui_state,
    set: (taskId, holder) => {
      const detail = state.details.get(taskId || state.activeTaskId);
      if (detail) detail.ui_state = holder;
    },
    persist: (taskId, mode) => api(`/desktop/api/tasks/${taskId}/access-mode`, {
      method: "PUT", body: JSON.stringify({ access_mode: mode }),
    }),
    pinned: () => false,
  },
  draft: {
    holder: () => state.drafts.get(state.activeTaskId)?.equipment,
    set: (_taskId, holder) => {
      const draft = state.drafts.get(state.activeTaskId);
      if (draft) draft.equipment = holder;
    },
    persist: () => { scheduleDraftSave(); return Promise.resolve(); },
    // 策展草稿的访问模式由服务端钉在工作区保护，界面因此不给可点的选择，只如实显示
    pinned: () => state.drafts.get(state.activeTaskId)?.mode === "context_curator",
  },
};
const accessModeSaves = new Map();

function accessModeOf(target, taskId) {
  return accessMode.readMode(ACCESS_MODE_TARGETS[target].holder(taskId));
}

function renderAccessModePicker(target) {
  const config = ACCESS_MODE_TARGETS[target];
  const mode = accessModeOf(target);
  const label = uiText(accessMode.labelKey(mode), accessMode.labelFallback(mode));
  const shield = `<svg class="access-mode-shield" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M10 2.4 16 4.8v4.6c0 4-2.4 6.8-6 8.2-3.6-1.4-6-4.2-6-8.2V4.8L10 2.4Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
  if (config.pinned()) {
    return `<div class="access-mode-picker is-pinned" data-access-mode-target="${target}" data-mode="${mode}">
      <span class="access-mode-chip is-pinned" title="${escapeHtml(uiText("access.mode_pinned_curator", "策展小兵固定为工作区保护"))}">${shield}<span class="access-mode-label">${escapeHtml(label)}</span></span>
    </div>`;
  }
  const options = accessMode.MODES.map(value => `
    <button type="button" role="menuitemradio" aria-checked="${value === mode}" class="access-mode-option is-${value}${value === mode ? " is-active" : ""}" data-action="select-access-mode" data-access-mode-target="${target}" data-access-mode-value="${value}">
      <span class="access-mode-option-mark" aria-hidden="true"></span>
      <span class="access-mode-option-copy"><span class="access-mode-option-title">${escapeHtml(uiText(accessMode.labelKey(value), accessMode.labelFallback(value)))}</span><small>${escapeHtml(uiText(accessMode.descriptionKey(value), accessMode.descriptionFallback(value)))}</small></span>
      <span class="access-mode-option-check" aria-hidden="true">✓</span>
    </button>`).join("");
  return `<div class="access-mode-picker" data-access-mode-target="${target}" data-mode="${mode}">
    <button type="button" class="access-mode-chip is-${mode}" data-action="toggle-access-mode" data-access-mode-target="${target}" aria-haspopup="menu" aria-expanded="false" title="${escapeHtml(uiText("access.mode_switch_hint", "切换本机资源访问模式"))}">${shield}<span class="access-mode-label">${escapeHtml(label)}</span><span class="access-mode-caret" aria-hidden="true">▾</span></button>
    <div class="access-mode-menu glass-surface" role="menu" hidden>
      <div class="access-mode-menu-head"><strong>${escapeHtml(uiText("access.mode_menu_title", "文件访问模式"))}</strong><span>${escapeHtml(uiText("access.mode_menu_scope", "当前会话"))}</span></div>
      <div class="access-mode-options">${options}</div>
      <p class="access-mode-menu-note">${escapeHtml(uiText("access.ability_boundary", "工具是否可用与进程文件边界分别生效"))}</p>
      <section class="access-mode-risk" hidden>${accessMode.riskNoticeHtml(uiText, { confirm: "confirm-access-mode", cancel: "cancel-access-mode" })}</section>
    </div>
  </div>`;
}

function readAccessModeFromDom(target) {
  const picker = document.querySelector(`.access-mode-picker[data-access-mode-target="${target}"]`);
  const selected = picker?.querySelector(".access-mode-option.is-active");
  return selected ? accessMode.normalize(selected.dataset.accessModeValue) : null;
}

function closeAccessModeMenus() {
  document.querySelectorAll(".access-mode-menu").forEach(menu => {
    void window.FocusSurfaceTransition.visible(menu, false, { distance: 8 }).then(done => {
      if (!done) return;
      menu.dataset.confirming = "false";
      const risk = menu.querySelector(".access-mode-risk");
      if (risk) risk.hidden = true;
    });
  });
  document.querySelectorAll('[data-action="toggle-access-mode"]').forEach(button => {
    button.setAttribute("aria-expanded", "false");
  });
}

function toggleAccessModeMenu(button) {
  const picker = button.closest(".access-mode-picker");
  const menu = picker?.querySelector(".access-mode-menu");
  if (!menu) return;
  const open = menu.hidden || menu.inert;
  closeAccessModeMenus();
  if (open) void window.FocusSurfaceTransition.visible(menu, true, { distance: 8 });
  button.setAttribute("aria-expanded", String(open));
  const risk = picker.querySelector(".access-mode-risk");
  if (risk) risk.hidden = true;
}

function selectAccessMode(button) {
  const picker = button.closest(".access-mode-picker");
  const target = button.dataset.accessModeTarget;
  const wanted = button.dataset.accessModeValue;
  if (accessMode.widens(accessModeOf(target), wanted) && picker?.querySelector(".access-mode-risk")) {
    picker.dataset.pendingMode = wanted;
    picker.querySelector(".access-mode-menu").dataset.confirming = "true";
    picker.querySelector(".access-mode-risk").hidden = false;
    return;
  }
  closeAccessModeMenus();
  applyAccessMode(target, wanted);
}

function confirmAccessMode(button) {
  const target = button.closest(".access-mode-picker")?.dataset.accessModeTarget;
  closeAccessModeMenus();
  if (target) applyAccessMode(target, button.closest(".access-mode-picker")?.dataset.pendingMode || accessMode.DEFAULT_MODE);
}

function cancelAccessMode(button) {
  const risk = button.closest(".access-mode-risk");
  if (risk) {
    risk.hidden = true;
    risk.closest(".access-mode-menu").dataset.confirming = "false";
  }
}

async function applyAccessMode(target, mode, taskId) {
  const config = ACCESS_MODE_TARGETS[target];
  if (config.pinned()) return;
  const id = taskId || state.activeTaskId;
  const save = async () => {
    const current = config.holder(id);
    if (!current) return;
    const next = accessMode.writeMode(current, mode);
    const changed = accessMode.readMode(next);
    await config.persist(id, changed);
    config.set(id, next);
    setStatus(uiText(
      changed === "danger-full-access" ? "access.mode_changed_full" : "access.mode_changed_workspace",
      changed === "danger-full-access" ? "未应用文件沙箱" : `已切换为${accessMode.labelFallback(changed)}`,
    ));
    render();
  };
  if (target !== "main") return save();
  const pending = (accessModeSaves.get(id) || Promise.resolve()).catch(() => {}).then(save);
  accessModeSaves.set(id, pending);
  try { await pending; } finally {
    if (accessModeSaves.get(id) === pending) accessModeSaves.delete(id);
  }
}

// === 本机资源访问批准面板 ===

const ACCESS_OPERATION_FALLBACKS = { read: "读取", write: "写入", command: "执行" };

function accessReviewKey(taskId, payload) {
  const reads = Array.isArray(payload?.reads) ? payload.reads.join("|") : "";
  const writes = Array.isArray(payload?.writes) ? payload.writes.join("|") : "";
  return [taskId, payload?.request_id || "", payload?.tool || "", payload?.command || "", reads, writes].join("::");
}

function showAccessReview(task, payload, agentId) {
  const key = accessReviewKey(task.task_id, payload);
  if (state.accessReviews.key === key && state.accessReviews.panel?.isConnected) return;

  const fragment = document.querySelector("#accessReviewTemplate").content.cloneNode(true);
  const panel = fragment.querySelector(".access-review-panel");
  const mainSubject = accessApproval.isMainSubject(agentId);
  panel.dataset.subject = mainSubject ? "main" : "background";
  panel.dataset.taskId = task.task_id;
  panel.querySelector(".access-review-subject").textContent = mainSubject
    ? uiText("access.subject_main", "主 Agent")
    : String(agentId || uiText("access.background_title", "后台执行主体请求本机资源"));
  renderAccessReviewFields(panel, payload);
  // 后台执行主体的访问模式随其装备（spawn 时写入）持久化，本面板只决定「这一次」，
  // 因此放宽动作仅对主执行身份开放；避免给出一个当下无法兑现的按钮
  panel.querySelector(".switch-full-button").hidden = !mainSubject;
  bindAccessReview(panel, task, payload);

  closeAccessReview();
  state.accessReviews = { panel, payload, taskId: task.task_id, key, busy: false };
  attachAccessReviewPanel(panel, mainSubject);
  setStatus(uiText("access.badge", "等待批准"), false);
}

function attachAccessReviewPanel(panel, mainSubject) {
  // 主执行身份的待决优先落在主流程面板；会话视图不可用时退回始终存在的待处理区，
  // 待决因此不会因为用户当前不在会话页而被静默丢弃
  const host = mainSubject ? conversationNode() || accessPendingNode : accessPendingNode;
  if (!host) return;
  host.append(panel);
  if (host === accessPendingNode) {
    accessPendingNode.hidden = false;
    return;
  }
  hideAccessPendingWhenEmpty();
  scrollConversation();
}

function restoreAccessReviewPanels(conversation) {
  // 会话视图重新渲染后，把仍属于当前任务的主执行待决移回会话区
  const panel = state.accessReviews.panel;
  if (!conversation || !panel) return;
  if (state.accessReviews.taskId !== state.activeTaskId) return;
  if (panel.dataset.subject !== "main") return;
  conversation.append(panel);
  hideAccessPendingWhenEmpty();
}

function hideAccessPendingWhenEmpty() {
  if (accessPendingNode?.querySelector(".access-review-panel")) return;
  accessPendingNode.hidden = true;
}

function renderAccessReviewFields(panel, payload) {
  const cells = accessApproval.approvalFields(payload).map(field => {
    const label = `<dt data-i18n="${escapeHtml(field.labelKey)}">${escapeHtml(uiText(field.labelKey, field.labelKey))}</dt>`;
    let value;
    if (field.mode) {
      // 访问模式的展示文案只来自访问模式模块
      value = `<dd data-i18n="${escapeHtml(accessMode.labelKey(field.mode))}">${escapeHtml(uiText(accessMode.labelKey(field.mode), accessMode.labelFallback(field.mode)))}</dd>`;
    } else if (field.lines?.length) {
      // 逐行标注操作类型：人据此判断这个路径会不会被改写
      value = `<dd>${field.lines.map(line => `
        <span class="access-review-target"><span class="access-review-op" data-i18n="${escapeHtml(line.operationKey)}">${escapeHtml(uiText(line.operationKey, ACCESS_OPERATION_FALLBACKS[line.operation] || line.operation))}</span><code>${escapeHtml(line.value)}</code></span>`).join("")}</dd>`;
    } else {
      value = `<dd>${escapeHtml(field.value)}</dd>`;
    }
    return `<div class="access-review-field">${label}${value}</div>`;
  }).join("");
  panel.querySelector(".access-review-fields").innerHTML = cells;
}

function bindAccessReview(panel, task, payload) {
  const risk = panel.querySelector(".access-review-risk");
  // 风险说明由访问模式模块渲染：批准面板与作曲区的模式选择器共用同一份文案与结构，
  // 只有「确认之后做什么」不同——这里还要放行当前这次调用
  risk.innerHTML = accessMode.riskNoticeHtml(uiText, {
    confirm: "confirm-access-review-full",
    cancel: "cancel-access-mode",
  });
  panel.querySelectorAll("[data-access-action]").forEach(button => {
    button.addEventListener("click", () => {
      const action = button.dataset.accessAction;
      const switched = accessApproval.switchedMode(action, payload);
      // 放宽访问范围前先确认：确认步骤本身就是风险提示的载体
      if (switched && accessMode.widens(accessModeOf("main", task.task_id), switched)) {
        risk.hidden = false;
        return;
      }
      resumeAccessReview(task, payload, action);
    });
  });
}

function confirmAccessReviewFull(button) {
  const panel = button.closest(".access-review-panel");
  const task = state.tasks.find(item => item.task_id === panel?.dataset.taskId);
  if (!task || !state.accessReviews.payload) return;
  resumeAccessReview(task, state.accessReviews.payload, "switch_full");
}

function closeAccessReview() {
  state.accessReviews.panel?.remove();
  state.accessReviews = { panel: null, payload: null, taskId: null, key: null, busy: false };
  hideAccessPendingWhenEmpty();
}

async function resumeAccessReview(task, payload, action) {
  if (state.accessReviews.busy) return;
  state.accessReviews.busy = true;
  const tool = String(payload?.tool || "");
  const switched = accessApproval.switchedMode(action, payload);
  try {
    if (switched) await applyAccessMode("main", switched, task.task_id);
    const run = await api(`/desktop/api/threads/${task.thread_id}/runs/resume`, {
      method: "POST",
      body: JSON.stringify({ resume: accessApproval.resumeValue(action, payload) }),
    });
    closeAccessReview();
    listenToRun(run);
    if (action === "reject") {
      setStatus(uiText("access.denied", "已拒绝：{tool} 未执行，模型将收到可读的失败结果", { tool }));
    } else if (action === "cancel") {
      setStatus(uiText("access.cancelled", "已取消本次操作"));
    } else if (switched) {
      setStatus(uiText("access.switched_full", "已允许本次调用；后续运行按完全权限执行"));
    } else {
      setStatus(uiText("access.approved", "已允许这一次：{tool}", { tool }));
    }
  } catch (error) {
    state.accessReviews.busy = false;
    setStatus(error.message, true);
    return;
  }
  state.accessReviews.busy = false;
}

function reconcileAccessReview(detail) {
  const pending = detail?.pending_access_review || null;
  if (!pending) {
    if (state.accessReviews.taskId === state.activeTaskId) closeAccessReview();
    return;
  }
  const task = state.tasks.find(item => item.task_id === state.activeTaskId);
  if (!task) return;
  showAccessReview(task, pending, `main:${task.task_id}`);
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
      setStatus("");
    } else if (state.commitment.taskId === state.activeTaskId && state.commitment.recoveryRestored) {
      resetCommitment();
    }
    return;
  }
  if (state.commitment.taskId && state.commitment.taskId !== state.activeTaskId) {
    resetCommitment();
    setStatus("");
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

async function openCompressionView(task, request, quickKeyword, manual = false) {
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
      manual: manual && !request && !detail.pending_compression,
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
    } else if (manual) {
      setStatus("选择需要整理的消息；确认前不会修改上下文");
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
    if (c.quick || c.manual) {
      // 快捷关键字压缩：先机械剥离禁用词再写回，不走 resume
      const keyword = c.quick?.keyword;
      await api("/desktop/api/compression/quick-apply", {
        method: "POST",
        body: JSON.stringify({
          task_id: task.task_id,
          ranges,
          scrub_terms: keyword ? [keyword] : [],
        }),
      });
      // 快捷 apply 无 run/SSE，需手动重载会话与上下文树，否则压缩块不会立刻显示
      await refreshActiveAfterTxn();
      closeCompressionView();
      setStatus(keyword ? `已关键字快捷压缩「${keyword}」，上下文已更新` : "上下文整理已提交");
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
    if (c.quick || c.manual) {
      // 快捷关键字压缩不经 interrupt/resume 通道（quick-apply 直接写回，无中断可取消）：
      // 取消只需关闭本地面板并清空状态，调用 resume 只会得到 409「无可恢复的承诺流程」。
      closeCompressionView();
      setStatus(c.manual ? "已取消上下文整理" : "已取消关键字快捷压缩");
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

async function refreshTaskAfterTxn(taskId) {
  await refreshTasks();
  await hydrateContextTrees();
  if (!taskId) return;
  if (state.activeTaskId === taskId) {
    await hydrateActive();
    return;
  }
  try {
    state.details.set(taskId, await api(`/desktop/api/tasks/${taskId}`));
  } catch (error) {
    taskRunOperations?.updateRun(taskId, taskRunOperations.get(taskId).active_run?.run_id || "unknown", { refresh_error: error.message });
  }
}

async function createTask(form) {
  if (state.creatingTask) return;
  const path = form.querySelector("#workspacePath").value.trim();
  const title = form.querySelector("#threadTitle").value.trim();
  if (!path || !title) return;
  state.creatingTask = true;
  const submit = form.querySelector('button[type="submit"]');
  if (submit) submit.disabled = true;
  try {
    const workspace = await api("/desktop/api/workspaces", { method: "POST", body: JSON.stringify({ path }) });
    const task = await api(`/desktop/api/workspaces/${workspace.workspace_id}/threads`, {
      method: "POST",
      body: JSON.stringify({ title, access_mode: accessMode.readNewSessionDefault(window.localStorage) }),
    });
    void window.FocusSurfaceTransition.visible(dialog, false, { modal: true }); replaceTasks([...state.tasks, task]); state.activeTaskId = task.task_id; state.view = "focus";
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

function refreshMaterialView(taskId, changed = false) {
  if (changed) {
    const draft = state.drafts.get(taskId);
    if (draft?.mode === "standard") window.FocusPatrolWorkbench?.get(draft.draft_id)?.configurationChanged();
  }
  if (state.activeTaskId === taskId) render();
}

async function handleMaterialAction(button) {
  const taskId = state.activeTaskId;
  const row = button.closest("[data-material-id]");
  const materialId = row.dataset.materialId;
  const material = (state.materials.get(taskId) || []).find(item => item.material_id === materialId);
  try {
  if (button.dataset.action === "toggle-material") { state.openMaterial = state.openMaterial === materialId ? null : materialId; return refreshMaterialView(taskId); }
  if (button.dataset.action === "toggle-run-material") { toggleRunMaterial(materialId); return refreshMaterialView(taskId); }
  if (button.dataset.action === "toggle-image-required") { await toggleImageRequired(materialId); return refreshMaterialView(taskId); }
  if (button.dataset.action === "move-material-group") {
    const groups = state.materialGroups.get(state.activeTaskId) || [];
    if (!groups.length) return setStatus("请先新建自定义分组", true);
    const requested = prompt(`输入目标分组名称：\n${groups.map(item => item.name).join("、")}`, groups.find(item => item.group_id === material?.custom_group_id)?.name || groups[0].name);
    if (requested === null) return;
    const group = groups.find(item => item.name === requested.trim());
    if (!group) return setStatus("没有这个分组", true);
    await moveMaterialMembership(materialId, group.group_id);
    return;
  }
  if (button.dataset.action === "restore-auto-group") {
    await moveMaterialMembership(materialId, null);
    return;
  }
  if (button.dataset.action === "preview-image-material") {
    const url = await materialContentLoader.load(state.activeTaskId, materialId);
    return openImageLightbox(url, material?.relative_path || "图片材料");
  }
  if (button.dataset.action === "open-material") {
    // f18:材料「查看」→ 右侧文件面板(不再全屏替换)
    if (!material) return setStatus("材料不存在", true);
    if (!pluginViewForMaterial(material)) return setStatus("当前没有启用的材料查看器", true);
    state.filesPanel = { ...material };
    render();
  }
  if (button.dataset.action === "save-material") {
    const body = Object.fromEntries([...row.querySelectorAll("select[data-field]")].map(select => [select.dataset.field, select.value]));
    try {
      const saved = await api(`/desktop/api/materials/${materialId}`, { method: "PUT", body: JSON.stringify(body) });
      Object.assign(material, saved); refreshMaterialView(taskId, true);
    } catch (error) {
      if (error.status === 409 && error.detail?.code === "git_init_required" && confirm("该文件夹还不是 Git 仓库。是否执行 git init 并启用不可遗失保护？")) {
        const saved = await api(`/desktop/api/materials/${materialId}`, { method: "PUT", body: JSON.stringify({ ...body, confirm_git_init: true }) });
        Object.assign(material, saved); refreshMaterialView(taskId, true);
      } else setStatus(error.message, true);
    }
  }
  if (button.dataset.action === "clear-material" && confirm("保留文件路径并将内容置空？此操作会保存新版本。")) {
    Object.assign(material, await api(`/desktop/api/materials/${materialId}/clear`, { method: "POST" }));
    state.materialSelections.set(
      taskId,
      runMaterialPicker.normalizeSelection(materialSelection(taskId), state.materials.get(taskId))
    );
    refreshMaterialView(taskId, true);
  }
  if (button.dataset.action === "delete-material" && confirm("删除这个可移除文件？")) {
    await api(`/desktop/api/materials/${materialId}`, { method: "DELETE" });
    materialContentLoader.releaseMaterial(taskId, materialId);
    state.materials.set(taskId, state.materials.get(taskId).filter(item => item.material_id !== materialId));
    state.materialSelections.set(
      taskId,
      runMaterialPicker.normalizeSelection(materialSelection(taskId), state.materials.get(taskId))
    );
    refreshMaterialView(taskId, true);
  }
  if (button.dataset.action === "load-versions") {
    const versions = await api(`/desktop/api/materials/${materialId}/versions`);
    row.querySelector("[data-versions]").innerHTML = versions.map(version => `<li><span>${escapeHtml(version.source)} · ${escapeHtml(version.created_at || "")}</span><button class="text-button" data-action="view-version" data-version-id="${escapeHtml(version.version_id)}">查看此历史版本</button><button class="text-button" data-action="restore-version" data-version-id="${version.version_id}">恢复</button></li>`).join("") || "<li>尚无版本</li>";
  }
  if (button.dataset.action === "view-version") {
    return openFileLink({ path: material.relative_path, version_id: button.dataset.versionId, view: "history" }, taskId);
  }
  if (button.dataset.action === "restore-version") {
    Object.assign(material, await api(`/desktop/api/materials/${materialId}/restore`, { method: "POST", body: JSON.stringify({ version_id: button.dataset.versionId }) })); refreshMaterialView(taskId, true);
  }
  } catch (error) {
    setStatus(error.message, true);
  }
}

async function refreshMaterialOrganization() {
  const taskId = state.activeTaskId;
  const [materials, groups, history] = await Promise.all([
    api(`/desktop/api/tasks/${taskId}/materials`),
    api(`/desktop/api/tasks/${taskId}/material-groups`),
    api(`/desktop/api/tasks/${taskId}/material-history`),
  ]);
  state.materials.set(taskId, materials);
  state.materialGroups.set(taskId, groups);
  state.materialHistory.set(taskId, history);
  state.materialSelections.set(taskId, runMaterialPicker.normalizeSelection(materialSelection(taskId), materials));
  refreshMaterialView(taskId);
}

async function moveMaterialMembership(materialId, groupId) {
  const group = (state.materialGroups.get(state.activeTaskId) || []).find(item => item.group_id === groupId);
  await api(`/desktop/api/tasks/${state.activeTaskId}/materials/${materialId}/group`, {
    method: "PUT",
    body: JSON.stringify({ group_id: groupId, position: group?.memberships?.length || 0 }),
  });
  await refreshMaterialOrganization();
}

async function dropMaterialMembership(materialId, groupId, beforeMaterialId) {
  const group = (state.materialGroups.get(state.activeTaskId) || []).find(item => item.group_id === groupId);
  const ordered = (group?.memberships || []).map(item => item.material_id).filter(id => id !== materialId);
  const target = beforeMaterialId ? ordered.indexOf(beforeMaterialId) : -1;
  await api(`/desktop/api/tasks/${state.activeTaskId}/materials/${materialId}/group`, {
    method: "PUT",
    body: JSON.stringify({ group_id: groupId, position: target < 0 ? ordered.length : target }),
  });
  await refreshMaterialOrganization();
}

async function createMaterialGroup() {
  const name = prompt("新分组名称");
  if (name === null || !name.trim()) return;
  await api(`/desktop/api/tasks/${state.activeTaskId}/material-groups`, {
    method: "POST", body: JSON.stringify({ name: name.trim() }),
  });
  await refreshMaterialOrganization();
}

async function renameMaterialGroup(groupId) {
  const group = (state.materialGroups.get(state.activeTaskId) || []).find(item => item.group_id === groupId);
  if (!group) return;
  const name = prompt("新的分组名称", group.name);
  if (name === null || !name.trim() || name.trim() === group.name) return;
  await api(`/desktop/api/tasks/${state.activeTaskId}/material-groups/${groupId}`, {
    method: "PUT", body: JSON.stringify({ name: name.trim() }),
  });
  await refreshMaterialOrganization();
}

async function deleteMaterialGroup(groupId) {
  const group = (state.materialGroups.get(state.activeTaskId) || []).find(item => item.group_id === groupId);
  if (!group || !confirm(`删除分组“${group.name}”？材料将恢复自动归类，材料本身和历史不会删除。`)) return;
  await api(`/desktop/api/tasks/${state.activeTaskId}/material-groups/${groupId}?confirm=true`, { method: "DELETE" });
  await refreshMaterialOrganization();
}

async function moveMaterialGroupOrder(groupId, direction) {
  const groups = [...(state.materialGroups.get(state.activeTaskId) || [])].sort((a, b) => a.position - b.position);
  const index = groups.findIndex(item => item.group_id === groupId);
  const target = index + Number(direction);
  if (index < 0 || target < 0 || target >= groups.length) return;
  [groups[index], groups[target]] = [groups[target], groups[index]];
  await api(`/desktop/api/tasks/${state.activeTaskId}/material-groups/order`, {
    method: "PUT", body: JSON.stringify({ group_ids: groups.map(item => item.group_id) }),
  });
  await refreshMaterialOrganization();
}

async function switchTask(taskId) {
  const requestId = ++taskSwitchSequence;
  if (state.view !== "focus") { state.inspector.open = true; state.inspector.tab = "materials"; }
  cancelPendingViewRequests();
  if (state.view === "focus") persistFocusState();
  const previousTaskId = state.activeTaskId;
  if (previousTaskId && previousTaskId !== taskId) materialContentLoader.releaseTask(previousTaskId);
  releaseFileLinkPreview();
  state.filesPanel = null;
  state.activeTaskId = taskId;
  state.view = "focus";
  render();
  await hydrateActive(taskId, { reusePending: true });
  if (requestId !== taskSwitchSequence || state.activeTaskId !== taskId || state.view !== "focus") return;
  const projectionStatus = state.details.get(taskId)?.context?.projection_status || "root";
  if (!["root", "valid", "repaired", "approved"].includes(projectionStatus)) return reopenContextDecision(taskId);
}

async function goFocusHome() {
  // 若当前任务是「无工作区模式」任务，点击「任务」默认回到最近的工作区任务
  if (activeTask()?.harness_mode === "assembly") {
    const recent = [...state.tasks].reverse().find(task => task.harness_mode !== "assembly");
    if (recent) return switchTask(recent.task_id);
    persistFocusState(); state.activeTaskId = null; state.view = "focus"; return render();
  }
  const taskId = state.activeTaskId;
  if (!taskId) return;
  if (state.view === "focus") persistFocusState();
  cancelPendingViewRequests();
  state.inspector.open = true;
  state.inspector.tab = "materials";
  state.view = "focus";
  render();
  await hydrateActive(taskId, { reusePending: true });
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
    if (settingsDialog?.open) void window.FocusSurfaceTransition.visible(settingsDialog, false, { modal: true });
    return refreshAfterSessionChange();
  } catch (error) { return setStatus(error.message, true); }
}

async function refreshAfterSessionChange() {
  const ready = await bootstrap();
  if (ready && state.view === "map" && state.mapViewMode === "graph") await hydrateMapPortfolio(mapPortfolio.workspaceId, true);
  return ready;
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
  if (state.view === "map" && ["tree", "graph"].includes(state.mapViewMode)) {
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

// === 模型设置面板：读取 / 编辑 / 保存 / 测试连接 / 删除 ===
// 草稿与快照放在模块级对象而不是 state：设置对话框在每次 render() 时会被重新打开，
// 未保存的编辑必须活过重渲染。
const modelSettings = {
  snapshot: null,
  error: null,
  draft: null,        // { index }：正在编辑/新增的条目在当前目录中的下标
  errors: {},         // 后端返回的字段级错误（键形如 models[0].base_url）
  test: null,         // { state: "running" | "ok" | "fail", reason }
  credentialDrafts: {}, // 按来源变量保留未保存密钥；只保存在内存，任何响应都不回填
  dirty: false,
  saving: false,
  setupChecked: false,
  setupMode: false,
  advanced: false,
  progress: null,
  onboarding: false,
  loading: null,
};

const MODEL_NUMERIC_FIELDS = ["context_window", "curation_max_output_tokens"];
const MODEL_FLAG_FIELDS = ["default", "curation_default", "supports_image_input"];

function modelCredentialVariable(entry) {
  // 与后端 match_env_ref 一致；$VAR 和 ${VAR} 共用同一份草稿。
  const match = String(entry?.api_key || "").match(/^\$([A-Z_][A-Z0-9_]*)$|^\$\{([A-Z_][A-Z0-9_]*)\}$/);
  return match ? match[1] || match[2] : "";
}

function modelCredentialValue(entry = modelSettings.snapshot?.models?.[modelSettings.draft?.index]) {
  return modelSettings.credentialDrafts[modelCredentialVariable(entry)] || "";
}

function updateModelCredential(value) {
  const entry = modelSettings.snapshot?.models?.[modelSettings.draft?.index];
  let variable = modelCredentialVariable(entry);
  if (!variable && modelSettings.setupMode && entry) {
    entry.api_key = "$FOCUS_SETUP_API_KEY";
    variable = modelCredentialVariable(entry);
  }
  if (!variable) return;
  if (value) modelSettings.credentialDrafts[variable] = value;
  else delete modelSettings.credentialDrafts[variable];
  modelSettings.dirty = true;
  modelSettings.test = null;
}

function pendingModelCredentials() {
  const variables = new Set((modelSettings.snapshot?.models || []).map(modelCredentialVariable));
  return Object.fromEntries(Object.entries(modelSettings.credentialDrafts)
    .filter(([variable, value]) => variable && variables.has(variable) && value));
}

function modelEntryPayload(entry) {
  const fields = modelSettings.snapshot?.editable_fields || [];
  const payload = {};
  fields.forEach(field => { payload[field] = entry[field] ?? null; });
  if (fields.includes("derivation_max_output_tokens")) payload.derivation_max_output_tokens = entry.derivation_max_output_tokens || {};
  MODEL_FLAG_FIELDS.forEach(field => { payload[field] = Boolean(entry[field]); });
  return payload;
}

function modelEntrySource(entry) {
  if (entry.source === "user_added") return uiText("settings.model_source_user_added", "你新增的");
  if (entry.source === "user_modified") return uiText("settings.model_source_user_modified", "已由你修改");
  return uiText("settings.model_source_shipped", "发行自带");
}

function modelCredentialState(entry) {
  if (modelCredentialValue(entry)) return uiText("settings.model_key_pending", "密钥待保存");
  if (!entry.api_key_set && !entry.api_key_literal && !entry.default && !entry.curation_default
      && entry.name !== modelSettings.snapshot?.default_model_override) {
    return uiText("settings.model_key_optional", "未配置（可选模板）");
  }
  if (entry.api_key_placeholder) return uiText("settings.model_placeholder_key", "占位密钥，请填写真实 API Key");
  if (entry.api_key_literal) return uiText("settings.model_key_literal", "已内联字面量（不建议）");
  if (entry.api_key_set ?? Boolean(entry.api_key)) return uiText("settings.model_key_set", "已设置");
  return uiText("settings.model_key_unset", "未设置");
}

function modelFieldError(field) {
  if (modelSettings.draft) {
    const indexed = modelSettings.errors[`models[${modelSettings.draft.index}].${field}`];
    if (indexed) return indexed;
  }
  return modelSettings.errors[field] || "";
}

function modelEntryRow(entry, index) {
  const badges = [
    entry.default ? `<span class="model-badge is-default">${uiText("settings.model_field_default", "默认模型")}</span>` : "",
    entry.curation_default ? `<span class="model-badge">${uiText("settings.model_field_curation_default", "策展默认模型")}</span>` : "",
    entry.supports_image_input ? `<span class="model-badge">${uiText("settings.model_field_supports_image", "支持图像输入")}</span>` : "",
  ].join("");
  return `<div class="settings-model-row${modelSettings.draft?.index === index ? " is-editing" : ""}" data-model-index="${index}">
    <div class="settings-model-info">
      <div class="settings-model-title"><strong>${escapeHtml(entry.display_name || entry.name || "")}</strong>${badges}</div>
      <div class="muted tiny">${escapeHtml(entry.name || "")} · ${escapeHtml(entry.model || "")} · ${escapeHtml(entry.base_url || "")}</div>
      <div class="muted tiny">${escapeHtml(entry.effective_provider || entry.provider || "")} · ${escapeHtml(entry.effective_protocol || entry.protocol || "")}</div>
      <div class="muted tiny">${escapeHtml(modelEntrySource(entry))} · ${uiText("settings.model_field_context_window", "上下文窗口")}: ${escapeHtml(String(entry.context_window ?? uiText("settings.model_unknown", "未知")))} · ${uiText("settings.model_field_api_key", "密钥来源变量")}: ${escapeHtml(modelCredentialState(entry))}</div>
    </div>
    <div class="settings-model-row-actions">
      <button class="text-button" data-action="model-edit" data-model-index="${index}">${uiText("settings.model_edit", "编辑")}</button>
      <button class="text-button danger" data-action="model-remove" data-model-index="${index}">${uiText("settings.model_remove", "删除")}</button>
    </div>
  </div>`;
}

function modelTextField(label, field, entry, options = {}) {
  const error = modelFieldError(field);
  const value = entry[field] ?? "";
  const control = options.select
    ? `<select data-model-field="${field}">${options.select.map(option => `<option value="${escapeHtml(option.value)}" ${option.value === value ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("")}</select>`
    : `<input type="${options.type || "text"}" data-model-field="${field}" value="${escapeHtml(String(value))}" ${options.list ? `list="${options.list}"` : ""} ${options.placeholder ? `placeholder="${escapeHtml(options.placeholder)}"` : ""}>`;
  return `<label class="settings-model-field${error ? " has-error" : ""}">${escapeHtml(label)}${control}${error ? `<span class="field-error">${escapeHtml(error)}</span>` : ""}</label>`;
}

function modelFlagField(label, field, entry) {
  return `<label class="settings-model-flag"><input type="checkbox" data-model-field="${field}" ${entry[field] ? "checked" : ""}>${escapeHtml(label)}</label>`;
}

function renderModelDraft() {
  const entry = modelSettings.snapshot.models[modelSettings.draft.index];
  if (!entry) return "";
  const adapters = (modelSettings.snapshot.adapters || []).map(adapter => ({ value: adapter.use, label: adapter.label }));
  const methods = [{ value: "", label: uiText("settings.model_curation_method_none", "不用于策展") }]
    .concat((modelSettings.snapshot.curation_output_methods || []).map(name => ({ value: name, label: name })));
  const simple = modelSettings.setupMode;
  const credential = `<label class="settings-model-field${modelFieldError("api_key") ? " has-error" : ""}">${simple ? "API Key" : uiText("settings.model_field_key_value", "密钥值")} · ${escapeHtml(modelCredentialState(entry))}
    <input type="password" data-model-credential value="${escapeHtml(modelCredentialValue(entry))}" autocomplete="off" placeholder="${escapeHtml(simple && !entry.api_key_set ? uiText("settings.model_setup_key_placeholder", "请输入服务商提供的 API Key") : uiText("settings.model_key_placeholder", "留空表示不修改"))}">
    ${modelFieldError("api_key") ? `<span class="field-error">${escapeHtml(modelFieldError("api_key"))}</span>` : ""}
  </label>`;
  return `<section class="settings-model-editor">
    <header><strong>${simple ? uiText("settings.model_connect", "连接模型") : uiText(modelSettings.draft.isNew ? "settings.model_add" : "settings.model_edit", modelSettings.draft.isNew ? "新增模型" : "编辑")}</strong>${simple ? "" : `<span class="muted tiny">${escapeHtml(entry.name || "")}</span>`}</header>
    ${simple ? `<div class="settings-model-grid is-simple">
      <label class="settings-model-field">${uiText("settings.model_service", "服务商")}<select data-model-service>${modelServiceOptions(entry).map(option => `<option value="${escapeHtml(option.id)}" ${option.selected ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("")}</select></label>
      ${credential}
      ${modelTextField(uiText("settings.model_field_model", "模型标识"), "model", entry, { list: "modelSetupSuggestions" })}
      <datalist id="modelSetupSuggestions">${(modelSettings.snapshot.shipped_models || []).filter(model => model.base_url === entry.base_url).map(model => `<option value="${escapeHtml(model.model)}"></option>`).join("")}</datalist>
    </div><p class="muted tiny">${uiText("settings.model_shared_default", "普通调用与策展使用同一个模型。其他设置可在高级配置中调整。")}</p>
    <button class="text-button" data-action="model-advanced" aria-expanded="${modelSettings.advanced}">${uiText("settings.model_advanced", "高级配置")}</button>` : ""}
    <div ${simple && !modelSettings.advanced ? "hidden" : ""}>
    <div class="settings-model-grid">
      ${modelTextField(uiText("settings.model_field_name", "条目名（查表键）"), "name", entry)}
      ${modelTextField(uiText("settings.model_field_display_name", "显示名"), "display_name", entry)}
      ${simple ? "" : modelTextField(uiText("settings.model_field_model", "模型标识"), "model", entry)}
      ${modelTextField(uiText("settings.model_field_use", "适配器"), "use", entry, { select: adapters })}
      ${modelTextField("Provider", "provider", entry, { select: [{ value: "", label: "沿用旧适配器映射" }, { value: "openai", label: "OpenAI" }, { value: "deepseek", label: "DeepSeek" }] })}
      ${modelTextField("API 协议", "protocol", entry, { select: [{ value: "", label: "沿用旧适配器映射" }, { value: "responses", label: "Responses" }, { value: "chat_completions", label: "Chat Completions（兼容）" }] })}
      ${modelTextField(uiText("settings.model_field_base_url", "端点"), "base_url", entry)}
      ${modelTextField(uiText("settings.model_field_context_window", "上下文窗口"), "context_window", entry, { type: "number" })}
      ${modelTextField(uiText("settings.model_field_curation_method", "策展输出方式"), "curation_output_method", entry, { select: methods })}
      ${modelTextField(uiText("settings.model_field_curation_tokens", "策展输出上限"), "curation_max_output_tokens", entry, { type: "number" })}
      <label class="settings-model-field${modelFieldError("api_key") ? " has-error" : ""}">${uiText("settings.model_field_api_key", "密钥来源变量")}
        <input type="text" data-model-field="api_key" value="${escapeHtml(String(entry.api_key || ""))}" placeholder="$OPENAI_API_KEY">
        ${modelFieldError("api_key") ? `<span class="field-error">${escapeHtml(modelFieldError("api_key"))}</span>` : ""}
      </label>
      ${simple ? "" : credential}
    </div>
    <div class="settings-model-flags">
      ${modelFlagField(uiText("settings.model_field_default", "默认模型"), "default", entry)}
      ${modelFlagField(uiText("settings.model_field_curation_default", "策展默认模型"), "curation_default", entry)}
      ${modelFlagField(uiText("settings.model_field_supports_image", "支持图像输入"), "supports_image_input", entry)}
    </div>
    </div>
    <div class="settings-model-editor-actions" ${simple && !modelSettings.advanced ? "hidden" : ""}>
      <button class="text-button" data-action="model-test">${modelSettings.test?.state === "running" ? uiText("settings.model_testing", "正在测试…") : uiText("settings.model_test", "测试连接")}</button>
      ${simple ? "" : `<button class="text-button" data-action="model-cancel">${uiText("settings.model_cancel", "取消")}</button>`}
    </div>
  </section>`;
}

// 服务商选项从发行目录提取；自定义服务通过高级配置填写地址与协议。
function modelServiceOptions(entry) {
  const templates = modelSettings.snapshot.shipped_models || [];
  const options = templates.filter((item, index) => templates.findIndex(other => other.base_url === item.base_url && other.provider === item.provider) === index)
    .map((item, index) => ({ id: String(index), label: ({ deepseek: "DeepSeek", openai: "OpenAI" })[item.provider] || item.base_url || item.display_name, template: item,
      selected: entry.base_url === item.base_url && entry.provider === item.provider }));
  options.push({ id: "openai", label: "OpenAI", template: {
    use: "langchain_openai:ChatOpenAI", provider: "openai", protocol: "chat_completions",
    base_url: "https://api.openai.com/v1", model: "", context_window: null,
    curation_output_method: "prompt_json", curation_max_output_tokens: 8192, supports_image_input: false,
  }, selected: !options.some(item => item.selected) && entry.base_url === "https://api.openai.com/v1" });
  options.push({ id: "custom", label: uiText("settings.model_custom_service", "其他 OpenAI 兼容服务（自定义）"), selected: !options.some(item => item.selected) });
  return options;
}

function updateModelService(value) {
  if (modelSettings.saving) return;
  const entry = modelSettings.snapshot?.models?.[modelSettings.draft?.index];
  if (!entry) return;
  const option = modelServiceOptions(entry).find(item => item.id === value);
  if (option?.template) {
    const fields = ["use", "provider", "protocol", "base_url", "model", "context_window", "curation_output_method", "curation_max_output_tokens", "supports_image_input"];
    fields.forEach(field => { entry[field] = option.template[field] ?? null; });
  } else {
    Object.assign(entry, { use: "langchain_openai:ChatOpenAI", provider: "openai", protocol: "chat_completions", base_url: "", model: "", context_window: null, curation_output_method: "prompt_json", supports_image_input: false });
    modelSettings.advanced = true;
  }
  modelSettings.dirty = true;
  renderModelSettings();
}

const MODEL_SETUP_CHECKS = [
  ["chat", "普通调用", "能否正常理解和回答"],
  ["curation", "策展输出", "能否整理和派生上下文"],
  ["semantic_index", "自动索引", "能否为后续工作检索上下文"],
];

function renderModelVerification() {
  const verification = modelSettings.progress || modelSettings.snapshot?.verification;
  if (!verification && !modelSettings.setupMode && !modelSettings.saving) return "";
  const checks = verification?.checks || [];
  const labels = { pending: "等待中", running: "验证中", passed: "已通过", failed: "失败", interrupted: "未完成" };
  return `<section class="settings-model-verification" aria-label="${uiText("settings.model_checks", "模型验证")}">
    <ol>${MODEL_SETUP_CHECKS.map(([capability, label, description]) => {
      const check = checks.find(item => item.capability === capability);
      const status = check?.status || (check ? (check.ok ? "passed" : "failed") : "pending");
      return `<li data-model-check="${capability}" data-check-status="${status}">
        <div><strong>${uiText(`settings.model_check_${capability}`, label)}</strong><p class="muted tiny">${uiText(`settings.model_check_${capability}_help`, description)}</p>
        ${check?.reason ? `<p class="field-error">${escapeHtml(check.reason)}</p>` : ""}</div>
        <span role="status" aria-label="${uiText(`settings.model_check_${capability}`, label)}: ${uiText(`settings.model_check_${status}`, labels[status] || status)}">${uiText(`settings.model_check_${status}`, labels[status] || status)}</span>
        ${status === "failed" ? `<button class="text-button" data-action="model-repair" data-capability="${capability}" ${modelSettings.saving ? "disabled" : ""}>${uiText("settings.model_repair", "修复此项")}</button>` : ""}
      </li>`;
    }).join("")}</ol>
    ${verification && !modelSettings.saving ? `<button class="text-button" data-action="model-verify" ${modelSettings.dirty ? "disabled" : ""}>${uiText("settings.model_retry", "重新验证")}</button>` : ""}
    ${verification?.ok && !modelSettings.saving && !modelSettings.dirty && modelSettings.setupMode ? `<button class="primary" data-action="model-start">${uiText("settings.model_start", "开始第一个项目")}</button>` : ""}
  </section>`;
}

function repairModelCheck(capability) {
  const verification = modelSettings.snapshot?.verification;
  const name = verification?.checks?.find(check => check.capability === capability)?.model;
  const index = modelSettings.snapshot.models.findIndex(entry => entry.name === name || (!name && (capability === "chat" ? entry.default : entry.curation_default)));
  if (index >= 0) startModelDraft(index);
  modelSettings.advanced = true;
  renderModelSettings();
}

function renderModelSettings() {
  const host = document.querySelector("#modelSettingsHost");
  if (!host) return;
  const snapshot = modelSettings.snapshot;
  if (!snapshot) {
    host.innerHTML = `<p class="muted tiny settings-model-empty">${escapeHtml(modelSettings.error || "")}</p>`;
    if (modelSettings.onboarding) host.innerHTML += `<button class="primary" data-action="model-setup-retry">${uiText("settings.model_setup_retry", "重新读取模型配置")}</button>`;
    return;
  }

  const banners = [];
  const verification = modelSettings.progress || snapshot.verification;
  if (modelSettings.onboarding) {
    banners.push(`<p role="status" class="settings-model-banner">${uiText("settings.model_setup_steps", "首次使用：配置模型 → 验证可用 → 选择项目文件夹 → 输入第一个目标。保存并验证通过后，点击“开始第一个项目”继续。")}</p>`);
    if (!modelSettings.setupMode && !snapshot.setup_required && snapshot.verification?.ok === true && !modelSettings.dirty && !modelSettings.saving) {
      banners.push(`<button class="primary" data-action="model-setup-continue">${uiText("settings.model_setup_continue", "继续：选择项目文件夹")}</button>`);
    }
  }
  if (snapshot.setup_required) {
    banners.push(`<p class="settings-model-banner is-warn">${escapeHtml(uiText("settings.model_setup_help", "选择服务商，填写 API Key 和模型，然后验证并开始。"))}</p>`);
    banners.push(`<ul class="settings-model-errors">${Object.values(snapshot.setup_issues || {}).map(message => `<li>${escapeHtml(message)}</li>`).join("")}</ul>`);
  }
  if (modelSettings.saving) {
    banners.push(`<p role="status" class="settings-model-banner">${escapeHtml(uiText("settings.model_verifying", "正在验证模型能力，请查看各项进度…"))}</p>`);
  } else if (verification) {
    const reason = verification.reason || uiText("settings.model_incomplete", "验证未完成，请重新验证");
    banners.push(`<p role="status" class="settings-model-banner ${verification.ok ? "is-ok" : "is-error"}">${escapeHtml(verification.status === "interrupted" ? reason : uiText("settings.model_verified", "设置已保存。{reason}", { reason }))}</p>`);
  }
  if (snapshot.default_error) {
    banners.push(`<p class="settings-model-banner is-error">${escapeHtml(uiText("settings.model_default_error", "默认模型无法解析：{message}", { message: snapshot.default_error }))}</p>`);
  }
  if (snapshot.default_model_override) {
    banners.push(`<p class="settings-model-banner is-warn">${escapeHtml(uiText("settings.model_env_override", "环境变量 FOCUS_MODEL 正在覆盖默认模型：{name}；它优先于这里的默认选择。", { name: snapshot.default_model_override }))}</p>`);
  }
  if (modelSettings.test) {
    const ok = modelSettings.test.state === "ok";
    const running = modelSettings.test.state === "running";
    const text = running
      ? uiText("settings.model_testing", "正在测试…")
      : ok ? uiText("settings.model_test_ok", "连接成功")
        : uiText("settings.model_test_fail", "连接失败：{reason}", { reason: modelSettings.test.reason || "" });
    banners.push(`<p class="settings-model-banner${ok ? " is-ok" : running ? "" : " is-error"}">${escapeHtml(text)}</p>`);
  }
  if (modelSettings.dirty) {
    banners.push(`<p class="settings-model-banner is-warn">${escapeHtml(uiText("settings.model_dirty", "有未保存的改动"))}</p>`);
  }
  if (snapshot.removed_models?.length) {
    banners.push(`<p class="muted tiny">${escapeHtml(uiText("settings.model_removed_note", "已删除：{names}", { names: snapshot.removed_models.join(", ") }))}</p>`);
  }
  const errorKeys = Object.keys(modelSettings.errors);
  if (errorKeys.length) {
    banners.push(`<ul class="settings-model-errors">${errorKeys.map(key => `<li>${escapeHtml(`${key}: ${modelSettings.errors[key]}`)}</li>`).join("")}</ul>`);
  }

  const rows = snapshot.models.length
    ? snapshot.models.map((entry, index) => modelEntryRow(entry, index)).join("")
    : `<p class="muted tiny settings-model-empty">${escapeHtml(uiText("settings.model_empty", "当前没有可用模型条目，请新增一个。"))}</p>`;

  host.innerHTML = `
    ${banners.join("")}
    <fieldset class="settings-model-controls" ${modelSettings.saving ? "disabled" : ""}>
    ${modelSettings.setupMode && modelSettings.draft ? renderModelDraft() : ""}
    <div class="settings-model-actions">
      ${!modelSettings.setupMode || modelSettings.advanced ? `<button class="text-button" data-action="model-add">${uiText("settings.model_add", "新增模型")}</button>
      <button class="text-button" data-action="model-reset">${uiText("settings.model_reset", "恢复发行默认")}</button>` : ""}
      <button class="primary" data-action="model-save" ${modelSettings.saving ? "disabled" : ""}>${modelSettings.setupMode ? uiText("settings.model_validate_start", "验证并开始") : uiText("settings.model_save", "保存并立即生效")}</button>
    </div>
    <div class="settings-model-list" ${modelSettings.setupMode && !modelSettings.advanced ? "hidden" : ""}>${rows}</div>
    ${!modelSettings.setupMode && modelSettings.draft ? renderModelDraft() : ""}
    </fieldset>
    ${renderModelVerification()}`;
  interfaceI18n.apply(host);
}

async function loadModelSettings({ force = false } = {}) {
  if (modelSettings.snapshot && !force) return modelSettings.snapshot;
  if (!modelSettings.loading) {
    modelSettings.loading = api("/desktop/api/settings/models").then(snapshot => {
      modelSettings.snapshot = snapshot;
      modelSettings.error = null;
      modelSettings.errors = {};
      return snapshot;
    }).finally(() => { modelSettings.loading = null; });
  }
  return modelSettings.loading;
}

async function ensureModelSetup({ onboarding = false } = {}) {
  if (onboarding) {
    modelSettings.onboarding = true;
    selectSettingsTab("models");
    renderModelSettings();
    void openSettings();
  }
  if (modelSettings.setupChecked && !onboarding) return;
  let snapshot;
  try { snapshot = await loadModelSettings({ force: !modelSettings.setupChecked && !modelSettings.draft }); }
  catch (error) { modelSettings.error = error.message; renderModelSettings(); throw error; }
  modelSettings.setupChecked = true;
  if (!snapshot.setup_required && snapshot.verification?.ok !== false && !modelSettings.onboarding) return;
  modelSettings.setupMode = true;
  const firstSetup = !snapshot.models.some(entry => entry.api_key_set);
  selectSettingsTab("models");
  // 不等待归档会话或沙箱检查，让模型修复入口立即可见。
  void openSettings();
  if (modelSettings.draft) { renderModelSettings(); return; }
  modelSettings.advanced = false;
  const issueIndex = Object.keys(snapshot.setup_issues || {}).map(key => /^models\[(\d+)\]/.exec(key)).find(Boolean);
  const index = !firstSetup && issueIndex ? Number(issueIndex[1]) : snapshot.models.findIndex(entry => entry.default);
  if (index >= 0) startModelDraft(index);
  else if (snapshot.models.length) startModelDraft(0);
  else {
    addModelEntry();
    const entry = snapshot.models[modelSettings.draft.index];
    Object.assign(entry, { name: "first-model", display_name: "My model", curation_output_method: "prompt_json" });
    updateModelService("openai");
  }
  const entry = snapshot.models[modelSettings.draft.index];
  if (firstSetup) {
    snapshot.models.forEach(model => { model.default = model === entry; model.curation_default = model === entry; });
    entry.curation_output_method ||= "prompt_json";
    modelSettings.dirty = true;
  }
  renderModelSettings();
}

async function finishDesktopModelSetup() {
  if (!modelSettings.onboarding || modelSettings.dirty || modelSettings.snapshot?.setup_required
      || modelSettings.snapshot?.verification?.ok !== true) return;
  if (await desktopPatrol?.completeModelSetup()) {
    modelSettings.onboarding = false;
    void window.FocusSurfaceTransition.visible(settingsDialog, false, { modal: true });
  }
}

function startModelDraft(index, isNew = false) {
  const entry = modelSettings.snapshot?.models?.[index];
  modelSettings.draft = {
    index, isNew, before: entry ? JSON.parse(JSON.stringify(entry)) : null,
    beforeFlags: modelSettings.snapshot.models.map(model => ({ default: model.default, curation_default: model.curation_default })),
    beforeCredentials: { ...modelSettings.credentialDrafts },
  };
  modelSettings.errors = {};
  modelSettings.test = null;
  renderModelSettings();
}

function cancelModelDraft() {
  const draft = modelSettings.draft;
  if (!draft) return;
  if (draft.isNew) modelSettings.snapshot.models.splice(draft.index, 1);
  else if (draft.before) modelSettings.snapshot.models[draft.index] = draft.before;
  modelSettings.snapshot.models.forEach((entry, index) => Object.assign(entry, draft.beforeFlags?.[index] || {}));
  modelSettings.credentialDrafts = draft.beforeCredentials || {};
  modelSettings.draft = null;
  modelSettings.errors = {};
  modelSettings.test = null;
  renderModelSettings();
}

function addModelEntry() {
  const template = (modelSettings.snapshot.shipped_models || [])[0] || {};
  const entry = {
    name: "", display_name: "", use: template.use || ((modelSettings.snapshot.adapters || [])[0]?.use ?? ""),
    model: "", api_key: template.api_key || "$OPENAI_API_KEY", base_url: template.base_url || "",
    provider: template.provider ?? null, protocol: template.protocol ?? null,
    context_window: template.context_window ?? null, curation_output_method: template.curation_output_method ?? null,
    curation_max_output_tokens: template.curation_max_output_tokens ?? 8192,
    curation_default: false, default: false, supports_image_input: false, source: "user_added",
  };
  modelSettings.snapshot.models.push(entry);
  modelSettings.dirty = true;
  startModelDraft(modelSettings.snapshot.models.length - 1, true);
}

function removeModelEntry(index) {
  modelSettings.snapshot.models.splice(index, 1);
  modelSettings.credentialDrafts = pendingModelCredentials();
  modelSettings.draft = null;
  modelSettings.dirty = true;
  modelSettings.errors = {};
  renderModelSettings();
}

function restoreShippedModels() {
  if (!confirm(uiText("settings.model_reset", "恢复发行默认"))) return;
  modelSettings.snapshot.models = (modelSettings.snapshot.shipped_models || []).map(entry => ({ ...entry, source: "shipped" }));
  modelSettings.credentialDrafts = {};
  modelSettings.draft = null;
  modelSettings.dirty = true;
  modelSettings.errors = {};
  renderModelSettings();
}

function updateModelField(target) {
  if (modelSettings.saving) return;
  const entry = modelSettings.snapshot?.models?.[modelSettings.draft?.index];
  if (!entry) return;
  const field = target.dataset.modelField;
  if (MODEL_FLAG_FIELDS.includes(field)) {
    if (target.checked && ["default", "curation_default"].includes(field)) {
      modelSettings.snapshot.models.forEach(model => { model[field] = false; });
    }
    entry[field] = target.checked;
    modelSettings.dirty = true;
    renderModelSettings();
    return;
  }
  let value = target.value;
  if (MODEL_NUMERIC_FIELDS.includes(field)) {
    const trimmed = String(value).trim();
    value = trimmed === "" ? null : (Number.isNaN(Number(trimmed)) ? value : Number(trimmed));
  }
  if (["provider", "protocol"].includes(field) && value === "") value = null;
  entry[field] = value;
  if (field === "api_key") {
    const input = document.querySelector("[data-model-credential]");
    if (input) input.value = modelCredentialValue(entry);
  }
  modelSettings.dirty = true;
  document.querySelectorAll('[data-action="model-start"], [data-action="model-verify"]').forEach(button => { button.disabled = true; });
  // 新的输入需要新一轮验证，旧的成功结果不能成为开始入口。
  modelSettings.progress = null;
}

function pollModelVerification(verificationId) {
  let polling = false;
  let stopped = false;
  const timer = setInterval(async () => {
    if (polling || stopped) return;
    polling = true;
    try {
      const result = await api(`/desktop/api/settings/models/verification?verification_id=${encodeURIComponent(verificationId)}`);
      if (!stopped && result.verification?.verification_id === verificationId && JSON.stringify(result.verification) !== JSON.stringify(modelSettings.progress)) {
        modelSettings.progress = result.verification;
        renderModelSettings();
      }
    } catch { /* 临时查询失败不替代保存请求的最终结果。 */ }
    finally { polling = false; }
  }, 500);
  return () => { stopped = true; clearInterval(timer); };
}

async function reverifyModelSettings() {
  if (modelSettings.saving || modelSettings.dirty) return;
  const verificationId = crypto.randomUUID();
  modelSettings.saving = true;
  modelSettings.progress = { status: "running", checks: [] };
  renderModelSettings();
  const stopPolling = pollModelVerification(verificationId);
  try {
    modelSettings.snapshot = await api("/desktop/api/settings/models/verify", { method: "POST", body: JSON.stringify({ verification_id: verificationId }) });
    modelSettings.progress = null;
  } catch (error) {
    modelSettings.progress = interruptedModelVerification();
    setStatus(error.message, true);
  } finally {
    stopPolling();
    modelSettings.saving = false;
    renderModelSettings();
  }
}

function interruptedModelVerification() {
  return { status: "interrupted", ok: false, reason: uiText("settings.model_incomplete", "验证未完成，请重新验证"), checks: MODEL_SETUP_CHECKS.map(([capability]) => {
    const check = modelSettings.progress?.checks?.find(item => item.capability === capability);
    return check && ["passed", "failed"].includes(check.status) ? check : { capability, status: "interrupted" };
  }) };
}

async function putModelSettings(payload, acknowledge = false) {
  const body = { ...payload };
  if (acknowledge) body.acknowledge_references = true;
  return api("/desktop/api/settings/models", { method: "PUT", body: JSON.stringify(body) });
}

async function saveModelSettings() {
  if (!modelSettings.snapshot || modelSettings.saving) return;
  modelSettings.saving = true;
  modelSettings.errors = {};
  modelSettings.progress = { status: "running", checks: [] };
  setStatus(uiText("settings.model_verifying", "正在验证模型能力，请查看各项进度…"));
  const verificationId = crypto.randomUUID();
  const payload = { models: modelSettings.snapshot.models.map(modelEntryPayload), verification_id: verificationId };
  const credentials = pendingModelCredentials();
  if (Object.keys(credentials).length) payload.credentials = credentials;
  renderModelSettings();
  const stopPolling = pollModelVerification(verificationId);
  try {
    let snapshot;
    try {
      snapshot = await putModelSettings(payload);
    } catch (error) {
      const references = error.status === 409 ? error.detail?.references || {} : null;
      if (!references) throw error;
      const names = Object.keys(references).join(", ");
      if (!confirm(uiText("settings.model_referenced", "这些条目仍被任务或运行引用：{names}。仍要删除吗？", { names }))) {
        throw Object.assign(new Error(""), { silent: true });
      }
      snapshot = await putModelSettings(payload, true);
    }
    modelSettings.snapshot = snapshot;
    // 首配保留三项输入以便修复失败；成功落盘后清除全部凭据草稿。
    if (!modelSettings.setupMode) modelSettings.draft = null;
    else if (modelSettings.draft) {
      modelSettings.draft.before = JSON.parse(JSON.stringify(snapshot.models[modelSettings.draft.index]));
      modelSettings.draft.beforeCredentials = {};
      modelSettings.draft.beforeFlags = snapshot.models.map(entry => ({ default: entry.default, curation_default: entry.curation_default }));
    }
    modelSettings.progress = null;
    modelSettings.credentialDrafts = {};
    modelSettings.dirty = false;
    state.equipment = { ...state.equipment, models: snapshot.models };
    if (snapshot.verification) {
      setStatus(uiText("settings.model_verified", "设置已保存。{reason}", { reason: snapshot.verification.reason }), !snapshot.verification.ok);
    } else setStatus(uiText("settings.model_saved", "已保存并立即生效"));
    if (!modelSettings.setupMode) await finishDesktopModelSetup();
  } catch (error) {
    modelSettings.progress = interruptedModelVerification();
    if (error.silent) return;
    if (error.status === 422 && error.detail?.errors) {
      modelSettings.errors = error.detail.errors;
      modelSettings.advanced = true;
    }
    else setStatus(error.message, true);
  } finally {
    stopPolling();
    modelSettings.saving = false;
    renderModelSettings();
  }
}

async function testModelConnection() {
  const entry = modelSettings.snapshot?.models?.[modelSettings.draft?.index];
  if (!entry) return;
  modelSettings.test = { state: "running" };
  modelSettings.errors = {};
  renderModelSettings();
  try {
    const result = await api("/desktop/api/settings/models/test", {
      method: "POST",
      body: JSON.stringify({ entry: modelEntryPayload(entry), api_key_value: modelCredentialValue(entry) || undefined }),
    });
    modelSettings.test = { state: result.ok ? "ok" : "fail", reason: result.reason };
  } catch (error) {
    if (error.status === 422 && error.detail?.errors) {
      modelSettings.errors = error.detail.errors;
      modelSettings.test = null;
    } else {
      modelSettings.test = { state: "fail", reason: error.message };
    }
  }
  renderModelSettings();
}

let storageSettingsController = null;
function loadStorageSettings() {
  if (!window.FocusStorageSettings) return;
  storageSettingsController ||= window.FocusStorageSettings.create({
    api, host: document.querySelector("#storageSettingsHost"), escapeHtml,
    english: () => interfaceI18n.locale() === "en-US",
  });
  return storageSettingsController.load();
}

function selectSettingsTab(tab) {
  const selected = ["general", "models", "data"].includes(tab) ? tab : "general";
  document.querySelectorAll("[data-settings-tab]").forEach(button => {
    const active = button.dataset.settingsTab === selected;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll("[data-settings-pane]").forEach(pane => {
    pane.hidden = pane.dataset.settingsPane !== selected;
  });
  if (selected === "data") return runUiAction(loadStorageSettings);
  if (selected === "models") {
    return runUiAction(async () => {
      try { await loadModelSettings(); } catch (error) { modelSettings.error = error.message; }
      renderModelSettings();
    });
  }
  return undefined;
}

function syncLanguageControls() {
  const current = interfaceI18n.locale();
  document.querySelectorAll("[data-action='set-language']").forEach(button => {
    const active = button.dataset.locale === current;
    button.setAttribute("role", "radio");
    button.setAttribute("aria-checked", String(active));
  });
}

async function openSettings() {
  if (!settingsDialog?.open || settingsDialog.inert) void window.FocusSurfaceTransition.visible(settingsDialog, true, { modal: true, source: document.activeElement });
  try {
    const sessions = await api("/desktop/api/sessions/archived");
    const list = document.querySelector("#archivedSessions");
    if (list) {
      list.innerHTML = sessions.length
        ? sessions.map(item => `
            <div class="archived-session-item" data-context-id="${escapeHtml(item.context_id)}">
              <div class="archived-session-info">
                <span class="archived-session-title">${escapeHtml(item.title)}</span>
                <span class="archived-session-meta">${escapeHtml(item.workspace_name || uiText("common.local_workspace", "本地工作区"))} · ${escapeHtml(item.context_id.slice(0, 8))}</span>
              </div>
              <div class="archived-session-actions">
                <button class="text-button" data-action="view-history" data-context-id="${escapeHtml(item.context_id)}">只读查看</button>
                <button class="text-button" data-action="unarchive-context" data-context-id="${escapeHtml(item.context_id)}">${uiText("common.restore", "恢复")}</button>
                <button class="text-button danger" data-action="delete-context" data-context-id="${escapeHtml(item.context_id)}">${uiText("common.delete", "删除")}</button>
              </div>
            </div>`).join("")
        : `<p class="muted tiny" style="padding:var(--space-3)">${uiText("common.no_archived", "暂无已归档会话")}</p>`;
    }
  } catch (error) { setStatus(error.message, true); }
  try {
    await loadModelSettings();
  } catch (error) {
    modelSettings.error = error.message;
  }
  renderModelSettings();
  syncLanguageControls();
  const newSessionMode = settingsDialog?.querySelector?.("#newSessionAccessMode");
  if (newSessionMode) newSessionMode.value = accessMode.readNewSessionDefault(window.localStorage);
  const notificationsToggle = settingsDialog?.querySelector?.("#taskNotificationsEnabled");
  if (notificationsToggle) {
    notificationsToggle.disabled = !window.focusDesktop?.notifications;
    if (!notificationsToggle.disabled) {
      try { notificationsToggle.checked = (await window.focusDesktop.notifications.settings()).enabled; }
      catch (error) { setStatus(error.message, true); }
    }
  }
  interfaceI18n.apply(settingsDialog);
  const sandboxHost = settingsDialog?.querySelector?.("#sandboxStatusHost");
  if (sandboxHost) {
    try {
      const status = await api("/desktop/api/sandbox/status");
      sandboxHost.innerHTML = renderSandboxStatus(status);
    } catch (error) {
      sandboxHost.textContent = error.message;
    }
  }
  await loadShellRecovery();
  if (document.querySelector('[data-settings-pane="data"]')?.hidden === false) await loadStorageSettings();
}

async function loadShellRecovery() {
  const host = settingsDialog?.querySelector?.("#shellRecoveryHost");
  if (!host) return;
  try {
    host.innerHTML = renderShellRecovery(await api("/desktop/api/shell-recovery"));
  } catch (error) {
    host.textContent = error.message;
  }
}

function renderShellRecovery(runs) {
  const refresh = `<button class="text-button" data-action="refresh-shell-recovery">${escapeHtml(uiText("shell_recovery.refresh", "刷新"))}</button>`;
  if (!runs.length) return `<p>${escapeHtml(uiText("shell_recovery.empty", "没有等待 Shell 退出核验的 Run"))}</p>${refresh}`;
  return `<p>${escapeHtml(uiText("shell_recovery.help", "退出证据未确认，以下 Run 和目录暂时保留。核验会请求遗留 Shell 停止；成功后保持 Run 中断并释放目录，不会自动重放命令。若回执损坏或 runner 崩溃，请重启 Windows 后再次核验；仅重启应用不能证明进程树已退出。"))}</p>
    ${runs.map(run => `<div class="archived-session-item">
      <div class="archived-session-info"><strong>${escapeHtml(run.title)}</strong>
        <p>Run <code>${escapeHtml(run.run_id)}</code> · ${escapeHtml(run.status)}</p>
        <p><code>${escapeHtml(run.directory || uiText("shell_recovery.no_directory", "目录信息不可用"))}</code></p>
        <ul>${run.attempts.map(attempt => `<li>${escapeHtml(attempt.tool)} · <code>${escapeHtml(attempt.call_id)}</code> · ${escapeHtml(attempt.state)}</li>`).join("")}</ul>
      </div><button class="text-button" data-action="recover-shell-cleanup" data-run-id="${escapeHtml(run.run_id)}" ${run.status !== "interrupted" ? "disabled" : ""}>${escapeHtml(uiText("shell_recovery.recover", "核验退出并恢复"))}</button>
    </div>`).join("")}${refresh}`;
}

document.addEventListener("click", event => {
  const button = event.target.closest?.("[data-action='recover-shell-cleanup'], [data-action='refresh-shell-recovery']");
  if (!button) return;
  runUiAction(async () => {
    button.disabled = true;
    const feedback = settingsDialog?.querySelector?.("#shellRecoveryFeedback");
    try {
      if (button.dataset.action === "recover-shell-cleanup") {
        if (feedback) feedback.textContent = uiText("shell_recovery.verifying", "正在核验退出证据…");
        await api(`/desktop/api/runs/${encodeURIComponent(button.dataset.runId)}/shell-recovery`, { method: "POST" });
        const message = uiText("shell_recovery.done", "退出已核验，Run 已结算，目录已释放；请核对命令副作用后再发起新调用");
        if (feedback) feedback.textContent = message;
        setStatus(message);
      }
    } catch (error) {
      if (feedback) feedback.textContent = error.message;
      throw error;
    } finally {
      button.disabled = false;
      await loadShellRecovery();
    }
  });
});

function renderSandboxStatus(status) {
  const prepared = Array.isArray(status?.prepared_workspaces) ? status.prepared_workspaces : [];
  const limitations = Array.isArray(status?.limitations) ? status.limitations : [];
  return `<p>${escapeHtml(status?.limited_label || "受限后端不可用")}</p>
    <p>${escapeHtml(uiText("sandbox.unrestricted", "完全访问：未应用文件沙箱"))}</p>
    <h4>${escapeHtml(uiText("sandbox.prepared", "已准备或曾尝试准备的工作区"))}</h4>
    ${prepared.length ? `<ul>${prepared.map(path => `<li><code>${escapeHtml(path)}</code></li>`).join("")}</ul>` : `<p>${escapeHtml(uiText("sandbox.none_prepared", "暂无"))}</p>`}
    <h4>${escapeHtml(uiText("sandbox.limitations", "实际限制"))}</h4>
    <ul>${limitations.map((item, index) => `<li>${escapeHtml(uiText(`sandbox.limit_${index + 1}`, String(item)))}</li>`).join("")}</ul>`;
}

function fileContextFromNode(node) {
  const source = node.closest("[data-file-context]");
  return {
    reference: source ? JSON.parse(source.dataset.fileContext) : {},
    taskId: source?.dataset.fileTaskId || state.activeTaskId,
  };
}

function openFileCard(button) {
  const source = fileContextFromNode(button);
  const reference = JSON.parse(button.dataset.fileReference);
  // A card can narrow its identity, but cannot silently turn a historical
  // container into a live disk read.
  if (source.reference.view === "history") reference.view = "history";
  return openFileLink({ ...source.reference, ...reference }, source.taskId);
}

// Capture all local links before Electron can navigate. Display text and material
// filenames never participate in resolution. Real web links remain external.
document.addEventListener("click", event => {
  FocusFileLinks.follow(event, {
    onExternal: window.focusDesktop?.openExternal,
    onError: error => setStatus(error.message, true),
    onFile: (path, anchor, location) => {
      const source = fileContextFromNode(anchor);
      const reference = anchor.dataset?.fileReference ? JSON.parse(anchor.dataset.fileReference) : {};
      if (source.reference.view === "history") reference.view = "history";
      return openFileLink({ ...source.reference, ...reference, path, ...FocusFileLinks.lineRange(location) }, source.taskId);
    },
  });
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
  const edge = event.target.closest(".map-portfolio-surface [data-edge-hit]");
  if (edge && state.view === "map") return inspectMapRelationship(edge.dataset.edgeHit, app.querySelector('[data-action="map-related"]'));
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  if (action === "reference-remove") return setMainReference(null);
  if (action === "reference-source") return jumpToReference(JSON.parse(button.dataset.reference));
  if (action === "retry-task-resource") {
    if (button.dataset.taskId !== state.activeTaskId || state.view !== "focus") return;
    return hydrateTaskResource(button.dataset.taskId, button.dataset.resource, { reusePending: true });
  }
  if (action === "open-file-panel") return openFileCard(button);
  if (action === "copy-answer" || action === "copy-question") return copyMessageText(button, button.dataset.copyText);
  if (action === "edit-history-message") return openMessageRetry(button, true);
  if (action === "retry-history-message") return openMessageRetry(button, false);
  if (action === "quote-history-message") return quoteHistoryMessage(button);
  if (action === "cancel-message-retry") return closeMessageRetry();
  if (action === "submit-message-retry") return submitMessageRetry();
  if (action === "copy-code") return copyMessageText(button, button.closest(".message-code-block")?.querySelector("pre > code")?.textContent);
  if (action === "export-conversation") return exportConversation();
  if (action === "show-patrol") {
    if (state.view === "focus") persistFocusState();
    cancelPendingViewRequests();
    workspacePatrolPicker?.dispose();
    workspacePatrolPicker = null;
    if (button.hasAttribute("data-patrol-switch")) patrolWorkspaceBeforePick = state.patrolWorkspace || patrolWorkspaceBeforePick;
    state.patrolWorkspace = button.hasAttribute("data-patrol-switch") ? null : state.patrolWorkspace || workspacePatrolIntake.readBinding();
    state.inspector.open = false;
    state.view = "patrol";
    return render();
  }
  if (action === "reload") return bootstrap();
  if (action === "toggle-nav-collapse") {
    // 已折叠则展开（恢复到 navWidth），未折叠则折叠为 0。
    return setShellNavCollapsed(!state.shellLayout.navCollapsed);
  }
  if (action === "new-task") {
    const titleInput = document.querySelector("#threadTitle");
    if (titleInput && ["新任务", "New Task"].includes(titleInput.value)) {
      titleInput.value = uiText("dialog.default_title", "新任务");
    }
    return window.FocusSurfaceTransition.visible(dialog, true, { modal: true, source: button });
  }
  if (action === "close-task-dialog") return window.FocusSurfaceTransition.visible(dialog, false, { modal: true });
  if (action === "pick-workspace") return pickWorkspace();
  if (action === "select-skill") return selectSkill(button.dataset.pickerKind, button.dataset.skillName);
  if (action === "select-commit") return selectCommitCommand(button.dataset.pickerKind);
  if (action === "remove-skill") return removeSkill(button.dataset.pickerKind, button.dataset.skillName);
  if (action === "show-map") { if (state.view === "focus") persistFocusState(); cancelPendingViewRequests(); state.inspector.open = false; state.view = "map"; render(); await hydrateContextTrees(); if (state.view === "map") render(); return; }
  if (action === "set-map-view") {
    const mode = button.dataset.mapView;
    if (!["graph", "tree", "cards"].includes(mode)) return;
    state.mapViewMode = mode;
    state.mapTreeFocusKey = "";
    if (mode === "tree") state.mapExpandedForTaskId = null;
    persistMapViewPreferences();
    renderMap();
    await hydrateContextTrees();
    if (state.view !== "map" || state.mapViewMode !== mode) return;
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
  if (action === "map-related") { mapRelatedOnly = !mapRelatedOnly; return patchMapGraph(); }
  if (action === "refresh-map-portfolio") {
    mapRunPreviews?.retry();
    if (mapLive) void mapLive.connection.start(mapLive.loopId);
    return hydrateMapPortfolio(mapPortfolio.workspaceId, true);
  }
  if (action === "show-contexts") return openInspector("context", button);
  if (action === "show-agents") return openInspector("agents", button);
  if (action === "toggle-session-patrol") {
    const detail = state.details.get(state.activeTaskId);
    if (!detail) return;
    detail.ui_state = { ...(detail.ui_state || {}), show_session_patrol: detail.ui_state?.show_session_patrol === false };
    document.querySelector("#patrolAvatarLayer").hidden = !detail.ui_state.show_session_patrol;
    persistFocusState();
    return;
  }
  if (action === "show-loop") return openLoopView();
  if (loopMissionEditor?.handleAction(button)) return;
  if (action === "loop-control") return controlLoop(button.dataset.loopControl);
  if (action === "inspect-loop-fact") {
    const loopId = state.loop.loopId;
    if (!loopId) return;
    const revision = button.closest("[data-fact-revision]")?.dataset.factRevision;
    const detail = await loopApi.factDetail(loopId, button.dataset.factId, undefined, revision ? Number(revision) : undefined);
    if (state.loop.loopId !== loopId) return;
    const panel = document.createElement("dialog");
    panel.className = "patrol-dialog glass-surface";
    panel.setAttribute("aria-label", "事实与来源");
    panel.innerHTML = `<header><h2>事实与来源</h2><form method="dialog"><button aria-label="关闭">×</button></form></header><div data-patrol-dialog-body>${window.FocusLoopFactsView.renderDetail(detail)}</div>`;
    if (detail.file) window.FocusFilePreview?.mountHistory(panel, detail.file, loopId);
    document.body.append(panel);
    panel.addEventListener("close", () => panel.remove(), { once: true });
    const dismiss = event => { event.preventDefault(); void window.FocusSurfaceTransition.visible(panel, false, { modal: true }); };
    panel.addEventListener("cancel", dismiss);
    panel.querySelector("form").addEventListener("submit", dismiss);
    void window.FocusSurfaceTransition.visible(panel, true, { modal: true, source: button });
    return;
  }
  if (action === "loop-resume-current-mission") return resumeLoopWithCurrentMission(loopStore?.get().snapshot?.wait_request);
  if (button.dataset.waitAction) {
    const request = loopStore?.get().snapshot?.wait_request;
    if (!request) return;
    const answer = { action: button.dataset.waitAction };
    if (answer.action === "revise_budget") {
      const form = button.closest("[data-loop-wait-response]");
      answer.budgets = Object.fromEntries(
        [...form.querySelectorAll('[name^="budget:"]')].map(input => [input.name.slice(7), Number(input.value)])
      );
    }
    return respondToLoopWait(request, answer);
  }
  if (action === "loop-exit") return exitAgentLoop(false);
  if (action === "loop-prepare-new" || action === "loop-new-run") return exitAgentLoop(true);
  if (action === "loop-select-context") {
    if (state.view !== "map") return loopConsoleController?.selectContext(button.dataset.contextId);
    if (state.selectionMode) return toggleSelectSession(button.dataset.contextId);
    if (state.soldierArmed) return openDraft(button.dataset.contextId);
    return inspectMapContext(button.dataset.contextId, button, button.dataset.revisionId);
  }
  if (action === "loop-restore-compression") return runUiAction(async () => {
    await loopApi.restoreCompression(button.dataset.contextId, button.dataset.messageId);
    await loopConsoleController?.selectContext(button.dataset.contextId, { preserveSelection: true, force: true });
    setStatus("压缩来源已恢复并发布新 Context Revision");
  });
  if (action === "loop-load-older") return loopConsoleController?.loadOlder();
  if (action === "loop-load-older-activity") return loopLiveConnection?.loadHistory(true);
  if (action === "loop-load-latest-activity") return loopLiveConnection?.loadHistory(false);
  if (action === "loop-load-newer") return loopConsoleController?.loadNewer();
  if (action === "loop-load-older-facts") return loopConsoleController?.loadOlderFacts();
  if (action === "loop-intervention-mode") return loopConsoleStore?.setMode(button.dataset.mode);
  if (action === "loop-message-filter") return loopConsoleStore?.setMessageFilter(button.dataset.filter);
  if (action === "loop-fact-filter") return loopConsoleController?.setFactFilter(button.dataset.filter);
  if (action === "loop-fact-status") return loopConsoleController?.setFactStatus(button.dataset.status);
  if (action === "loop-fact-scope") return loopConsoleController?.setFactScope(button.dataset.scope);
  if (action === "loop-revoke-grant") return mutateAgentLoopGrant({ command: "revoke" });
  if (action === "open-loop-revision") return openLoopRevision(button.dataset.openRevision);
  if (action === "close-loop-revision") { state.loop.revisionSnapshot = null; return renderLoop(); }
  if (action === "open-inspector-tab") return openInspector(button.dataset.inspectorTab, button);
  if (action === "close-inspector") return closeInspector();
  if (action === "show-plugins") return openPluginsView();
  if (action === "show-memory") return openMemoryView();
  if (action === "memory-category") { state.memory.category = button.dataset.category; return renderMemory(); }
  if (action === "new-strategy") { openMemoryCompose(false); state.memory.draft = { ...state.memory.draft, category: "evolution_strategy", enabled: true }; renderMemory(); return; }
  if (action === "save-strategy") return saveStrategy();
  if (action === "import-strategy") return openStrategyImport();
  if (action === "preview-strategy") return previewStrategyImport();
  if (action === "install-strategy") return installStrategyImport();
  if (action === "fork-imported-strategy") return forkImportedStrategy();
  if (action === "export-strategy") return exportStrategy(button.dataset.memoryId);
  if (action === "generate-strategy-draft") return generateStrategyDraft();
  if (action === "rehearse-strategy") return rehearseStrategy();
  if (action === "extract-memory-strategy") { openMemoryCompose(true); return generateStrategyDraft(); }
  if (action === "cancel-strategy-import") { strategyImportRequestSequence++; state.memory.strategyImport = null; return renderMemory(); }
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
      return await showAssembly();
    } catch (error) { setStatus(error.message, true); }
    return render();
  }
  if (action === "pick-assembly-workspace") {
    button.disabled = true;
    try {
      const path = window.focusDesktop?.selectWorkspace ? await window.focusDesktop.selectWorkspace() : (await api("/desktop/api/workspaces/select", { method: "POST" })).path;
      if (!path) return;
      const workspace = await api("/desktop/api/workspaces", { method: "POST", body: JSON.stringify({ path }) });
      await showAssembly(workspace.workspace_id);
    } catch (error) { setStatus(error.message, true); }
    finally { button.disabled = false; }
    return;
  }
  if (action === "refresh-capabilities") {
    const taskId = state.activeTaskId;
    button.disabled = true;
    try {
      const result = await api(`/desktop/api/tasks/${taskId}/capabilities/refresh`, { method: "POST" });
      state.skillCatalogs.set(taskId, result.skills);
      state.capabilityRefreshResults.set(taskId, result);
      if (state.activeTaskId === taskId) renderFocus();
      const failures = result.mcp_failures || [];
      const hasFailures = failures.length > 0 || result.failed_servers.length > 0;
      setStatus(hasFailures
        ? `MCP 刷新存在失败；${capabilityRefreshSummary(result)}，请查看装配区的处理建议`
        : `${capabilityRefreshSummary(result)}；下次运行生效`, hasFailures);
    } catch (error) { setStatus(error.message, true); }
    finally { button.disabled = false; }
    return;
  }
  if (action === "refresh-plugins") { await hydratePlugins(); return render(); }
  if (action === "reload-plugins") {
    if (state.plugins.loading) return;
    state.plugins = { ...state.plugins, loading: true, error: null };
    renderPlugins();
    try {
      await api("/desktop/api/plugins/reload", { method: "POST" });
      pluginAssetsLoading = null;
      await hydratePlugins();
    } catch (error) {
      state.plugins = { ...state.plugins, loading: false, error: error.message };
      if (state.view === "plugins") renderPlugins();
    }
    return;
  }
  if (action === "retry-equipment") return hydrateEquipment();
  if (action === "set-library-layout") {
    const library = button.dataset.library;
    if (!["plugins", "memory"].includes(library) || !["cards", "list"].includes(button.dataset.layout)) return;
    const layout = button.dataset.layout;
    state[library].layout = layout;
    library === "plugins" ? renderPlugins() : renderMemory();
    app.querySelector(`[data-library="${library}"][data-layout="${layout}"]`)?.focus({ preventScroll: true });
    return;
  }
  if (action === "refresh-memory") { await hydrateMemory(); return render(); }
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
  if (action === "focus-home") return state.activeTaskId ? goFocusHome() : (state.view = "focus", render());
  if (action === "derive-context") return openContextEditor(state.activeTaskId);
  if (action === "organize-context") return openCompressionView(activeTask(), null, null, true);
  if (action === "open-settings") return openSettings();
  if (action === "view-history") return openHistory(button.dataset.contextId);
  if (action === "open-history-result") {
    const hit = state.historySearch.results[Number(button.dataset.resultIndex)];
    if (hit) return openHistory(hit.context_id, hit.key, hit.message_id);
    return;
  }
  if (action === "clear-history-search") { state.mapQuery = ""; queueHistorySearch(""); return renderMap(); }
  if (action === "retry-history-search") return loadHistorySearch();
  if (action === "more-history-results") return loadHistorySearch(true);
  if (action === "retry-history-reader") return openHistory(state.historyReader.contextId, state.historyReader.targetKey, state.historyReader.messageId);
  if (action === "back-history-search") { state.view = "map"; return render(); }
  if (action === "history-continue") return switchTask(button.dataset.contextId);
  if (action === "history-earlier" || action === "history-later") {
    state.historyReader.start = Math.max(0, state.historyReader.start + (action === "history-earlier" ? -80 : 80));
    renderHistoryReader();
    app.scrollTop = 0;
    return;
  }
  if (action === "close-settings") return void window.FocusSurfaceTransition.visible(settingsDialog, false, { modal: true });
  if (action === "settings-tab") return selectSettingsTab(button.dataset.settingsTab);
  if (action === "model-add") return runUiAction(addModelEntry);
  if (action === "model-edit") return runUiAction(() => startModelDraft(Number(button.dataset.modelIndex)));
  if (action === "model-remove") return runUiAction(() => removeModelEntry(Number(button.dataset.modelIndex)));
  if (action === "model-reset") return runUiAction(restoreShippedModels);
  if (action === "model-cancel") return runUiAction(cancelModelDraft);
  if (action === "model-save") return runUiAction(saveModelSettings);
  if (action === "model-setup-continue") return runUiAction(finishDesktopModelSetup);
  if (action === "model-setup-retry") return runUiAction(() => ensureModelSetup({ onboarding: true }));
  if (action === "model-test") return runUiAction(testModelConnection);
  if (action === "model-advanced") { modelSettings.advanced = !modelSettings.advanced; return renderModelSettings(); }
  if (action === "model-verify") return runUiAction(reverifyModelSettings);
  if (action === "model-repair") return runUiAction(() => repairModelCheck(button.dataset.capability));
  if (action === "model-start") {
    if (modelSettings.saving || modelSettings.dirty || !modelSettings.snapshot?.verification?.ok) return;
    if (modelSettings.onboarding) {
      await finishDesktopModelSetup();
      if (!modelSettings.onboarding) modelSettings.setupMode = false;
      return;
    }
    modelSettings.setupMode = false;
    await window.FocusSurfaceTransition.visible(settingsDialog, false, { modal: true });
    return window.FocusSurfaceTransition.visible(dialog, true, { modal: true, source: button });
  }
  if (action === "set-language") {
    interfaceI18n.setLocale(button.dataset.locale);
    syncLanguageControls();
    return;
  }
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
  if (action === "context-message-toggle") {
    const row = button.closest("[data-context-ui-key]");
    if (row) return toggleContextMessage(row.dataset.contextUiKey);
    return;
  }
  if (action === "context-message-undo") return undoContextMessageDelete();
  if (["context-message-copy", "context-message-delete"].includes(action)) {
    try {
      const uiKey = button.closest("[data-context-ui-key]")?.dataset.contextUiKey;
      const index = state.contextDraft?.uiKeys.indexOf(uiKey) ?? -1;
      if (index < 0) return;
      if (action === "context-message-copy") return insertContextMessage(state.contextDraft.messages[index], index + 1);
      return deleteContextMessage(uiKey);
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
  if (action === "zoom-image" && button.dataset.imageUrl) return openImageLightbox(button.dataset.imageUrl, "图片");
  if (action === "close-lightbox") { const root = document.querySelector("#overlayRoot"); if (root) root.innerHTML = ""; return; }
  if (action === "retry-must-view") return resumeMustView("retry");
  if (action === "cancel-must-view") return resumeMustView("cancel");
  if (action === "send-main") return sendMain();
  if (action === "open-composer-file" || action === "remove-composer-file") {
    const refs = fileReferences(state.activeTaskId), index = Number(button.dataset.referenceIndex);
    if (!refs[index]) return;
    if (action === "open-composer-file") return openFileLink(refs[index]);
    return updateFileReferences(state.activeTaskId, refs.filter((_, i) => i !== index));
  }
  if (action === "queue-main") return sendMain(false, "queue");
  if (action === "edit-queued-main") return withdrawQueuedMain(button.dataset.inputId, true);
  if (action === "delete-queued-main") return withdrawQueuedMain(button.dataset.inputId, false);
  if (action === "retry-main") return sendMain(true);
  if (action === "edit-main") return editMainRequest();
  if (action.startsWith("composer-") && action.endsWith("-attachment")) return handleComposerAttachmentAction(button);
  if (action === "load-earlier-conversation") return loadEarlierConversation();
  if (action === "expand-stream-content") {
    const article = button.closest("[data-stream-run]");
    const buffer = state.streamBuffers.get(article?.dataset.streamRun);
    if (buffer && buffer.taskId === state.activeTaskId) return conversationView.expandStreaming(article, buffer);
    return;
  }
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
  if (action === "toggle-access-mode") return toggleAccessModeMenu(button);
  if (action === "select-access-mode") return selectAccessMode(button);
  if (action === "confirm-access-mode") return confirmAccessMode(button);
  if (action === "confirm-access-review-full") return confirmAccessReviewFull(button);
  if (action === "cancel-access-mode") return cancelAccessMode(button);
  if (action === "exit-draft") { if (await saveDraft(state.activeTaskId)) { state.view = "map"; return render(); } return; }
  if (action === "set-patrol-mode") {
    if (!await saveDraft(state.activeTaskId)) return;
    const draft = syncDraftFromDom();
    const mode = button.dataset.mode;
    if (!setDraftPatrolMode(draft, mode)) return;
    renderDraft();
    if (await saveDraft(state.activeTaskId, { syncDom: false })) renderDraft();
    return;
  }
  if (action === "deploy") return deployDraft();
  if (action === "set-material-grouping") {
    const detail = state.details.get(state.activeTaskId);
    detail.ui_state = { ...(detail.ui_state || {}), material_grouping_mode: button.dataset.mode };
    renderInspector();
    return persistFocusState();
  }
  if (action === "toggle-material-group") {
    const detail = state.details.get(state.activeTaskId);
    const collapsed = new Set(detail.ui_state?.collapsed_material_groups || []);
    if (collapsed.has(button.dataset.groupKey)) collapsed.delete(button.dataset.groupKey);
    else collapsed.add(button.dataset.groupKey);
    detail.ui_state = { ...(detail.ui_state || {}), collapsed_material_groups: [...collapsed] };
    renderInspector();
    return persistFocusState();
  }
  if (action === "create-material-group") return createMaterialGroup();
  if (action === "rename-material-group") return renameMaterialGroup(button.dataset.groupId);
  if (action === "delete-material-group") return deleteMaterialGroup(button.dataset.groupId);
  if (action === "move-material-group-order") return moveMaterialGroupOrder(button.dataset.groupId, button.dataset.direction);
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

document.addEventListener("dragstart", event => {
  // 材料行可沿整行拖放重排；起点落在行内文本编辑控件（备注 textarea 等）时属于文本编辑手势，
  // 不得转成材料拖放。刻意不排除 button：材料名称摘要本身就是 button，排除它会让重排无处可抓。
  if (event.target.closest?.("input, textarea, select, [contenteditable]")) return;
  const row = event.target.closest?.("[data-material-id]");
  if (!row || !event.dataTransfer) return;
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/focus-material-id", row.dataset.materialId);
});

document.addEventListener("dragover", event => {
  if (event.target.closest?.("[data-drop-group-id]")) event.preventDefault();
});

document.addEventListener("drop", event => {
  const target = event.target.closest?.("[data-drop-group-id]");
  if (!target || !event.dataTransfer) return;
  event.preventDefault();
  const materialId = event.dataTransfer.getData("text/focus-material-id");
  if (!materialId) return;
  const beforeMaterialId = event.target.closest?.("[data-material-id]")?.dataset.materialId;
  runUiAction(() => dropMaterialMembership(
    materialId,
    target.dataset.dropGroupId || null,
    beforeMaterialId && beforeMaterialId !== materialId ? beforeMaterialId : null
  ));
});

document.addEventListener("compositionstart", event => {
  if (event.target.id === "mainInput") composingMainInputs.add(event.target);
});
document.addEventListener("compositionend", event => {
  if (event.target.id === "mainInput") composingMainInputs.delete(event.target);
});

document.addEventListener("input", event => {
  if (event.target.dataset?.pageSearch) {
    const page = event.target.dataset.pageSearch;
    const position = event.target.selectionStart;
    if (event.isComposing) { if (page === "map") { state.mapQuery = event.target.value; queueHistorySearch(""); } else state[page].query = event.target.value; return; }
    if (page === "plugins") { state.plugins.query = event.target.value; renderPlugins(); }
    else if (page === "memory") { state.memory.query = event.target.value; renderMemory(); }
    else if (page === "map") { state.mapQuery = event.target.value; queueHistorySearch(event.target.value); renderMap(); }
    const input = app.querySelector(`[data-page-search="${page}"]`);
    input?.focus();
    input?.setSelectionRange(position, position);
    return;
  }
  if (event.target.id === "mainInput") {
    updateAtHighlight(event.target);
    composerDraft.claim(state.activeTaskId, event.target.value);
    scheduleComposerPersist();
  }
  if (event.target.matches('[data-field="run-material-note"]')) {
    const row = event.target.closest("[data-material-id]");
    if (row) updateRunMaterialNote(row.dataset.materialId, event.target.value);
  }
  if (event.target.matches("[data-skill-input]")) {
    const fileMentionOpen = event.target.id === "mainInput" && mainFilePicker?.update();
    if (fileMentionOpen) document.querySelector("#mainSkillList").hidden = true;
    else updateSkillMenu(event.target, true);
  }
  if (event.target.matches("[data-draft-field],[data-message-field],[data-equipment],[data-permission],[data-curation-policy]")) scheduleDraftSave();
  if (event.target.matches("[data-memory-field]")) syncMemoryField(event.target.dataset.memoryField, event.target.type === "checkbox" ? event.target.checked : event.target.value);
  if (event.target.matches("[data-strategy-import-field]")) syncStrategyImportField(event.target.dataset.strategyImportField, event.target.type === "checkbox" ? event.target.checked : event.target.value);
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
  if (event.target.matches("[data-model-field]")) updateModelField(event.target);
  // 密钥输入只进内存、不回填：任何响应都不携带凭据值
  if (event.target.matches("[data-model-credential]") && !modelSettings.saving) {
    updateModelCredential(event.target.value);
    // 不重建输入框，以免打断键盘输入；立即禁用旧结果上的开始与重试入口。
    document.querySelectorAll('[data-action="model-start"], [data-action="model-verify"]').forEach(button => { button.disabled = true; });
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
  if (document.querySelector("dialog[open]") && event.key === "Escape") return;
  if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key === "1") {
    event.preventDefault(); document.querySelector("[data-nav-key=patrol]").click(); return;
  }
  if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "k") {
    event.preventDefault(); (app.querySelector("[data-page-search]") || app.querySelector("[data-patrol-content]:not(:disabled), #mainInput") || app.querySelector("[data-patrol-switch]"))?.focus(); return;
  }
  if (event.key === "Escape" && contextPointerDrag) {
    event.preventDefault();
    return cancelContextPointerDrag();
  }
  if (event.key === "Escape" && state.inspector.open) {
    event.preventDefault();
    closeInspector();
    return;
  }
  // 导航折叠/展开快捷键：Alt+N（与头部控制钮等价）。
  if (event.altKey && (event.key === "n" || event.key === "N") && !event.shiftKey && !event.ctrlKey && !event.metaKey) {
    event.preventDefault();
    setShellNavCollapsed(!state.shellLayout.navCollapsed);
    return;
  }
  // 整页缩放：Ctrl/Cmd + -（缩小）、= / +（放大）、0（重置）。跨平台同时接受 Ctrl 与 Cmd。
  if ((event.ctrlKey || event.metaKey) && !event.altKey) {
    const zoomKey = event.key;
    if (zoomKey === "-" || zoomKey === "_") { event.preventDefault(); return changeZoomLevel(-1); }
    if (zoomKey === "=" || zoomKey === "+") { event.preventDefault(); return changeZoomLevel(1); }
    if (zoomKey === "0") { event.preventDefault(); return applyZoomLevel(0); }
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
  const rows = [...list.querySelectorAll(".context-message-editor[data-context-ui-key]")].filter(row => row !== drag.card);
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
  if (event.target.closest(".memory-source-edit-list") && event.dataTransfer.types.includes("application/x-focus-source-index")) { event.preventDefault(); event.dataTransfer.dropEffect = "move"; }
  if (event.target.closest(".memory-compose") && event.dataTransfer.types.includes("application/x-focus-memory")) { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; }
});

document.addEventListener("dragleave", event => event.target.closest(".task-card, .map-collapsible-context-item")?.classList.remove("drop-target"));
document.addEventListener("drop", event => {
  const card = event.target.closest(".task-card, .map-collapsible-context-item");
  if (card && event.dataTransfer.getData("application/x-focus-soldier")) { event.preventDefault(); return openDraft(card.dataset.taskId); }
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


document.addEventListener("submit", event => {
  if (event.target.id === "taskForm") {
    event.preventDefault();
    runUiAction(() => createTask(event.target));
    return;
  }
  if (event.target.matches("[data-loop-wait-response]")) {
    event.preventDefault();
    const request = loopStore?.get().snapshot?.wait_request;
    if (!request) return;
    const answer = window.FocusLoopWaitRequestView.readAnswer(event.target, request.response_mode);
    respondToLoopWait(request, answer);
    return;
  }
  if (event.target.id === "agentLoopStartForm") {
    event.preventDefault();
    runUiAction(() => startAgentLoop(event.target));
    return;
  }
  if (event.target.id === "agentLoopOverrideForm") {
    event.preventDefault();
    runUiAction(() => overrideAgentLoop(event.target));
    return;
  }
  if (event.target.id === "agentLoopGrantForm") {
    event.preventDefault();
    runUiAction(() => narrowAgentLoopGrant(event.target));
    return;
  }
  if (event.target.id === "agentLoopBudgetForm") {
    event.preventDefault();
    runUiAction(() => adjustAgentLoopBudgets(event.target));
    return;
  }
  if (event.target.id === "loopInterventionForm") {
    event.preventDefault();
    const form = event.target;
    const content = new FormData(form).get("content");
    const submitted = loopConsoleStore.get();
    runUiAction(async () => {
      await loopConsoleController?.submit(content);
      const current = loopConsoleStore.get();
      const currentForm = document.querySelector("#loopInterventionForm");
      if (current.selectedContextId === submitted.selectedContextId && current.interventionMode === submitted.interventionMode
          && currentForm?.elements.content.value === content) currentForm.reset();
    });
    return;
  }
  if (event.target.id !== "agentInspectorContinueForm") return;
  event.preventDefault();
  runUiAction(continueAgentDetails);
});
document.addEventListener("input", event => {
  const form = event.target.closest?.("[data-loop-wait-response]");
  const requestId = form?.closest?.("[data-wait-request-id]")?.dataset.waitRequestId;
  if (requestId) loopWaitDrafts?.set(requestId, serializeWaitDraft(form));
});
document.addEventListener("change", event => {
  if (event.target.matches("[data-model-service]")) return updateModelService(event.target.value);
  if (event.target.id === "newSessionAccessMode") {
    const select = event.target;
    try { select.value = accessMode.saveNewSessionDefault(window.localStorage, select.value); }
    catch (error) {
      select.value = accessMode.readNewSessionDefault(window.localStorage);
      setStatus(error.message, true);
    }
    return;
  }
  if (event.target.id === "taskNotificationsEnabled") {
    const input = event.target;
    const enabled = input.checked;
    input.disabled = true;
    runUiAction(async () => {
      try { input.checked = (await window.focusDesktop.notifications.settings(enabled)).enabled; }
      catch (error) { input.checked = !enabled; setStatus(error.message, true); }
      finally { input.disabled = false; }
    });
    return;
  }
  if (event.target.matches?.("[data-assembly-target]")) {
    const select = event.target;
    select.disabled = true;
    runUiAction(async () => {
      try { await showAssembly(select.value); }
      finally { if (select.isConnected) { select.disabled = false; renderFocus(); } }
    });
    return;
  }
  if (event.target.matches?.("[data-strategy-file]")) { readStrategyFile(event.target.files?.[0]); return; }
  const form = event.target.closest?.("[data-loop-wait-response]");
  const requestId = form?.closest?.("[data-wait-request-id]")?.dataset.waitRequestId;
  if (requestId) loopWaitDrafts?.set(requestId, serializeWaitDraft(form));
});

function serializeWaitDraft(form) {
  return window.FocusLoopWaitRequestView.serializeDraft(form);
}
async function uploadMaterialFile(file, taskId = state.activeTaskId, entry = null) {
  if (!file) return;
  if (!taskId) return setStatus("请先选择任务，再添加材料", true);
  entry ||= createComposerAttachment(file, taskId);
  if (entry.removed) { entry.finish(); return; }
  try {
    const body = new FormData(); body.append("file", file);
    const material = await api(`/desktop/api/tasks/${taskId}/materials/upload`, { method: "POST", body });
    if (!material?.material_id) throw new Error("未收到有效的附件回执");
    const materials = state.materials.get(taskId) || [];
    state.materials.set(taskId, [...materials.filter(item => item.material_id !== material.material_id), material]);
    entry.materialId = material.material_id;
    entry.status = "ready";
    if (!entry.removed) {
      const selection = materialSelection(taskId);
      if (!selection.bindings.some(item => item.materialId === material.material_id)) {
        state.materialSelections.set(taskId, runMaterialPicker.toggleMaterial(selection, material.material_id));
      }
    }
    refreshComposerAttachments(taskId);
    // 更新检查器，不重建正在输入的 textarea；上传回执已足够用于本次发送。
    clearAttachmentPreparationError(taskId);
    const draft = state.drafts.get(taskId);
    if (draft?.mode === "standard") window.FocusPatrolWorkbench?.get(draft.draft_id)?.configurationChanged();
    if (state.activeTaskId === taskId) renderInspector();
  } catch (error) {
    entry.status = "failed";
    entry.error = error.message;
    if (!entry.removed && state.activeTaskId === taskId) setStatus(error.message, true);
    refreshComposerAttachments(taskId);
  } finally { entry.finish(); }
}

function queueComposerAttachments(files, taskId) {
  const entries = files.map(file => createComposerAttachment(file, taskId));
  return runUiAction(async () => {
    for (const entry of entries) {
      if (entry.removed) { entry.finish(); continue; }
      await uploadMaterialFile(entry.file, taskId, entry);
    }
  });
}

document.addEventListener("change", event => {
  if (event.target.id !== "fileInput") return;
  const input = event.target;
  const files = [...(input.files || [])];
  if (!files.length) return;
  const taskId = state.activeTaskId;
  if (!taskId) return setStatus("请先选择任务，再添加材料", true);
  input.value = "";
  queueComposerAttachments(files, taskId);
});

document.addEventListener("input", event => {
  if (!event.target.matches?.("[data-loop-message-search]")) return;
  const query = event.target.value.trim().toLowerCase();
  app.querySelectorAll("[data-loop-transcript] .loop-message").forEach(message => {
    message.hidden = Boolean(query) && !message.textContent.toLowerCase().includes(query);
  });
});

document.addEventListener("paste", event => {
  if (event.target !== document.querySelector("#mainInput")) return;
  const images = [...(event.clipboardData?.items || [])].filter(item => item.type.startsWith("image/"));
  if (!images.length) return;
  event.preventDefault();
  if (!state.activeTaskId) return setStatus("请先选择任务，再粘贴图片", true);
  const taskId = state.activeTaskId;
  const files = images.map(item => item.getAsFile()).filter(Boolean);
  queueComposerAttachments(files, taskId);
});

function mainComposerDropTarget(event) {
  const composer = event.target.closest?.("[data-main-composer]");
  const input = document.querySelector("#mainInput");
  return composer && input && !input.disabled ? composer : null;
}

document.addEventListener("dragover", event => {
  const composer = event.target.closest?.("[data-main-composer]");
  if (!composer || !Array.from(event.dataTransfer?.types || []).includes("Files")) return;
  event.preventDefault();
  if (!mainComposerDropTarget(event)) {
    event.dataTransfer.dropEffect = "none";
    composer.classList.remove("is-file-dragover");
    return;
  }
  event.dataTransfer.dropEffect = "copy";
  composer.classList.add("is-file-dragover");
});

document.addEventListener("dragleave", event => {
  const composer = event.target.closest?.("[data-main-composer]");
  if (composer && !composer.contains(event.relatedTarget)) composer.classList.remove("is-file-dragover");
});

document.addEventListener("drop", event => {
  const composer = event.target.closest?.("[data-main-composer]");
  composer?.classList.remove("is-file-dragover");
  if (!composer || !Array.from(event.dataTransfer?.types || []).includes("Files")) return;
  event.preventDefault();
  if (!mainComposerDropTarget(event)) return;
  const files = Array.from(event.dataTransfer.files || []);
  if (!files.length) return;
  const taskId = state.activeTaskId;
  if (!taskId) return setStatus("请先选择任务，再添加材料", true);
  queueComposerAttachments(files, taskId);
});

function persistUiState(taskId = state.activeTaskId) {
  const detail = state.details.get(taskId);
  if (!detail) return Promise.resolve();
  return api(`/desktop/api/tasks/${taskId}/ui-state`, { method: "PUT", body: JSON.stringify(detail.ui_state) }).catch(() => {});
}

function persistFocusState() {
  const taskId = state.activeTaskId;
  const detail = state.details.get(taskId);
  if (!detail) return Promise.resolve();
  const input = document.querySelector("#mainInput");
  if (input) composerDraft.claim(taskId, input.value);
  const conversation = document.querySelector("#conversation");
  detail.ui_state = {
    ...(detail.ui_state || {}),
    input: composerDraft.value(taskId, detail.ui_state?.input ?? ""),
    file_references: fileReferences(taskId),
    message_reference: referenceDrafts.get(taskId, detail.ui_state?.message_reference),
    skills: selectedSkills("main"),
    scrollTop: conversation ? (conversation.scrollTop || 0) : (detail.ui_state?.scrollTop || 0),
  };
  return persistUiState(taskId);
}

const COMPOSER_PERSIST_DELAY = 400;
let composerPersistTimer = null;

function scheduleComposerPersist() {
  if (composerPersistTimer !== null) return;
  composerPersistTimer = setTimeout(() => {
    composerPersistTimer = null;
    void persistFocusState();
  }, COMPOSER_PERSIST_DELAY);
}

function flushComposerPersist() {
  if (composerPersistTimer !== null) {
    clearTimeout(composerPersistTimer);
    composerPersistTimer = null;
  }
  const taskId = state.activeTaskId;
  const detail = state.details.get(taskId);
  if (!detail) return Promise.resolve();
  const input = document.querySelector("#mainInput");
  if (input) composerDraft.claim(taskId, input.value);
  const unsent = composerDraft.value(taskId, null);
  detail.ui_state = { ...(detail.ui_state || {}), file_references: fileReferences(taskId), ...(unsent !== null ? { input: unsent } : {}) };
  return persistUiState(taskId);
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") { void flushComposerPersist(); flushWorkspacePatrol(); }
});
function flushWorkspacePatrol(checkpoint = false) {
  try {
    if (checkpoint) workspacePatrolIntake?.checkpoint();
    void workspacePatrolIntake?.flush().catch(error => setStatus(`Patrol 草稿保存失败：${error.message}`, true));
  } catch (error) { setStatus(`Patrol 草稿保存失败：${error.message}`, true); }
}
window.addEventListener("pagehide", () => { void flushComposerPersist(); flushWorkspacePatrol(true); });

document.addEventListener("focus:languagechange", () => {
  render();
  interfaceI18n.apply(document);
  syncLanguageControls();
  if (settingsDialog?.open) runUiAction(openSettings);
});

document.addEventListener("focus:gateway-restored", () => runUiAction(async () => {
  const taskId = state.activeTaskId;
  await refreshTasks();
  if (taskId) await hydrateActive(taskId, { preserveMount: true });
  // 只对账会话内容，保持输入节点、选择区、当前页面和阅读锚点。
  const task = state.tasks.find(item => item.task_id === taskId);
  const detail = state.details.get(taskId);
  if (task && detail && state.view === "focus" && state.activeTaskId === taskId) {
    replaceConversation(task, detail.messages || [], detail.active_run?.run_id);
    updateMainRecoveryControls();
    await flushComposerPersist();
  }
}));

window.addEventListener("beforeunload", () => {
  stopMapLive();
  workspacePatrolController?.dispose();
  for (const source of state.streams.values()) source.close();
  loopConnection?.stop();
  if (loopLifecycleFrame !== null) cancelAnimationFrame(loopLifecycleFrame);
  loopConsoleController?.destroy();
  materialContentLoader.releaseAll();
  for (const entries of [...state.composerAttachments.values()]) {
    for (const entry of [...entries.values()]) releaseComposerAttachment(entry);
  }
  releaseFileLinkPreview();
});

// 壳层三栏可拖拽：启动即应用持久化宽度/折叠态，并绑定两个 resizer 手柄。
applyShellLayout(state.shellLayout);
bindShellResizers();

// 整页缩放：启动恢复持久化级别（非法/越界回落 100%），播报文案随语言切换。
restoreZoomLevel();

async function openPatrolWorkbench(target) {
  if (target.setupModels) return ensureModelSetup({ onboarding: true });
  cancelPendingViewRequests();
  const navigation = ++workspacePatrolNavigation;
  const workspace = target.workspaceId ? await workspacePatrolIntake.workspace(target.workspaceId) : state.patrolWorkspace;
  if (navigation !== workspacePatrolNavigation) return;
  state.patrolWorkspace = workspace;
  state.view = "patrol";
  render();
  await renderWorkspacePatrol();
  if (navigation !== workspacePatrolNavigation) return;
  if (target.deliveryId) workspacePatrolController?.showDelivery();
  else workspacePatrolController?.showDetails();
  if (target.attentionId) workspacePatrolController?.showAttention(target.attentionId, target.loopId);
  if (target.taskId) await workspacePatrolController?.showContext(target.taskId, target.revisionId);
}
if (desktopPatrol) desktopPatrol.onWorkbench(target => runUiAction(() => openPatrolWorkbench(target)));
window.focusDesktop?.notifications?.onTasks(tasks => {
  replaceTasks(tasks);
  const rail = app.querySelector(".task-workspace-list");
  rail?.querySelectorAll("[data-task-id]").forEach(button => {
    const task = state.tasks.find(item => item.task_id === button.dataset.taskId);
    const status = button.querySelector("small");
    if (task && status) status.textContent = `${task.workspace_name || "本地工作区"} · ${presentTaskStatus(task).label}`;
  });
  // Patch only status badges: polling must not discard composer text or selection.
  app.querySelectorAll(".task-card[data-task-id]").forEach(card => {
    const task = state.tasks.find(item => item.task_id === card.dataset.taskId);
    const badge = card.querySelector(".ui-badge");
    if (!task || !badge) return;
    const status = presentTaskStatus(task);
    badge.textContent = status.label;
    badge.className = `ui-badge is-${status.tone}`;
    card.querySelector(".task-last-result")?.remove();
    if (task.active_run && task.latest_result) {
      const previous = document.createElement("span");
      previous.className = "task-last-result";
      previous.textContent = `上次执行：${presentRunStatus(task.latest_result.status).label}`;
      card.append(previous);
    }
  });
});
async function openTaskNotification(target) {
  await refreshTasks();
  const task = state.tasks.find(item => item.task_id === target.taskId && sessionLifecycle(item) === "active");
  if (!task) return setStatus("该任务已归档或删除，可在设置中查看归档任务。");
  for (const modal of [settingsDialog, dialog]) {
    if (modal?.open) await window.FocusSurfaceTransition.visible(modal, false, { modal: true });
  }
  if (target.workspaceId || (target.loopId && task.loop?.interaction_mode === "workspace_patrol")) {
    await openPatrolWorkbench({ workspaceId: target.workspaceId || task.loop.workspace_id,
      loopId: target.loopId, attentionId: target.attentionId, deliveryId: target.deliveryId });
    return;
  }
  await switchTask(task.task_id);
  if (target.loopId && task.loop?.loop_id === target.loopId) {
    localStorage.setItem(`focus-agent-loop:${task.task_id}`, target.loopId);
    await openLoopView();
    const pending = [...app.querySelectorAll("[data-wait-request-id]")].find(node => node.dataset.waitRequestId === target.attentionId);
    pending?.scrollIntoView?.({ block: "center" });
  } else if (target.attentionId) {
    state.inspector.open = true;
    state.inspector.tab = "run";
    render();
    const pending = document.querySelector(".access-review-panel, .review-panel, .compression-panel, .must-view-panel");
    pending?.scrollIntoView?.({ block: "center" });
  }
}
window.focusDesktop?.notifications?.onOpen(target => runUiAction(() => openTaskNotification(target)));
runUiAction(async () => { await bootstrap(); if (desktopPatrol) await desktopPatrol.ready(); });
