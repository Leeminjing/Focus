/* 本文件提供桌面 Patrol 唯一可编辑面板，组合共享 intake、材料卡与既有 Live 连接。
 * 输入为工作区绑定、预览/要求、明确发送和宿主唤起事件；输出为稳定发送回执、工作区草稿和已提交回答卡。
 * 工作流为先预览，再绑定/发送，按工作区代际订阅与读取；隐藏保留草稿，旧工作区回调不覆盖当前视图。
 * 要求与参考文字分开，Enter/IME 不发送，Ctrl+Enter 显式发送；示例：拖入截图、补充“解释报错”、点击发送。
 * 回答仅取固定 Context revision 的 assistant 正文，进度/产物来自 Live；授权处理沿用工作台，不展示 Patrol 内部理由。
 */
(async function () {
  "use strict";
  if (new URLSearchParams(location.search).get("surface") !== "panel") return;
  document.body.dataset.surface = "panel";
  const host = document.querySelector("#panel"); host.hidden = false;
  const $ = selector => host.querySelector(selector), bridge = window.focusDesktop?.patrol;
  const runtime = window.focusDesktop?.runtime() || { apiBase: location.origin, session: new URLSearchParams(location.search).get("session") || "" };
  const api = FocusLoopApi.create(runtime), live = FocusLoopLiveStore.create();
  const connection = FocusLoopLiveConnection.create({ api, store: live });
  const intake = FocusWorkspacePatrolIntake.create({ runtime, api, selectWorkspace: window.focusDesktop?.selectWorkspace,
    recovery: bridge?.enabled ? bridge.recovery : null,
    accessMode: FocusAccessMode.readNewSessionDefault(localStorage) });
  const preview = FocusWorkspacePatrolMaterialPreview.create({ host, intake, workspaceId: () => workspace?.workspace_id || null, onError: fail });
  let workspace = null, generation = 0, loopId = null, unsubscribe = null, reads = new AbortController();
  let resultKey = "", inputKey = "", publishedState = "", binding = false, nativeReady = false, commandQueue = Promise.resolve();
  const draftStore = () => intake.store(workspace?.workspace_id || null);
  function fail(error) { $("#error").hidden = !error; $("#error").textContent = error ? String(error.message || error) : ""; }
  function enqueue(action) { commandQueue = commandQueue.then(action).catch(fail); return commandQueue; }
  function element(tag, text, parent) { const node = document.createElement(tag); node.textContent = text; parent?.append(node); return node; }
  function action(text, parent, handler) { const button = element("button", text, parent); button.type = "button"; button.addEventListener("click", () => { void Promise.resolve().then(handler).catch(fail); }); return button; }
  function openWorkbench(taskId, revisionId) {
    if (bridge?.enabled) return bridge.openWorkbench({ workspaceId: workspace?.workspace_id, ...(taskId ? { taskId, revisionId } : {}) });
    location.href = `./index.html?session=${encodeURIComponent(runtime.session)}`;
  }
  function notify() {
    const status = FocusWorkspacePatrolFeedback.status(live.get().projection, live.get().connection, draftStore().get());
    $("#status").textContent = workspace ? status.text : "先预览材料，绑定工作区后再发送";
    $("#activity").hidden = !workspace || status.state === "idle";
    $("#activity").textContent = status.text;
    $("#reconnect").hidden = status.state !== "disconnected";
    const state = { workspaceId: workspace?.workspace_id || "", workspaceName: workspace?.display_name || "", state: status.state, dirty: intake.hasUnfinished(), pending: intake.isSending() };
    const key = JSON.stringify(state);
    if (bridge?.enabled && nativeReady && key !== publishedState) { publishedState = key; void bridge.setState(state).catch(fail); }
  }
  function renderInput(model) {
    if ($("#instruction").value !== model.draft.content) $("#instruction").value = model.draft.content;
    preview.render(model.draft);
    $("#reply-target").hidden = !model.draft.request_id;
    $("#reply-target span").textContent = model.draft.request_id ? `回复指定问题 · 版本 ${model.draft.request_revision}` : "";
    const status = { accepted: "已受理", sending: "发送中", failed: "未确认", edited: "已转为草稿" };
    $("#receipt").textContent = status[model.rows[0]?.status] || "";
    const items = $("#history-items"); items.replaceChildren();
    for (const row of model.rows) {
      const card = element("article", "", items);
      element("pre", row.content || "仅参考材料", card);
      element("small", `${status[row.status]}${row.attachments?.length ? ` · ${row.attachments.length} 份材料` : ""}`, card);
      if (row.status === "failed") {
        element("small", row.error, card);
        const id = workspace?.workspace_id;
        action("按原内容重试", card, () => send(() => intake.retry(id, row.submission_id), id));
        action("编辑为新输入", card, () => intake.editFailed(workspace?.workspace_id, row.submission_id));
      }
    }
    $("#older").hidden = !model.cursor;
    $("#send").disabled = !model.draft.content.trim() && !model.draft.attachments?.length;
    notify();
  }
  function watchDraft() { unsubscribe?.(); unsubscribe = draftStore().subscribe(renderInput); }
  function connect(id) {
    if (!id || id === loopId) return;
    reads.abort(); reads = new AbortController(); resultKey = "";
    $("#results").replaceChildren();
    loopId = id;
    void connection.start(id).catch(error => { if (loopId === id) fail(error); });
  }
  async function activate(bound) {
    if (bound?.workspace_id === workspace?.workspace_id && workspace) return;
    const token = ++generation;
    reads.abort(); reads = new AbortController(); connection.stop(); loopId = null;
    workspace = bound; resultKey = ""; inputKey = "";
    $("#results").replaceChildren(); $("#requests").replaceChildren(); fail(null);
    if (bound) { intake.remember(bound); intake.claimUnbound(bound.workspace_id); }
    $("#binding-name").textContent = bound ? bound.display_name : "绑定工作区";
    $("#binding").title = bound?.path || "选择工作区文件夹";
    watchDraft();
    if (!bound) return;
    const [loop, page] = await Promise.all([api.workspacePatrol(bound.workspace_id), api.workspaceInputs(bound.workspace_id)]);
    if (token !== generation) return;
    intake.history(bound.workspace_id, page, false, true);
    if (loop) connect(loop.loop_id);
    await intake.reconcile(bound.workspace_id, page);
  }
  async function choose() {
    if (binding) return false;
    binding = true; $("#binding").disabled = true;
    try {
      const path = await intake.choose();
      if (!path) return false;
      await activate(await intake.bind(path)); return true;
    } finally { binding = false; $("#binding").disabled = false; }
  }
  async function send(operation, id) {
    fail(null);
    try {
      const promise = operation(); notify();
      const receipt = await promise;
      if (workspace?.workspace_id === id && receipt?.loop_id) {
        const current = await api.workspacePatrol(id);
        if (workspace?.workspace_id === id) connect(current?.loop_id || receipt.loop_id);
      }
    } finally { notify(); }
  }
  async function submit() {
    if (!workspace) { await choose(); return; }
    const id = workspace.workspace_id;
    // 写盘失败后，显式发送必须再次保存眼前的文字，不能发送回滚后的旧草稿。
    if (draftStore().get().draft.content !== $("#instruction").value) draftStore().edit({ content: $("#instruction").value });
    const visible = FocusWorkspacePatrolInputs.captureVisibleContext({ projection: live.get().projection });
    await send(() => intake.submit(id, visible), id);
  }
  function renderRequests(projection) {
    const container = $("#requests"); container.replaceChildren();
    for (const entity of Object.values(projection?.wait_requests || {})) {
      const request = entity.state;
      if (request.status !== "open") continue;
      const card = element("article", "", container);
      element("h2", request.scope?.information_only ? "需要补充一点信息" : "有一项工作需要处理", card);
      element("p", request.prompt || "打开工作台查看", card);
      if (request.scope?.information_only) action("回复这个问题", card, () => {
        draftStore().edit({ request_id: entity.entity_id, request_revision: entity.revision }); $("#instruction").focus();
      }); else action("去工作台处理 ↗", card, () => openWorkbench());
    }
  }
  async function renderResults(projection, key, token, signal) {
    const cards = await FocusWorkspacePatrolFeedback.results(api, projection, signal);
    if (token !== generation || key !== resultKey || projection.loop_id !== loopId) return;
    const container = $("#results"); container.replaceChildren();
    for (const result of cards) {
      const card = element("article", "", container);
      element("h2", result.title, card); element("pre", result.text || result.error, card);
      const source = action("查看来源 ↗", card, () => openWorkbench(result.context_id, result.revision_id));
      source.title = `Context ${result.context_id} · Revision ${result.revision_id}`;
      source.dataset.contextId = result.context_id; source.dataset.revisionId = result.revision_id;
    }
    const progress = projection.task_progress?.state?.document?.items || [];
    if (progress.length) {
      const card = element("article", "", container); element("h2", "已提交进度", card);
      const verified = progress.filter(item => item.state === "completed" && item.support === "supported").length;
      element("p", `${verified} / ${progress.length} 项已有验证证据`, card); action("查看进度 ↗", card, () => openWorkbench());
    }
    const files = FocusLoopLiveSelectors.selectFacts(projection).filter(item => item.kind === "file" || item.kind === "artifact").slice(0, 3);
    if (files.length) { const card = element("article", "", container); element("h2", `${files.length} 项产物更新`, card); action("查看文件和来源 ↗", card, () => openWorkbench()); }
  }
  live.subscribe(({ projection }) => {
    notify();
    if (!projection || projection.loop_id !== loopId) return;
    renderRequests(projection);
    const artifacts = FocusLoopLiveSelectors.selectFacts(projection).filter(item => ["file", "artifact"].includes(item.kind)).map(item => [item.fact_id, item.revision]);
    const key = JSON.stringify([FocusWorkspacePatrolFeedback.signature(projection), projection.task_progress?.revision, artifacts]);
    if (key !== resultKey) {
      resultKey = key;
      void renderResults(projection, key, generation, reads.signal).catch(error => { if (error.name !== "AbortError") fail(error); });
    }
    const next = JSON.stringify(projection.interventions);
    if (next !== inputKey && workspace) {
      inputKey = next; const token = generation, id = workspace.workspace_id;
      void api.workspaceInputs(id).then(page => { if (token === generation) intake.history(id, page); }).catch(error => { if (token === generation) fail(error); });
    }
  });
  $("#compose").addEventListener("submit", event => { event.preventDefault(); void submit().catch(fail); });
  $("#instruction").addEventListener("input", event => {
    try { draftStore().edit({ content: event.target.value }); fail(null); } catch (error) { fail(new Error(`草稿保存失败：${error.message}`)); }
  });
  $("#instruction").addEventListener("keydown", event => {
    if (event.key === "Enter" && event.ctrlKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); $("#compose").requestSubmit(); }
  });
  $("#binding").addEventListener("click", () => { void enqueue(choose); });
  $("#clear-reply").addEventListener("click", () => draftStore().edit({ request_id: null, request_revision: null }));
  $("#hide").addEventListener("click", () => bridge?.hidePanel());
  $("#toggle-history").addEventListener("click", () => {
    const history = $("#history"); history.hidden = !history.hidden; history.open = !history.hidden;
    $("#toggle-history").setAttribute("aria-expanded", String(!history.hidden));
    $(".panel-menu").open = false;
  });
  document.addEventListener("click", event => { if (!event.target.closest(".panel-menu")) $(".panel-menu").open = false; });
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    if ($(".panel-menu").open) { $(".panel-menu").open = false; $(".panel-menu summary").focus(); }
    else bridge?.hidePanel();
  });
  $("#workbench").addEventListener("click", () => { void openWorkbench(); });
  $("#reconnect").addEventListener("click", () => { const id = loopId; loopId = null; connect(id); });
  $("#add-material").addEventListener("click", () => $("#file-picker").click());
  $("#file-picker").addEventListener("change", async event => {
    const id = workspace?.workspace_id || null, files = [...event.target.files]; event.target.value = "";
    try { await intake.add(id, await FocusWorkspacePatrolMaterialPreview.readTransfer({ files }, await intake.resourceLimits())); } catch (error) { fail(error); }
  });
  $("#older").addEventListener("click", async () => {
    const id = workspace?.workspace_id, store = draftStore(), before = store.get().cursor;
    if (!id || !before) return;
    $("#older").disabled = true;
    try { intake.history(id, await api.workspaceInputs(id, before), true); } catch (error) { fail(error); } finally { $("#older").disabled = false; }
  });
  watchDraft();
  const sizeObserver = new ResizeObserver(() => {
    const contentHeight = $(".panel-header").offsetHeight + $(".panel-content").offsetHeight + 2;
    const menuHeight = $(".panel-menu").open ? $(".panel-menu-content").getBoundingClientRect().bottom + 10 : 0;
    const height = Math.ceil(Math.max(contentHeight, menuHeight));
    if (bridge?.enabled) void bridge.resizePanel(height).catch(fail);
  });
  sizeObserver.observe($(".panel-content"));
  sizeObserver.observe($(".panel-menu-content"));
  await intake.resourceLimits();
  if (bridge?.enabled) {
    bridge.onOpen(target => enqueue(async () => {
      if (target.workspaceId && target.workspaceId !== workspace?.workspace_id) await activate(await intake.workspace(target.workspaceId));
      if (target.requestId) draftStore().edit({ request_id: target.requestId, request_revision: target.requestRevision });
    }));
    bridge.onContent(payload => enqueue(async () => {
      const id = payload.workspaceId || null;
      await intake.add(id, payload.items);
      if (id !== (workspace?.workspace_id || null)) fail(new Error("材料已保留在拖入时工作区的草稿中，切换回该工作区即可查看"));
      notify();
    }));
    const ready = await bridge.ready();
    await enqueue(async () => {
      try {
        const id = intake.readBinding()?.workspace_id || ready.state.workspaceId;
        if (!workspace && id) await activate(await intake.workspace(id));
      }
      finally { nativeReady = true; notify(); }
    });
  } else if (intake.readBinding()) await activate(await intake.workspace(intake.readBinding().workspace_id));
  window.addEventListener("beforeunload", () => { sizeObserver.disconnect(); preview.dispose(); connection.stop(); reads.abort(); });
})().catch(error => { const node = document.querySelector("#error"); node.hidden = false; node.textContent = error.message; });
