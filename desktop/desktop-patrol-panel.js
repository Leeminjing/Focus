/* 本文件提供桌面 Patrol 唯一可编辑面板，组合共享 intake、材料卡与既有 Live 连接。
 * 输入为工作区绑定、预览/要求、明确发送和宿主唤起事件；输出为稳定发送回执、工作区草稿和已提交回答卡。
 * 工作流为先预览，再绑定/发送，按工作区代际订阅与读取；隐藏保留草稿，旧工作区回调不覆盖当前视图。
 * 要求与参考文字分开，Enter/IME 不发送，Ctrl+Enter 显式发送；示例：拖入截图、补充“解释报错”、点击发送。
 * 回答仅取固定 Context revision 的 assistant 正文并有界缓存，进度/产物分别来自 Live；隐藏时保留 Live 状态同步，取消正文读取并暂停结果 DOM 更新。
 * 回答独立读取和重试，保留其他卡片 DOM；展开后按需渲染 Markdown 并提供全文/代码复制，文件直接打开固定版本或加入引用。
 * 原生显隐由宿主通知，浏览器回退用 visibilitychange；再次显示时读取最新投影。授权处理沿用工作台，不展示 Patrol 内部理由。
 * 状态旁原位暂停/继续，停止收进菜单；复用 Loop control，区分 HTTP 受理与 Live 生效，隔离工作区代际并阻止在途重复控制。
 */
