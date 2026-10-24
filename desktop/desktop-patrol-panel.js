/* 本文件提供桌面 Patrol 唯一可编辑面板，组合共享 intake、材料卡与既有 Live 连接。
 * 输入为工作区绑定、预览/要求、明确发送和宿主唤起事件；输出为稳定发送回执、工作区草稿和已提交回答卡。
 * 工作流为先预览，再绑定/发送，按工作区代际订阅与读取；隐藏保留草稿，旧工作区回调不覆盖当前视图。
 * 要求与参考文字分开，Enter/IME 不发送，Ctrl+Enter 显式发送；示例：拖入截图、补充“解释报错”、点击发送。
 * 回答仅取固定 Context revision 的 assistant 正文并有界缓存，进度/产物分别来自 Live；隐藏时保留 Live 状态同步，取消正文读取并暂停结果 DOM 更新。
 * 回答独立读取和重试，保留其他卡片 DOM；展开后按需渲染 Markdown 并提供全文/代码复制，文件直接打开固定版本或加入引用。
 * 原生显隐由宿主通知，浏览器回退用 visibilitychange；再次显示时读取最新投影。授权处理沿用工作台，输入下方可展开已提交的处理说明和证据。
 * 状态旁原位暂停/继续，停止收进菜单；复用 Loop control，区分 HTTP 受理与 Live 生效，隔离工作区代际并阻止在途重复控制。
 * 当前 Loop 就绪即连接 Live，历史独立读取；历史加载/失败/重试仅更新记录面板，不重启工作连接。
 * 停止/失败在输入区展示保存提示与重新启用，复用工作区 restart；HTTP 受理后等待新 Loop 的 Live 确认，保留草稿并隔离迟到响应。
 */