(async function () {
  "use strict";
  if (new URLSearchParams(location.search).get("surface") !== "panel") return;
  document.body.dataset.surface = "panel";
  const host = document.querySelector("#panel"); host.hidden = false;
  const $ = selector => host.querySelector(selector), bridge = window.focusDesktop?.patrol;
  host.prepend($("#version-picker"),$("#version-detail"));
  const runtime = window.focusDesktop?.runtime() || { apiBase: location.origin, session: new URLSearchParams(location.search).get("session") || "" };
  const api = FocusLoopApi.create(runtime), live = FocusLoopLiveStore.create();
  const connection = FocusLoopLiveConnection.create({ api, store: live });
  const feedback = FocusWorkspacePatrolFeedback.createReader(api);
  const markdown = window.markdownit({ html:false, linkify:true });
  const answerCards = new Map();
  const intake = FocusWorkspacePatrolIntake.create({ runtime, api, selectWorkspace: window.focusDesktop?.selectWorkspace,
    recovery: bridge?.enabled ? bridge.recovery : null,
    onSaveError: error => fail(new Error(`草稿保存失败：${error.message}`)),
    accessMode: FocusAccessMode.readNewSessionDefault(localStorage) });
  const preview = FocusWorkspacePatrolMaterialPreview.create({ host, intake, workspaceId: () => workspace?.workspace_id || null, onError: fail, compact:true });
  let workspace = null, workspaceLoaded = false, generation = 0, loopId = null, unsubscribe = null, reads = new AbortController();
  let resultKey = "", inputKey = "", publishedState = "", binding = false, nativeReady = false, shownFailure = "", closeEpoch = 0, commandQueue = Promise.resolve();
  let historyKey = "", requestKey = "", progressKey = "", artifactKey = "";
  let panelVisible = bridge?.enabled ? false : !document.hidden, visibilityKnown = false, resultsNeedReset = false;
  let control = null;
  const draftStore = () => intake.store(workspace?.workspace_id || null);
  const references = FocusDesktopPatrolReferences.create({ host, api, store:draftStore, loopId:() => loopId, onError:fail });
  function fail(error) { $("#error").hidden = !error; $("#error").textContent = error ? String(error.message || error) : ""; if (!error) $("#retry-workspace").hidden = true; }
  let budget = null;
  function resetBudget() {
    budget?.dispose();
    $("#task-budget").hidden = !workspace;
    $("#task-budget").innerHTML = FocusWorkspacePatrolBudget.markup();
    budget = FocusWorkspacePatrolBudget.create({ host: $("#task-budget"), api,
      getDraft: () => draftStore().get().draft, editDraft: patch => draftStore().edit(patch), onError: fail });
    budget.update(null);
  }
  function enqueue(action) { commandQueue = commandQueue.then(action).catch(fail); return commandQueue; }
  function element(tag, text, parent) { const node = document.createElement(tag); node.textContent = text; parent?.append(node); return node; }
  function action(text, parent, handler) { const button = element("button", text, parent); button.type = "button"; button.addEventListener("click", () => { void Promise.resolve().then(handler).catch(fail); }); return button; }
  function openWorkbench(taskId, revisionId) {
    if (bridge?.enabled) return bridge.openWorkbench({ workspaceId: workspace?.workspace_id, ...(taskId ? { taskId, revisionId } : {}) });
    location.href = `./index.html?session=${encodeURIComponent(runtime.session)}`;
  }
  function notify() {
    const current = live.get(), projection = current.projection?.loop_id === loopId ? current.projection : null;
    const status = FocusWorkspacePatrolFeedback.status(projection, current.connection, draftStore().get());
    const loop = projection?.loop?.state;
    const pausable = loop?.status === "running" || (loop?.status === "waiting_user" && loop.waiting_reason === "awaiting_input");
    const resumable = loop?.status === "paused";
    if (control?.phase === "accepted") {
      // HTTP acceptance is separate from the canonical Live state. A newer
      // revision may already have superseded the requested state (e.g. resume).
      if (loop?.status === control.target && (control.revision == null || loop.revision >= control.revision)) control.phase = "applied";
      else if (control.revision != null && loop?.revision > control.revision) control = null;
    }
    if (control?.phase === "applied" && loop?.status !== control.target) control = null;
    const pendingControl = control && control.phase !== "applied";
    const toggle = $("#toggle-work");
    toggle.hidden = !pausable && !resumable;
    toggle.textContent = resumable ? "继续" : "暂停";
    toggle.dataset.command = resumable ? "resume" : "pause";
    toggle.setAttribute("aria-label", resumable ? "继续当前工作" : "暂停当前工作");
    toggle.disabled = !!pendingControl || current.connection.status !== "live" || (!pausable && !resumable);
    $("#stop-work").disabled = !!pendingControl || current.connection.status !== "live" || !["running", "paused", "waiting_user"].includes(loop?.status);
    for (const button of [toggle, $("#stop-work")]) {
      if (pendingControl) button.setAttribute("aria-busy", "true");
      else button.removeAttribute("aria-busy");
    }
    $("#control-feedback").hidden = !control;
    $("#control-feedback").textContent = control ? `${{ pause: "暂停", resume: "继续", stop: "停止" }[control.command]}${{ requesting: "请求中…", accepted: "已受理 · 等待工作状态更新", applied: "已生效" }[control.phase]}` : "";
    $("#status").textContent = workspace ? status.text : "准备接收新输入";
    $("#activity").hidden = !workspace || (status.state === "idle" && !pausable && !resumable);
    $("#activity-text").textContent = status.text; $("#activity-text").title = status.text;
    host.dataset.state = status.state;
    $("#activity-reconnect").hidden = status.state !== "disconnected";
    $("#reconnect").hidden = status.state !== "disconnected";
    const state = { workspaceId: workspace?.workspace_id || "", workspaceName: workspace?.display_name || "", state: status.state, dirty: intake.hasUnfinished(), pending: intake.isSending() };
    const key = JSON.stringify(state);
    if (bridge?.enabled && nativeReady && key !== publishedState) { publishedState = key; void bridge.setState(state).catch(fail); }
  }
  async function controlWork(command) {
    const button = command === "stop" ? $("#stop-work") : $("#toggle-work");
    if (!loopId || button.disabled || button.hidden) return;
    const owner = { loopId, command, target: { pause: "paused", resume: "running", stop: "stopped" }[command], phase: "requesting" };
    const id = loopId, token = generation;
    control = owner; fail(null); notify();
    $(".panel-menu").open = false;
    try {
      const receipt = await api.control(id, command);
      if (token !== generation || id !== loopId || control !== owner) return;
      owner.phase = "accepted"; owner.revision = receipt?.revision;
    } catch (error) {
      if (token !== generation || id !== loopId || control !== owner) return;
      control = null; fail(error);
    } finally { if (token === generation && id === loopId) notify(); }
  }
  function renderInput(model) {
    budget?.syncDraft();
    if ($("#instruction").value !== model.draft.content) $("#instruction").value = model.draft.content;
    preview.render(model.draft);
    references.render(model.draft);
    $("#instruction").style.height = "36px";
    $("#instruction").style.height = `${Math.max(36, Math.min(108, $("#instruction").scrollHeight))}px`;
    $("#reply-target").hidden = !model.draft.request_id;
    $("#reply-target span").textContent = model.draft.request_id ? `回复指定问题 · 版本 ${model.draft.request_revision}` : "";
    const status = { accepted: "已受理", sending: "发送中", failed: "未确认", edited: "已转为草稿" };
    $("#receipt").textContent = status[model.rows[0]?.status] || "";
    const nextHistory = `${workspace?.workspace_id || ""}:${model.historyRevision}`;
    if (historyKey !== nextHistory) {
      historyKey = nextHistory;
      const items = $("#history-items"); items.replaceChildren();
      for (const row of model.rows) {
        const card = element("article", "", items);
        element("pre", row.content || "仅参考材料", card);
        const count = (row.attachments?.length || 0) + (row.file_refs?.length || 0);
        element("small", `${status[row.status]}${count ? ` · ${count} 项引用内容` : ""}`, card);
        if (row.status === "failed") {
          element("small", row.error, card);
          const id = workspace?.workspace_id;
          action("按原内容重试", card, () => send(() => intake.retry(id, row.submission_id), id));
          action("编辑为新输入", card, () => intake.editFailed(id, row.submission_id));
        }
      }
    }
    $("#older").hidden = !model.cursor;
    if (model.rows[0]?.status === "failed" && shownFailure !== model.rows[0].submission_id) {
      shownFailure = model.rows[0].submission_id;
      $("#history").hidden = false; $("#history").open = true;
      $("#toggle-history").setAttribute("aria-expanded", "true");
    }
    $("#send").disabled = !model.draft.content.trim() && !model.draft.attachments?.length;
    notify();
  }
  function watchDraft() { unsubscribe?.(); unsubscribe = draftStore().subscribe(renderInput); }
  function cancelResults() { reads.abort(); reads = new AbortController(); resultKey = ""; }
  function resetResults() {
    cancelResults(); requestKey = ""; progressKey = ""; artifactKey = ""; resultsNeedReset = true;
    if (panelVisible) refreshVisible();
  }
  function syncReading() {
    host.dataset.reading = String([...$("#answers").querySelectorAll(".answer-reading")].some(node => node.open));
  }
  function connect(id) {
    if (!id || id === loopId) return;
    if (control?.loopId !== id) control = null;
    loopId = id;
    resetResults();
    void connection.start(id).catch(error => { if (loopId === id) fail(error); });
  }
  async function activate(bound) {
    if (bound?.workspace_id === workspace?.workspace_id && workspace && workspaceLoaded) return;
    const token = ++generation;
    control = null;
    loopId = null; resetResults(); connection.stop();
    workspace = bound; workspaceLoaded = false; inputKey = "";
    references.close();
    $("#reference-file").disabled = !bound;
    resetBudget();
    fail(null);
    if (bound) { intake.remember(bound); intake.claimUnbound(bound.workspace_id); }
    $("#binding-name").textContent = bound ? bound.display_name : "绑定工作区";
    $("#binding").title = bound?.path || "选择工作区文件夹";
    watchDraft();
    if (!bound) return;
    try {
      const [loop, page] = await Promise.all([api.workspacePatrol(bound.workspace_id), api.workspaceInputs(bound.workspace_id)]);
      if (token !== generation) return;
      intake.history(bound.workspace_id, page, false, true);
      workspaceLoaded = true;
      if (loop) connect(loop.loop_id);
      await intake.reconcile(bound.workspace_id, page);
    } catch (error) {
      if (token !== generation) return;
      $("#retry-workspace").hidden = false; throw error;
    }
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
      if (workspace?.workspace_id === id && receipt?.loop_id) connect(receipt.loop_id);
    } catch (error) {
      if (workspace?.workspace_id !== id) return;
      // The input row already owns its transport error and original-identity
      // retry. Keep one recovery surface instead of duplicating it above results.
      if (draftStore().get().rows.some(row => row.status === "failed")) { fail(null); $(".panel-feedback").scrollTop = 0; }
      else throw error;
    } finally { notify(); }
  }
  async function submit() {
    if (!workspace) { await choose(); return; }
    references.close();
    const id = workspace.workspace_id;
    // 显式发送以眼前的文字为准，发送流程会等待本地持久化成功。
    if (draftStore().get().draft.content !== $("#instruction").value) draftStore().edit({ content: $("#instruction").value });
    const visible = FocusWorkspacePatrolInputs.captureVisibleContext({ projection: live.get().projection });
    await send(() => intake.submit(id, visible), id);
  }
  async function hidePanel() {
    await intake.flush();
    const owner = ++closeEpoch;
    host.classList.remove("is-opening"); host.classList.add("is-closing"); host.inert = true;
    await Promise.all(host.getAnimations().map(animation => animation.finished.catch(() => {})));
    if (owner !== closeEpoch) return;
    try { await bridge?.hidePanel(); }
    finally { if (owner === closeEpoch) { host.classList.remove("is-closing"); host.inert = false; } }
  }
  function renderRequests(projection) {
    const next = JSON.stringify([workspace?.workspace_id, projection?.wait_requests]);
    if (requestKey === next) return;
    requestKey = next;
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
  function renderAnswer(record, result) {
    const card = record.node; card.replaceChildren(); card.hidden = !result;
    record.refreshReading = null;
    if (!result) return;
    element("h2", result.title, card);
    if (result.error) {
      element("p", result.error, card).setAttribute("role", "status");
      action("重试读取", card, () => record.retry());
    } else {
      const reading = element("details", "", card); reading.className = "answer-reading";
      const summary = element("summary", "", reading);
      element("span", result.text.replace(/\s+/g, " ").slice(0, 200), summary).className = "answer-preview";
      const label = element("span", "展开阅读", summary); label.className = "answer-toggle";
      const content = element("div", "", reading); content.className = "answer-content";
      let rendered = false;
      function copyButton(text, parent, value) {
        const button = action(text, parent, async () => {
          await navigator.clipboard.writeText(value);
          button.textContent = "已复制";
        });
        button.setAttribute("aria-label", text); button.setAttribute("aria-live", "polite");
        button.addEventListener("blur", () => { button.textContent = text; });
      }
      function updateReading() {
        if (!panelVisible) return;
        label.textContent = reading.open ? "收起阅读" : "展开阅读";
        if (reading.open && !rendered) {
          rendered = true;
          copyButton("复制全文", content, result.text);
          const body = element("div", "", content); body.className = "answer-markdown";
          body.innerHTML = markdown.render(result.text);
          body.addEventListener("click", event => {
            const href = event.target.closest("a[href]")?.getAttribute("href");
            if (!href || !/^https?:\/\//i.test(href)) return;
            if (window.focusDesktop?.openExternal) {
              event.preventDefault();
              void Promise.resolve(window.focusDesktop.openExternal(href)).catch(fail);
            }
          });
          for (const pre of body.querySelectorAll("pre")) {
            const code = pre.querySelector("code");
            if (!code) continue;
            const block = document.createElement("div"); block.className = "answer-code";
            pre.replaceWith(block); copyButton("复制代码", block, code.textContent); block.append(pre);
          }
        }
        syncReading();
      }
      record.refreshReading = updateReading;
      reading.addEventListener("toggle", updateReading);
    }
    const details = element("details", "", card); details.className = "answer-source";
    element("summary", "来源与版本", details);
    element("small", `Context ${result.context_id} · Revision ${result.revision_id}`, details);
    const source = action("查看来源 ↗", details, () => openWorkbench(result.context_id, result.revision_id));
    source.title = `Context ${result.context_id} · Revision ${result.revision_id}`;
    source.dataset.contextId = result.context_id; source.dataset.revisionId = result.revision_id;
  }
  function renderResults(projection, key, token, signal) {
    const container = $("#answers"), entries = feedback.entries(projection);
    const identity = result => JSON.stringify([projection.loop_id, result.context_id, result.revision_id]);
    const wanted = new Set(entries.map(identity));
    for (const [id, record] of answerCards) if (!wanted.has(id)) { record.node.remove(); answerCards.delete(id); }
    const current = () => !signal.aborted && panelVisible && token === generation && key === resultKey && projection.loop_id === loopId;
    entries.forEach((entry, index) => {
      const id = identity(entry);
      let record = answerCards.get(id);
      if (!record) {
        const node = document.createElement("article"); node.dataset.answerContext = entry.context_id;
        record = { node, status:"pending" }; answerCards.set(id, record);
      }
      if (container.children[index] !== record.node) container.insertBefore(record.node, container.children[index] || null);
      record.entry = entry;
      const title = record.node.querySelector("h2"); if (title) title.textContent = entry.title;
      record.retry = async () => {
        if (!current() || record.status === "loading") return;
        record.status = "loading"; record.node.hidden = false;
        record.node.replaceChildren(); element("h2", record.entry.title, record.node);
        element("p", "正在读取回答…", record.node).setAttribute("role", "status");
        syncReading();
        try {
          const result = await feedback.read(projection.loop_id, record.entry, signal);
          if (!current()) return;
          record.status = result?.error ? "failed" : "ready";
          renderAnswer(record, result ? { ...result, title:record.entry.title } : null); syncReading();
        } catch (error) { if (current() && error.name !== "AbortError") fail(error); }
      };
      // A signature change aborts the preceding batch, including still-loading cards.
      if (["pending", "loading"].includes(record.status)) { record.status = "pending"; void record.retry(); }
      else record.refreshReading?.();
    });
    syncReading();
  }
  function refreshVisible() {
    if (!panelVisible) return;
    if (resultsNeedReset) {
      for (const selector of ["#answers", "#progress", "#artifacts", "#requests"]) $(selector).replaceChildren();
      answerCards.clear(); syncReading();
      resultsNeedReset = false;
    }
    const { projection } = live.get();
    if (!projection || projection.loop_id !== loopId) return;
    budget?.update(projection);
    renderRequests(projection);
    const progress = projection.task_progress?.state?.document?.items || [];
    const verified = progress.filter(item => item.state === "completed" && item.support === "supported").length;
    const nextProgress = JSON.stringify([verified, progress.length]);
    if (nextProgress !== progressKey) {
      progressKey = nextProgress;
      const container = $("#progress"); container.replaceChildren();
      if (progress.length) {
        const card = element("article", "", container); element("h2", "已提交进度", card);
        element("p", `${verified} / ${progress.length} 项已有验证证据`, card); action("查看进度 ↗", card, () => openWorkbench());
      }
    }
    const files = FocusLoopLiveSelectors.selectFacts(projection).filter(item => item.kind === "file" || item.kind === "artifact").slice(0, 3);
    const nextArtifacts = JSON.stringify(files.map(item => [item.fact_id, item.revision]));
    if (nextArtifacts !== artifactKey) {
      artifactKey = nextArtifacts;
      const container = $("#artifacts"); container.replaceChildren();
      if (files.length) {
        const card = element("article", "", container); element("h2", `${files.length} 项产物更新`, card);
        for (const fact of files) {
          const file = FocusLoopFactsView.fileReference(fact), row = element("div", "", card); row.className = "artifact-result";
          const name = element("span", file?.path?.split(/[\\/]/).at(-1) || fact.title || "产物", row); name.title = file?.path || fact.title || "产物";
          const actions = element("div", "", row); actions.className = "artifact-actions";
          action("打开", actions, () => file?.path && file.result_key ? references.detail(file) : openWorkbench(fact.context_id, fact.revision_id));
          if (file?.path && file.result_key) action("引用", actions, () => references.reference(file));
        }
        action("查看全部文件和来源 ↗", card, () => openWorkbench());
      }
    }
    const key = FocusWorkspacePatrolFeedback.signature(projection);
    if (key !== resultKey) {
      cancelResults();
      resultKey = key;
      const token = generation, signal = reads.signal;
      renderResults(projection, key, token, signal);
    }
  }
  function setVisibility(visible) {
    if (!visible) void intake.flush().catch(fail);
    visibilityKnown = true;
    if (panelVisible === visible) return;
    panelVisible = visible;
    if (!visible) cancelResults();
    else refreshVisible();
  }
  if (!bridge?.enabled) document.addEventListener("visibilitychange", () => setVisibility(!document.hidden));
  live.subscribe(({ projection }) => {
    notify();
    refreshVisible();
    if (!projection || projection.loop_id !== loopId) return;
    const next = JSON.stringify(projection.interventions);
    if (next !== inputKey && workspace) {
      inputKey = next; const token = generation, id = workspace.workspace_id;
      void api.workspaceInputs(id).then(page => { if (token === generation) intake.history(id, page); }).catch(error => { if (token === generation) fail(error); });
    }
  });
  $("#compose").addEventListener("submit", event => { event.preventDefault(); void submit().catch(fail); });
  $("#instruction").addEventListener("input", event => {
    try { draftStore().edit({ content: event.target.value }); fail(null); if (!event.isComposing) references.input(); }
    catch (error) { fail(new Error(`草稿保存失败：${error.message}`)); }
  });
  $("#instruction").addEventListener("compositionend", () => references.input());
  $("#instruction").addEventListener("keydown", event => {
    if (event.key === "Enter" && event.ctrlKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); $("#compose").requestSubmit(); }
  });
  $("#binding").addEventListener("click", () => { void enqueue(choose); });
  $("#clear-reply").addEventListener("click", () => draftStore().edit({ request_id: null, request_revision: null }));
  $("#hide").addEventListener("click", () => { void hidePanel().catch(fail); });
  $("#toggle-history").addEventListener("click", () => {
    const history = $("#history"); history.hidden = !history.hidden; history.open = !history.hidden;
    $("#toggle-history").setAttribute("aria-expanded", String(!history.hidden));
    $(".panel-menu").open = false;
  });
  document.addEventListener("click", event => { if (!event.target.closest(".panel-menu")) $(".panel-menu").open = false; });
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    if ($(".panel-menu").open) { $(".panel-menu").open = false; $(".panel-menu summary").focus(); }
    else if (references.isOpen()) references.close(true);
    else { void hidePanel().catch(fail); }
  });
  $("#workbench").addEventListener("click", () => { void openWorkbench(); });
  $("#toggle-work").addEventListener("click", () => { void controlWork($("#toggle-work").dataset.command); });
  $("#stop-work").addEventListener("click", () => { void controlWork("stop"); });
  $("#reconnect").addEventListener("click", () => { const id = loopId; loopId = null; connect(id); });
  $("#activity-reconnect").addEventListener("click", () => $("#reconnect").click());
  $("#retry-workspace").addEventListener("click", () => { void enqueue(() => activate(workspace)); });
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
  resetBudget();
  watchDraft();
  const sizeObserver = new ResizeObserver(() => {
    const detail = !$("#version-detail").hidden, beside = detail && matchMedia("(min-width:580px)").matches;
    const popupHeight = [$("#version-picker"),$("#version-detail")].reduce((sum,node) => sum + (node.hidden || (beside && node.id === "version-detail") ? 0 : node.offsetHeight + 8),0);
    const shellStyle = getComputedStyle($(".panel-shell"));
    const contentStyle = getComputedStyle($(".panel-content"));
    const composeHeight = $(".panel-header").offsetHeight + $("#compose").offsetHeight + parseFloat(contentStyle.paddingTop) + parseFloat(contentStyle.paddingBottom) + parseFloat(shellStyle.borderTopWidth) + parseFloat(shellStyle.borderBottomWidth);
    const popupRoom = `${Math.max(80,Math.min(500,screen.availHeight)-composeHeight-8)}px`;
    if (host.style.getPropertyValue("--popup-room") !== popupRoom) host.style.setProperty("--popup-room",popupRoom);
    const contentHeight = composeHeight + $(".panel-feedback-content").offsetHeight + popupHeight;
    const menuHeight = $(".panel-menu").open ? $(".panel-menu-content").offsetTop + $(".panel-menu-content").offsetHeight + 10 : 0;
    const gutter = parseFloat(getComputedStyle(host).paddingTop);
    const height = Math.round(Math.max(contentHeight, menuHeight, beside ? $("#version-detail").offsetHeight + 56 : 0)+gutter*2);
    if (bridge?.enabled) void bridge.resizePanel({height,width:(detail ? 590 : host.dataset.reading === "true" ? 520 : 288)+gutter*2,animate:!matchMedia("(prefers-reduced-motion:reduce)").matches}).catch(fail);
  });
  sizeObserver.observe(host);
  sizeObserver.observe($(".panel-content"));
  sizeObserver.observe($("#compose"));
  sizeObserver.observe($(".panel-feedback-content"));
  sizeObserver.observe($(".panel-menu-content"));
  sizeObserver.observe($("#version-picker"));
  sizeObserver.observe($("#version-detail"));
  if (bridge?.enabled) {
    bridge.recovery?.onFlush(async token => {
      let error = null;
      try { await intake.flush(); } catch (failure) { fail(failure); error = failure.message; }
      await bridge.recovery.flushed({ token, error });
    });
    bridge.onVisibility(setVisibility);
    bridge.onOpen(target => {
      closeEpoch++; host.inert = false; host.classList.remove("is-closing");
      host.classList.remove("is-opening");
      const owner = closeEpoch;
      requestAnimationFrame(() => { if (owner === closeEpoch) host.classList.add("is-opening"); });
      return enqueue(async () => {
        if (target.workspaceId) await activate(target.workspaceId === workspace?.workspace_id ? workspace : await intake.workspace(target.workspaceId));
        else if (workspace && !workspaceLoaded) await activate(workspace);
        if (target.requestId) draftStore().edit({ request_id: target.requestId, request_revision: target.requestRevision });
        if (target.file_refs?.length) {
          const refs = new Map(draftStore().get().draft.file_refs.map(file => [file.result_key,file]));
          target.file_refs.forEach(file => refs.set(file.result_key,file));
          if (refs.size > 32) throw new Error("一次输入最多引用 32 个文件版本");
          draftStore().edit({file_refs:[...refs.values()]});
        }
      });
    });
    bridge.onContent(payload => enqueue(async () => {
      const id = payload.workspaceId || null;
      await intake.add(id, payload.items);
      if (id !== (workspace?.workspace_id || null)) fail(new Error("材料已保留在拖入时工作区的草稿中，切换回该工作区即可查看"));
      notify();
    }));
    const ready = await bridge.ready();
    if (!visibilityKnown) setVisibility(ready.panelVisible === true);
    await enqueue(async () => {
      try {
        const id = intake.readBinding()?.workspace_id || ready.state.workspaceId;
        if (!workspace && id) await activate(await intake.workspace(id));
      }
      finally { nativeReady = true; notify(); }
    });
  } else if (intake.readBinding()) await activate(await intake.workspace(intake.readBinding().workspace_id));
  window.addEventListener("beforeunload", () => { budget?.dispose(); sizeObserver.disconnect(); references.dispose(); preview.dispose(); connection.stop(); reads.abort(); });
})().catch(error => { const node = document.querySelector("#error"); node.hidden = false; node.textContent = error.message; });