(async function () {
  "use strict";
  // 源文案在渲染时取当前语言；模板插值始终保留原文。
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }

  if (new URLSearchParams(location.search).get("surface") !== "panel") return;
  document.body.dataset.surface = "panel";
  const host = document.querySelector("#panel"); host.hidden = false;
  for (const id of ["binding-name", "status", "toggle-work", "restart-work"]) document.querySelector(`#${id}`).removeAttribute("data-i18n");
  document.querySelector("#binding").removeAttribute("data-i18n-title");
  document.querySelector("#toggle-work").removeAttribute("data-i18n-aria-label");
  document.querySelector("#instruction").removeAttribute("data-i18n-placeholder");
  host.inert = true;
  const $ = selector => host.querySelector(selector), bridge = window.focusDesktop?.patrol;
  host.prepend($("#version-picker"),$("#version-detail"));
  const runtime = window.focusDesktop?.runtime() || { apiBase: location.origin, session: new URLSearchParams(location.search).get("session") || "" };
  const api = FocusLoopApi.create(runtime), live = FocusLoopLiveStore.create();
  const connection = FocusLoopLiveConnection.create({ api, store: live });
  const feedback = FocusWorkspacePatrolFeedback.createReader(api);
  const readingView = window.FocusMessageReading;
  const answerCards = new Map();
  const intake = FocusWorkspacePatrolIntake.create({ runtime, api, selectWorkspace: window.focusDesktop?.selectWorkspace,
    recovery: bridge?.enabled ? bridge.recovery : await FocusPatrolBrowserRecovery.open("panel"),
    onSaveError: error => fail(new Error(localize`草稿保存失败：${error.message}`)),
    accessMode: () => FocusAccessMode.readNewSessionDefault(localStorage) });
  host.inert = false;
  const preview = FocusWorkspacePatrolMaterialPreview.create({ host, intake, workspaceId: () => workspace?.workspace_id || null, onError: fail, compact:true });
  let workspace = null, workspaceLoaded = false, generation = 0, loopId = null, unsubscribe = null, reads = new AbortController();
  let resultKey = "", publishedState = "", binding = false, nativeReady = false, shownFailure = "", closeEpoch = 0, commandQueue = Promise.resolve();
  let historyRefresh = FocusWorkspacePatrolFeedback.createInputRefresh();
  let historyKey = "", requestKey = "", progressKey = "", artifactKey = "";
  let historyRead = null, historyReady = false, historyRetryBefore = null, historyRefreshPending = false;
  let panelVisible = bridge?.enabled ? false : !document.hidden, visibilityKnown = false, resultsNeedReset = false;
  let control = null;
  let restart = null;
  let onboarding = false;
  const draftStore = () => intake.store(workspace?.workspace_id || null);
  const references = FocusDesktopPatrolReferences.create({ host, api, runtime, store:draftStore, loopId:() => loopId, onError:fail });
  const artifacts = FocusArtifactDrawer.create({ host, api, getLoopId: () => loopId, onSource: id => openWorkbench(id) });
  function openArtifacts() { references.close(); return artifacts.open(); }
  const historyReferences = FocusPatrolInputReferences.create({ host, intake, workspaceId: () => workspace?.workspace_id,
    onFile: (row, file) => references.detail(file, row.loop_id || loopId), onError: fail });
  function fail(error) { $("#error").hidden = !error; $("#error").textContent = error ? String(error.message || error) : ""; if (!error) $("#retry-workspace").hidden = true; }
  function renderOnboarding() {
    $("#first-goal").hidden = !onboarding;
    $("#first-goal-help").textContent = workspace
      ? localize("项目文件夹已就绪。输入第一个目标，点击发送或按 Ctrl+Enter 开始。")
      : localize("模型已验证可用。接下来选择项目文件夹，用于读取资料和保存工作结果。");
    $("#first-goal-folder").hidden = !!workspace;
    $("#instruction").placeholder = onboarding ? localize("你想在这个项目中完成什么？") : localize("有什么想法？");
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
    const recovery = FocusWorkspacePatrolFeedback.recovery(projection, draftStore().get());
    if (restart?.phase === "accepted" && projection?.loop_id === restart.loopId) {
      if (recovery) restart = null;
      else if (loop?.status) restart.phase = "applied";
    }
    if (restart?.phase === "applied" && recovery) restart = null;
    const restarting = restart && restart.phase !== "applied";
    $("#work-recovery").hidden = !recovery && !restart;
    $("#recovery-message").textContent = restart ? ({ requesting: localize("正在重新启用…"), accepted: localize("重新启用请求已受理，正在确认工作状态…"), applied: localize("工作已重新启用。") }[restart.phase]) : recovery?.text || "";
    const restartButton = $("#restart-work");
    restartButton.hidden = !recovery && !restarting;
    restartButton.textContent = recovery?.label || localize("重新启用");
    restartButton.disabled = !!restarting || current.connection.status !== "live";
    restartButton.setAttribute("aria-busy", String(!!restarting));
    const latest = draftStore().get().rows[0];
    $("#receipt").textContent = latest?.status === "accepted" && recovery ? localize("已保存")
      : ({ accepted: localize("已受理"), sending: localize("发送中"), failed: localize("未确认"), edited: localize("已转为草稿") }[latest?.status] || "");
    const deliveryHtml = FocusWorkspacePatrolFeedback.delivery(projection, workspace);
    $("#delivery").hidden = !deliveryHtml;
    if ($("#delivery").innerHTML !== deliveryHtml) $("#delivery").innerHTML = deliveryHtml;
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
    toggle.textContent = resumable ? localize("继续") : localize("暂停");
    toggle.dataset.command = resumable ? "resume" : "pause";
    toggle.setAttribute("aria-label", resumable ? localize("继续当前工作") : localize("暂停当前工作"));
    toggle.disabled = !!pendingControl || current.connection.status !== "live" || (!pausable && !resumable);
    $("#stop-work").disabled = !!pendingControl || current.connection.status !== "live" || !["running", "paused", "waiting_user"].includes(loop?.status);
    for (const button of [toggle, $("#stop-work")]) {
      if (pendingControl) button.setAttribute("aria-busy", "true");
      else button.removeAttribute("aria-busy");
    }
    $("#control-feedback").hidden = !control;
    $("#control-feedback").textContent = control ? `${{ pause: localize("暂停"), resume: localize("继续"), stop: localize("停止") }[control.command]}${{ requesting: localize("请求中…"), accepted: localize("已受理 · 等待工作状态更新"), applied: localize("已生效") }[control.phase]}` : "";
    $("#status").textContent = workspace ? status.text : localize("准备接收新输入");
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
  async function restartWork() {
    if (!workspace || $("#restart-work").disabled || $("#restart-work").hidden) return;
    const owner = { phase: "requesting", loopId: null }, token = generation, id = workspace.workspace_id;
    restart = owner; fail(null); notify();
    try {
      const loop = await api.restartWorkspacePatrol(id);
      if (token !== generation || restart !== owner) return;
      owner.phase = "accepted"; owner.loopId = loop.loop_id;
      connect(loop.loop_id);
    } catch (error) {
      if (token !== generation || restart !== owner) return;
      restart = null; fail(error);
    } finally { if (token === generation) notify(); }
  }
  function renderInput(model) {
    if ($("#instruction").value !== model.draft.content) $("#instruction").value = model.draft.content;
    preview.render(model.draft);
    references.render(model.draft);
    $("#instruction").style.height = "36px";
    $("#instruction").style.height = `${Math.max(36, Math.min(108, $("#instruction").scrollHeight))}px`;
    $("#reply-target").hidden = !model.draft.request_id;
    $("#reply-target span").textContent = model.draft.request_id ? localize`回复指定问题 · 版本 ${model.draft.request_revision}` : "";
    const status = { accepted: localize("已受理"), sending: localize("发送中"), failed: localize("未确认"), edited: localize("已转为草稿") };
    $("#receipt").textContent = status[model.rows[0]?.status] || "";
    const nextHistory = `${workspace?.workspace_id || ""}:${model.historyRevision}`;
    if (historyKey !== nextHistory) {
      historyKey = nextHistory;
      const items = $("#history-items");
      const restoreResults = FocusWorkspacePatrolFeedback.preserveInputResults(items);
      items.replaceChildren();
      for (const row of model.rows) {
        const card = element("article", "", items);
        card.dataset.submissionId = row.submission_id;
        element("pre", row.content || localize("仅参考材料"), card);
        card.insertAdjacentHTML("beforeend", FocusWorkspacePatrolFeedback.inputResult(row));
        card.insertAdjacentHTML("beforeend", FocusPatrolInputReferences.render(row));
        element("small", status[row.status], card);
        if (row.status === "failed") {
          element("small", row.error, card);
          const id = workspace?.workspace_id;
          action(localize("按原内容重试"), card, () => send(() => intake.retry(id, row.submission_id), id));
          action(localize("编辑为新输入"), card, () => intake.editFailed(id, row.submission_id));
        }
      }
      restoreResults();
      historyReferences.bind(model);
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
  function readHistory(before = null) {
    if (!workspace) return Promise.resolve();
    if (historyRead) return historyRead;
    const token = generation, id = workspace.workspace_id, initialize = !historyReady;
    const refresh = before || initialize ? null : historyRefresh.take();
    const status = $("#history-status"), retry = $("#retry-history");
    status.textContent = localize("正在读取记录…"); status.hidden = false; retry.hidden = true;
    $("#older").disabled = true;
    historyRead = (async () => {
      try {
        const page = before || initialize ? await api.workspaceInputs(id, before)
          : await FocusWorkspacePatrolFeedback.refreshInputs(api, id, draftStore().get(), refresh);
        if (token !== generation) return;
        intake.history(id, page, Boolean(before), initialize);
        if (initialize) await intake.reconcile(id, page);
        if (token !== generation) return;
        historyReady = true;
        status.hidden = true; status.textContent = "";
      } catch (error) {
        if (token !== generation) return;
        if (refresh) historyRefresh.restore(refresh);
        historyRetryBefore = before;
        status.textContent = localize`读取记录失败：${error.message || error}`;
        retry.hidden = false;
      } finally {
        if (token === generation) {
          historyRead = null; $("#older").disabled = false;
          if (historyRefreshPending) { historyRefreshPending = false; void readHistory(); }
        }
      }
    })();
    return historyRead;
  }
  async function activate(bound) {
    if (bound?.workspace_id === workspace?.workspace_id && workspace && workspaceLoaded) return;
    const token = ++generation;
    control = null;
    restart = null;
    loopId = null; resetResults(); connection.stop();
    workspace = bound; workspaceLoaded = false; historyRefresh = FocusWorkspacePatrolFeedback.createInputRefresh();
    historyRead = null; historyReady = false; historyRetryBefore = null; historyRefreshPending = false;
    $("#history-status").hidden = true; $("#history-status").textContent = ""; $("#retry-history").hidden = true;
    references.close();
    artifacts.reset();
    $("#reference-file").disabled = !bound;
    fail(null);
    if (bound) { intake.remember(bound); intake.claimUnbound(bound.workspace_id); }
    $("#binding-name").textContent = bound ? bound.display_name : localize("绑定工作区");
    $("#binding").title = bound?.path || localize("选择工作区文件夹");
    renderOnboarding();
    watchDraft();
    if (!bound) return;
    await Promise.all([
      (async () => {
        try {
          const loop = await api.workspacePatrol(bound.workspace_id);
          if (token !== generation) return;
          workspaceLoaded = true;
          if (loop) connect(loop.loop_id);
        } catch (error) {
          if (token !== generation) return;
          $("#retry-workspace").hidden = false; throw error;
        }
      })(),
      readHistory(),
    ]);
  }
  async function choose() {
    if (binding) return false;
    binding = true; $("#binding").disabled = true;
    try {
      const path = await intake.choose();
      if (!path) return false;
      await activate(await intake.bind(path));
      $("#instruction").focus(); return true;
    } finally { binding = false; $("#binding").disabled = false; }
  }
  async function send(operation, id) {
    fail(null);
    try {
      const promise = operation(); notify();
      const receipt = await promise;
      if (receipt && workspace?.workspace_id === id && onboarding) { onboarding = false; renderOnboarding(); }
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
      element("h2", request.scope?.information_only ? localize("需要补充一点信息") : localize("有一项工作需要处理"), card);
      element("p", request.prompt || localize("打开工作台查看"), card);
      if (request.scope?.information_only) action(localize("回复这个问题"), card, () => {
        draftStore().edit({ request_id: entity.entity_id, request_revision: entity.revision }); $("#instruction").focus();
      }); else action(localize("去工作台处理 ↗"), card, () => openWorkbench());
    }
  }
  function renderAnswer(record, result) {
    const card = record.node; card.replaceChildren(); card.hidden = !result;
    record.refreshReading = null;
    if (!result) return;
    element("h2", result.title, card);
    if (result.error) {
      element("p", result.error, card).setAttribute("role", "status");
      action(localize("重试读取"), card, () => record.retry());
    } else {
      const reading = element("details", "", card); reading.className = "answer-reading";
      const summary = element("summary", "", reading);
      element("span", result.text.replace(/\s+/g, " ").slice(0, 200), summary).className = "answer-preview";
      const label = element("span", localize("展开阅读"), summary); label.className = "answer-toggle";
      const content = element("div", "", reading); content.className = "answer-content";
      let rendered = false;
      function updateReading() {
        if (!panelVisible) return;
        label.textContent = reading.open ? localize("收起阅读") : localize("展开阅读");
        if (reading.open && !rendered) {
          rendered = true;
          content.innerHTML = readingView.render({ role: "assistant", content: result.text }, { copyLabel: localize("复制全文") });
          const body = content.querySelector(".message-rich"); body.classList.add("answer-markdown");
          content.addEventListener("click", event => {
            if (readingView.handleCopy(event, { onStatus: (text, error) => { if (error) fail(new Error(text)); } })) return;
            FocusFileLinks.follow(event, {
              onExternal: window.focusDesktop?.openExternal,
              onError: fail,
              onFile: path => {
                const source = result.file_context;
                if (source?.error) throw new Error(source.error);
                if (!source?.task_id || source.workspace_id !== workspace?.workspace_id)
                  throw new Error(localize("回答缺少可信的文件来源，无法确定执行位置；请从文件 / 产物打开"));
                fail(null);
                return references.link(FocusFileLinks.reference({ ...source, path }), source.task_id);
              },
            });
          });
        }
        syncReading();
      }
      record.refreshReading = updateReading;
      reading.addEventListener("toggle", updateReading);
    }
    const details = element("details", "", card); details.className = "answer-source";
    element("summary", localize("来源与版本"), details);
    element("small", `Context ${result.context_id} · Revision ${result.revision_id}`, details);
    const source = action(localize("查看来源 ↗"), details, () => openWorkbench(result.context_id, result.revision_id));
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
        element("p", localize("正在读取回答…"), record.node).setAttribute("role", "status");
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
    renderRequests(projection);
    const progress = projection.task_progress?.state?.document?.items || [];
    const verified = progress.filter(item => item.state === "completed" && item.support === "supported").length;
    const nextProgress = JSON.stringify([verified, progress.length]);
    if (nextProgress !== progressKey) {
      progressKey = nextProgress;
      const container = $("#progress"); container.replaceChildren();
      if (progress.length) {
        const card = element("article", "", container); element("h2", localize("已提交进度"), card);
        element("p", localize`${verified} / ${progress.length} 项已有验证证据`, card); action(localize("查看进度 ↗"), card, () => openWorkbench());
      }
    }
    const files = FocusLoopLiveSelectors.selectFacts(projection).filter(item => item.kind === "file" || item.kind === "artifact");
    const nextArtifacts = JSON.stringify(files.map(item => [item.fact_id, item.revision]));
    if (nextArtifacts !== artifactKey) {
      artifactKey = nextArtifacts;
      artifacts.update(files);
      const container = $("#artifacts"); container.replaceChildren();
      if (files.length) {
        const card = element("article", "", container); element("h2", localize`${files.length} 项产物更新`, card);
        const latestFiles = FocusArtifactDrawer.groupFiles(files).map(versions => versions[0]);
        const legacyArtifacts = files.filter(fact => !FocusLoopFactsView.fileReference(fact));
        for (const fact of [...latestFiles, ...legacyArtifacts].slice(0, 3)) {
          const file = FocusLoopFactsView.fileReference(fact), row = element("div", "", card); row.className = "artifact-result";
          const name = element("span", file?.path?.split(/[\\/]/).at(-1) || fact.title || localize("产物"), row); name.title = file?.path || fact.title || localize("产物");
          const actions = element("div", "", row); actions.className = "artifact-actions";
          action(localize("打开"), actions, () => file?.path && file.result_key ? references.detail(file) : openWorkbench(fact.context_id, fact.revision_id));
          if (file?.path && file.result_key) action(localize("引用"), actions, () => references.reference(file));
        }
        action(localize("文件 / 产物"), card, openArtifacts);
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
  document.addEventListener("focus:languagechange", () => {
    $("#binding-name").textContent = workspace?.display_name || localize("绑定工作区");
    $("#binding").title = workspace?.path || localize("选择工作区文件夹");
    historyKey = progressKey = artifactKey = requestKey = "";
    renderOnboarding();
    renderInput(draftStore().get());
    cancelResults();
    resultsNeedReset = true;
    refreshVisible();
    notify();
  });
  live.subscribe(({ projection }) => {
    notify();
    refreshVisible();
    if (!projection || projection.loop_id !== loopId) return;
    if (workspace && historyRefresh.observe(projection, draftStore().get())) {
      if (historyRead) historyRefreshPending = true;
      else void readHistory();
    }
  });
  $("#compose").addEventListener("submit", event => { event.preventDefault(); void submit().catch(fail); });
  $("#instruction").addEventListener("input", event => {
    try { draftStore().edit({ content: event.target.value }); fail(null); if (!event.isComposing) references.input(); }
    catch (error) { fail(new Error(localize`草稿保存失败：${error.message}`)); }
  });
  $("#instruction").addEventListener("compositionend", () => references.input());
  $("#instruction").addEventListener("keydown", event => {
    if (event.key === "Enter" && event.ctrlKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); $("#compose").requestSubmit(); }
  });
  $("#binding").addEventListener("click", () => { void enqueue(choose); });
  $("#first-goal-folder").addEventListener("click", () => { void enqueue(choose); });
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
    else if (artifacts.isOpen()) artifacts.close();
    else if (references.isOpen()) references.close(true);
    else { void hidePanel().catch(fail); }
  });
  $("#workbench").addEventListener("click", () => { void openWorkbench(); });
  $("#open-files").addEventListener("click", () => { $(".panel-menu").open = false; void openArtifacts().catch(fail); });
  $("#delivery").addEventListener("click", event => {
    const button = event.target.closest("button");
    if (button?.hasAttribute("data-patrol-files-open")) void openArtifacts().catch(fail);
    else if (button?.dataset.patrolDeliveryContext) void Promise.resolve(openWorkbench(button.dataset.patrolDeliveryContext, button.dataset.revisionId)).catch(fail);
  });
  $("#toggle-work").addEventListener("click", () => { void controlWork($("#toggle-work").dataset.command); });
  $("#stop-work").addEventListener("click", () => { void controlWork("stop"); });
  $("#restart-work").addEventListener("click", () => { void restartWork(); });
  $("#reconnect").addEventListener("click", () => { const id = loopId; loopId = null; connect(id); });
  $("#activity-reconnect").addEventListener("click", () => $("#reconnect").click());
  $("#retry-workspace").addEventListener("click", () => { void enqueue(() => activate(workspace)); });
  $("#retry-history").addEventListener("click", () => { void readHistory(historyRetryBefore); });
  $("#add-material").addEventListener("click", () => $("#file-picker").click());
  $("#file-picker").addEventListener("change", async event => {
    const id = workspace?.workspace_id || null, files = [...event.target.files]; event.target.value = "";
    try { await intake.add(id, await FocusWorkspacePatrolMaterialPreview.readTransfer({ files }, await intake.resourceLimits())); } catch (error) { fail(error); }
  });
  $("#older").addEventListener("click", () => {
    const before = draftStore().get().cursor;
    if (before) void readHistory(before);
  });
  watchDraft();
  const resizePanel = () => {
    const detail = !$("#version-detail").hidden, beside = detail && matchMedia("(min-width:580px)").matches;
    const popupHeight = [$("#version-picker"),$("#version-detail"),$(".artifact-drawer")].reduce((sum,node) => sum + (node.hidden || (beside && node.id === "version-detail") ? 0 : node.offsetHeight + 8),0);
    const shellStyle = getComputedStyle($(".panel-shell"));
    const contentStyle = getComputedStyle($(".panel-content"));
    const composeHeight = $(".panel-header").offsetHeight + $("#compose").offsetHeight + parseFloat(contentStyle.paddingTop) + parseFloat(contentStyle.paddingBottom) + parseFloat(shellStyle.borderTopWidth) + parseFloat(shellStyle.borderBottomWidth);
    const popupRoom = `${Math.max(80,Math.min(500,screen.availHeight)-composeHeight-8)}px`;
    if (host.style.getPropertyValue("--popup-room") !== popupRoom) host.style.setProperty("--popup-room",popupRoom);
    const contentHeight = composeHeight + $(".panel-feedback-content").offsetHeight + popupHeight;
    const menuHeight = $(".panel-menu").open ? $(".panel-menu-content").offsetTop + $(".panel-menu-content").offsetHeight + 10 : 0;
    const gutter = parseFloat(getComputedStyle(host).paddingTop);
    const height = Math.round(Math.max(contentHeight, menuHeight, beside ? $("#version-detail").offsetHeight + 56 : 0)+gutter*2);
    if (bridge?.enabled) void bridge.resizePanel({height,width:(detail || artifacts.isOpen() ? 590 : host.dataset.reading === "true" ? 520 : 288)+gutter*2,animate:!matchMedia("(prefers-reduced-motion:reduce)").matches}).catch(fail);
  };
  const sizeObserver = new ResizeObserver(resizePanel);
  $(".panel-menu").addEventListener("toggle", resizePanel);
  sizeObserver.observe(host);
  sizeObserver.observe($(".panel-content"));
  sizeObserver.observe($("#compose"));
  sizeObserver.observe($(".panel-feedback-content"));
  sizeObserver.observe($(".panel-menu-content"));
  sizeObserver.observe($("#version-picker"));
  sizeObserver.observe($("#version-detail"));
  sizeObserver.observe($(".artifact-drawer"));
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
        if (target.setupWorkspace) { onboarding = true; renderOnboarding(); }
        if (target.workspaceId) await activate(target.workspaceId === workspace?.workspace_id ? workspace : await intake.workspace(target.workspaceId));
        else if (workspace && !workspaceLoaded) await activate(workspace);
        if (target.requestId) draftStore().edit({ request_id: target.requestId, request_revision: target.requestRevision });
        if (target.file_refs?.length) {
          const refs = new Map(draftStore().get().draft.file_refs.map(file => [file.result_key,file]));
          target.file_refs.forEach(file => refs.set(file.result_key,file));
          if (refs.size > 32) throw new Error(localize("一次输入最多引用 32 个文件版本"));
          draftStore().edit({file_refs:[...refs.values()]});
        }
        if (target.setupWorkspace) {
          renderOnboarding();
          if (!workspace) await choose();
          else $("#instruction").focus();
        }
      });
    });
    bridge.onContent(payload => enqueue(async () => {
      const id = payload.workspaceId || null;
      await intake.add(id, payload.items);
      if (id !== (workspace?.workspace_id || null)) fail(new Error(localize("材料已保留在拖入时工作区的草稿中，切换回该工作区即可查看")));
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
  const flush = () => { void intake.flush().catch(fail); };
  document.addEventListener("visibilitychange", () => { if (document.hidden) flush(); });
  window.addEventListener("pagehide", () => { try { intake.checkpoint(); } catch (error) { fail(error); } flush(); });
  window.addEventListener("beforeunload", () => { sizeObserver.disconnect(); historyReferences.dispose(); references.dispose(); preview.dispose(); connection.stop(); reads.abort(); });
})().catch(error => { const node = document.querySelector("#error"); node.hidden = false; node.textContent = error.message; });
