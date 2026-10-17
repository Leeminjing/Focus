/* 本文件对外提供工作区 Patrol 页面控制器。
 * 输入为已绑定工作区、共享 Loop API、Live Store/Connection 及输入 store；输出为逐条耐久回执和图主导工作台更新；选择身份由原 ContextInspector 持有。
 * 当前 Loop 与历史独立读取，Loop 就绪即订阅单路 Live，历史错误仅留在记录面板；依据投影结构共享只更新变化区域，保留 composer、进度和未变等待表单 DOM，异步响应按工作区代际隔离；
 * 图谱 Live 变化在同一动画帧合并，使用最新投影检查拓扑与更新节点；离开时取消待执行帧。
 * 主输入走 workspace intake，固定当时可见的 Live 游标、Fact revision 与独立 Run 有界片段；旧请求重试复用原文和证据。Observation 按精确版本检视，迟到分页不得进入另一版本。
 * 显隐复用 SurfaceTransition 保留输入节点，运行预览复用共享 Run 观察器，折叠/离开释放订阅；原生 dialog 管理焦点，AbortController 清理监听与读请求。示例：await controller.mount(host, workspace)。
 * 文件详情/候选响应绑定工作区与请求代际；实时文件事实保留历史滚动位置，详情与输入节点独立。
 * 输入记录复用非模态显隐和游标查询，开关只改变面板；草稿更新保留记录 DOM，内容更新恢复可见记录偏移，滚动续页仅允许一个在途请求。
 * 弹窗类型由现有 dialogKind 投影为 data-patrol-dialog-kind，供局部材质区分；每次打开同步，关闭动画期间保留，不作为业务状态来源。
 * intake 统一绑定/上传/投递，浏览器拖放先预览；onCompose 存在时原生工作台转交输入入口、精确回复目标及固定文件版本，不持有第二份可编辑草稿。
 */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusWorkspacePatrolController = api;
})(globalThis, function (root) {
  "use strict";
  function create({ api, inputs, intake, onCompose, liveStore, connection, onOpenTask, createRunPreviews, view = root.FocusWorkspacePatrolView }) {
    let host = null;
    let workspace = null;
    let loopId = null;
    let token = 0;
    let lineageKey = null;
    let lineageSnapshot = null;
    let lineageToken = 0;
    let historyKey = null;
    let historyToken = 0;
    let historyOpen = false;
    let historyReady = false;
    let historyLoading = false;
    let historyRefreshPending = false;
    let historyFailure = null;
    let historyFailureBefore = null;
    let historyHTML = "";
    let unlisten = null;
    let materialPreview = null;
    let historyReferences = null;
    let budget = null;
    let artifacts = null;


    let lifetime = null;
    let inspector = null;
    let detailsOpen = false;
    let detailsScroll = 0;
    let relatedOnly = false;
    let highlightIds = [];
    let projection = null;
    let graphFrame = null;
    let lineagePending = false;
    let observation = null;
    let observationIdentity = null;
    let observationVersion = 0;
    let observationSections = {};
    let observationInspection = null;
    let observationLoading = new Set();
    let inspectionVersion = 0;
    let dialogKind = null;
    let movedPanel = null;
    let attentionTarget = null;



    let factPage = null;
    let visibleFact = null;
    let factOptions = { kind: "", status: "", outcomeStatus: "", contextId: "" };
    let factVersion = 0;
    let fileVersion = 0;
    let fileCandidates = [];
    let progressInspection = null;
    let progressVersion = 0;
    let progressFilter = "all";
    let grantInspection = null;
    const waitDrafts = root.FocusLoopWaitRequestView.createDraftStore();
    const waitUi = new Map();
    const previews = createRunPreviews?.((values, structural) => {
      if (!host || !detailsOpen) return;
      if (structural) scheduleGraphPatch();
      else root.FocusPortfolioMapView.patchPreviews(host.querySelector("[data-patrol-lineage]"), values);
      patchRetry();
    });
    function patchRetry() {
      if (host) host.querySelector("[data-patrol-live-retry]").hidden = liveStore.get().connection.status !== "unavailable" && !Object.values(previews?.get() || {}).some(item => item.status === "unavailable");
    }
    const error = value => {
      if (!host) return;
      const node = host.querySelector("[data-patrol-error]");
      node.textContent = value?.message || String(value || "");
      node.hidden = !node.textContent;
    };
    function restorePanel() {
      if (movedPanel && host) { movedPanel.hidden = true; host.querySelector(".workspace-patrol").append(movedPanel); }
      movedPanel = null;
      dialogKind = null;
    }
    function showDialog(kind, title, html = "", panel = null) {
      restorePanel();
      dialogKind = kind;
      const dialog = host.querySelector("[data-patrol-dialog]");
      dialog.dataset.patrolDialogKind = kind;
      dialog.querySelector("h2").textContent = title;
      const body = dialog.querySelector("[data-patrol-dialog-body]");
      body.innerHTML = html;
      if (panel) { movedPanel = panel; panel.hidden = false; body.append(panel); }
      if (!dialog.open || dialog.inert) void root.FocusSurfaceTransition.visible(dialog, true, { modal: true, source: root.document.activeElement });
    }
    function closeDialog() {
      attentionTarget = null;
      dialogKind = null; progressVersion++; factVersion++; inspectionVersion++; observationInspection = null;
      return root.FocusSurfaceTransition.visible(host.querySelector("[data-patrol-dialog]"), false, { modal: true });
    }
    function toggleDetails() {
      setHistoryOpen(false);
      detailsOpen = !detailsOpen;
      const section = host.querySelector(".workspace-patrol"), details = host.querySelector("[data-patrol-details-content]");
      const composer = host.querySelector("[data-patrol-composer]");
      if (!detailsOpen) detailsScroll = host.scrollTop;
      void root.FocusSurfaceTransition.layout(composer, () => {
        if (!detailsOpen) details.dataset.surfaceClosing = "";
        else delete details.dataset.surfaceClosing;
        section.classList.toggle("is-quiet", !detailsOpen);
        root.document.body.classList.toggle("has-patrol-workbench", detailsOpen);
        void root.FocusSurfaceTransition.visible(details, detailsOpen);
        if (detailsOpen) host.scrollTop = detailsScroll;
      });
      for (const button of host.querySelectorAll("[data-patrol-details]")) button.setAttribute("aria-expanded", String(detailsOpen));
      composer.querySelector("[data-patrol-details]").textContent = detailsOpen ? "收起详情 ↙" : "查看工作详情 ↗";
      if (!detailsOpen) { previews?.stop(); closeInspection(); }
      else if (projection) { patchGraph(); void refreshLineage(projection); void refreshObservation(); }
    }
    function closeInspection() { inspector?.close(); }
    async function openFilePicker() {
      if (!loopId) return;
      const owner = token, version = ++fileVersion;
      const picker = host.querySelector("[data-patrol-file-picker]");
      picker.hidden = false;
      picker.textContent = "正在读取文件版本…";
      const page = await api.facts(loopId, { kind: "artifact", limit: 100, signal: lifetime.signal });
      if (owner !== token || version !== fileVersion) return;
      fileCandidates = page.facts || [];
      renderFilePicker();
    }
    function renderFilePicker() {
      const query = inputs.get().draft.content.match(/(?:^|\s)@([^@\n]*)$/)?.[1]?.toLowerCase() || "";
      const candidates = fileCandidates.map(root.FocusLoopFactsView.fileReference).filter(file => file && file.path.toLowerCase().includes(query));
      host.querySelector("[data-patrol-file-picker]").innerHTML = candidates.map(file => `<button type="button" data-patrol-file-add="${view.escape(JSON.stringify(root.FocusLoopFactsView.inputFileReference(file)))}">${view.escape(file.path)} · ${view.escape((file.sha256 || file.result_key).slice(0, 8))}</button>`).join("") || "没有可引用的文件版本";
    }
    function addFileReference(file) {
      const draft = inputs.get().draft;
      const refs = [...(draft.file_refs || []).filter(item => item.result_key !== file.result_key), file];
      if (refs.length > 32) throw new Error("一次输入最多引用 32 个文件版本");
      const pickerOpen = !host.querySelector("[data-patrol-file-picker]").hidden;
      inputs.edit({ file_refs: refs, content: pickerOpen ? draft.content.replace(/(^|\s)@[^@\n]*$/, "$1") : draft.content });
      host.querySelector("[data-patrol-file-picker]").hidden = true;
      host.querySelector("[data-patrol-content]").focus({ preventScroll: true });
    }
    const manifest = () => lineageSnapshot ? view.lineageManifest(lineageSnapshot, projection) : { nodes: [], edges: [] };
    function showPanel(name) {
      host.querySelector(".workspace-patrol").dataset.workbenchPanel = name;
      for (const button of host.querySelectorAll("[data-workbench-show]")) button.setAttribute("aria-pressed", String(button.dataset.workbenchShow === name));
    }
    function patchGraph() {
      if (!host || !detailsOpen || !lineageSnapshot) return;
      const graph = host.querySelector("[data-patrol-lineage]"), selected = inspector?.selection();
      const options = { presentation: "workbench", selectedRevisionId: selected?.revisionId, relatedOnly, highlightIds };
      const raw = manifest(), activity = root.FocusLoopLiveSelectors?.selectGraphActivity(projection);
      previews?.sync({ workspaceId: workspace.workspace_id, loopId, nodes: raw.nodes });
      const data = previews?.decorate(raw) || raw;
      const existing = graph.querySelector(".portfolio-map");
      if (!root.FocusPortfolioMapView.reconcile(graph, data, selected?.contextId, activity, options)) graph.innerHTML = root.FocusPortfolioMapView.render(data, selected?.contextId, activity, options);
      root.FocusPortfolioMapView.bind(graph);
      if (previews) root.FocusPortfolioMapView.patchPreviews(graph, previews.get());
      if (!existing) requestAnimationFrame(() => { if (graph.isConnected) graph.querySelector('[data-portfolio-zoom="fit"]')?.click(); });
      const button = host.querySelector("[data-patrol-related]");
      button.disabled = !selected;
      button.setAttribute("aria-pressed", String(relatedOnly));
      button.textContent = relatedOnly ? "恢复全部关系" : "仅看所选关联";
      for (const item of host.querySelectorAll("[data-patrol-progress-locate]")) item.setAttribute("aria-pressed", String(JSON.parse(item.dataset.patrolProgressLocate).includes(selected?.contextId)));
    }
    function scheduleGraphPatch(refreshTopology = false) {
      if (!host || !detailsOpen) return;
      lineagePending ||= refreshTopology;
      if (graphFrame !== null) return;
      const owner = token;
      graphFrame = requestAnimationFrame(() => {
        if (owner !== token) return;
        graphFrame = null;
        const refresh = lineagePending;
        lineagePending = false;
        if (!host || !detailsOpen) return;
        if (refresh && projection) void refreshLineage(projection);
        patchGraph();
      });
    }
    function selectionChanged(selected) {
      if (!selected) relatedOnly = false;
      patchGraph();
    }
    async function inspectContext(contextId, trigger, revisionId) {
      const node = manifest().nodes.find(node => node.context_id === contextId) || projection?.contexts?.[contextId]?.state || {};
      if (root.innerWidth < 1200) showPanel("context");
      await inspector.select(loopId, contextId, node, trigger, revisionId || node.current_revision_id);
    }
    async function locateProgress(ids, trigger) {
      highlightIds = ids;
      relatedOnly = false;
      const notice = host.querySelector("[data-workbench-location]");
      notice.hidden = false;
      const available = ids.filter(id => manifest().nodes.some(node => node.context_id === id));
      notice.textContent = !ids.length ? "此条目尚未关联工作线" : !available.length ? "关联工作线尚未发布到当前关系图" : `${available.length} 条关联工作线已突出显示，请选择查看`;
      showPanel("graph"); patchGraph();
      if (available.length === 1) await inspectContext(available[0], trigger);
      host.querySelector(`[data-patrol-lineage] [data-context-id="${root.CSS.escape(available[0] || "")}"]`)?.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
    function inspectEdges(identity) {
      showDialog("relationship", "已提交来源关系", root.FocusPortfolioMapView.relationship(manifest(), identity));
    }
    async function refreshObservation(force = false) {
      const id = projection?.round?.state?.observation_id;
      if (!id || (!force && id === observationIdentity)) return;
      observationIdentity = id;
      observation = null;
      const owner = token, version = ++observationVersion;
      host.querySelector("[data-patrol-observation]").textContent = "正在读取本轮冻结输入";
      host.querySelector("[data-patrol-observation-open]").disabled = true;
      try {
        const value = await api.observation(loopId, id, { signal: lifetime.signal });
        if (token !== owner || version !== observationVersion || observationIdentity !== id) return;
        observation = value;
        host.querySelector("[data-patrol-observation]").innerHTML = root.FocusObservationView.card(value, { compact: true });
        host.querySelector("[data-patrol-observation-open]").disabled = false;
      } catch (failure) {
        if (owner === token && version === observationVersion && failure.name !== "AbortError") {
          observationIdentity = null;
          host.querySelector("[data-patrol-observation]").innerHTML = `读取失败：${view.escape(failure.message)} <button data-patrol-observation-retry>重试</button>`;
        }
      }
    }
    async function loadObservationSection(section) {
      const selected = observationInspection, owner = token, loading = observationLoading;
      if (!selected || loading.has(section)) return;
      loading.add(section);
      const old = observationSections[section];
      try {
        const page = await api.observation(loopId, selected.observation_id, { section, cursor: old?.next_cursor, signal: lifetime.signal });
        if (owner !== token || selected !== observationInspection) return;
        observationSections[section] = { ...page, items: [...(old?.items || []), ...page.items] };
        if (dialogKind === "observation") showDialog("observation", "Observation", root.FocusObservationView.render(selected, observationSections));
      } finally { loading.delete(section); }
    }
    async function openObservation(id) {
      const owner = token, version = ++inspectionVersion;
      observationInspection = null; observationSections = {}; observationLoading = new Set();
      showDialog("observation", "Observation", "正在读取所选冻结版本");
      const value = await api.observation(loopId, id, { signal: lifetime.signal });
      if (owner !== token || version !== inspectionVersion || dialogKind !== "observation") return;
      observationInspection = value;
      showDialog("observation", "Observation", root.FocusObservationView.render(value, observationSections));
    }
    function visibleContext() {
      if (!projection || projection.loop_id !== loopId) return null;
      const shown = selector => detailsOpen && Boolean(host.querySelector(selector)?.getClientRects().length);
      const facts = [...(shown("[data-patrol-facts]") ? root.FocusLoopLiveSelectors.selectFacts(projection) : []),
        ...(dialogKind === "facts" ? factPage?.facts || [] : []), ...(dialogKind === "fact" && visibleFact ? [visibleFact] : [])];
      return root.FocusWorkspacePatrolInputs.captureVisibleContext({ projection, facts,
        previews: shown("[data-patrol-lineage]") ? previews?.get() || {} : {} });
    }
    function renderProgressInspection() {
      const page = progressInspection;
      if (!page || dialogKind !== "progress") return;
      const selected = page.selected || page.current;
      const versions = [...(page.history || [])];
      if (selected && !versions.some(item => item.progress_id === selected.progress_id)) versions.unshift(selected);
      const toolbar = `<label>查看已提交版本<select data-patrol-progress-version>${versions.map(item => `<option value="${view.escape(item.progress_id)}"${item.progress_id === selected?.progress_id ? " selected" : ""}>P${item.generation}${item.progress_id === page.current?.progress_id ? " · 当前" : ""}</option>`).join("")}</select></label>${page.history_has_more ? "<p>列表为最近 32 个版本；Observation 可读取本轮精确前序。</p>" : ""}<p>版本 ${view.escape(selected?.progress_id || "尚未提交")}，不会随实时事件改写此份检查。</p>`;
      showDialog("progress", "Task Progress · 已提交版本", toolbar + view.progress(selected, true, progressFilter) + (page.work || []).map(work => `<p>进度沉淀：${view.escape(work.state)}${work.failure_kind ? ` · ${view.escape(work.failure_kind)}` : ""}${work.state === "blocked" ? ` <button data-patrol-progress-retry="${view.escape(work.work_id || work.observation_id)}">重试这份冻结进度</button>` : ""}</p>`).join(""));
    }
    async function openProgress(progressId = null) {
      const owner = token, version = ++progressVersion;
      showDialog("progress", "Task Progress", '正在读取已提交版本 <button data-patrol-progress-open>重试读取</button>');
      const page = await api.taskProgress(loopId, progressId, lifetime.signal);
      if (owner !== token || version !== progressVersion || dialogKind !== "progress") return;
      progressInspection = page;
      renderProgressInspection();
    }
    async function openGrant() {
      const owner = token;
      showDialog("grant", "工作区授权与预算", '正在读取当前授权 <button data-patrol-grant-open>重试</button>');
      const value = await api.get(loopId);
      if (owner !== token || dialogKind !== "grant") return;
      grantInspection = value;
      showDialog("grant", "工作区授权与预算", root.FocusLoopView.grantControls(value) || "当前没有可修改的有效授权。历史工作仍可检查。");
    }
    function patchRequests() {
      if (!host || !projection) return;
      const requests = Object.values(projection.wait_requests).map(item => ({ request_id: item.entity_id, revision: item.revision, ...item.state }));
      const container = host.querySelector("[data-patrol-requests]"), template = root.document.createElement("template");
      template.innerHTML = view.requests(requests, id => ({ ...waitUi.get(id), draft: waitDrafts.get(id) }));
      const key = card => card.dataset.waitRequestId || card.dataset.requestId;
      const existing = new Map([...container.children].map(card => [key(card), card]));
      let previous = null;
      for (const card of [...template.content.children]) {
        const id = key(card), request = requests.find(item => item.request_id === id), ui = waitUi.get(id);
        const signature = JSON.stringify([request?.revision, ui?.pending, ui?.error]);
        const prior = existing.get(id);
        existing.delete(id);
        const placed = prior?._focusRequestPresentation === signature ? prior : card;
        placed._focusRequestPresentation = signature;
        if (prior && placed !== prior) prior.replaceWith(placed);
        const reference = previous ? previous.nextSibling : container.firstChild;
        if (placed !== reference) container.insertBefore(placed, reference);
        previous = placed;
      }
      existing.forEach(card => card.remove());
    }
    function locateAttention() {
      if (!attentionTarget || !projection || projection.loop_id !== loopId || dialogKind !== "requests") return;
      const target = attentionTarget;
      attentionTarget = null;
      const card = [...host.querySelectorAll("[data-wait-request-id], [data-request-id]")]
        .find(node => (node.dataset.waitRequestId || node.dataset.requestId) === target);
      card?.scrollIntoView?.({ block: "center" });
      card?.querySelector("input, textarea, select, button")?.focus({ preventScroll: true });
    }
    async function respondWait(request, answer) {
      if (waitUi.get(request.entity_id)?.pending) return;
      const owner = token, id = request.entity_id;
      waitUi.set(id, { pending: true });
      patchRequests();
      try {
        await api.resolveWait(loopId, id, { request_revision: request.revision, idempotency_key: crypto.randomUUID(), answer });
        waitDrafts.delete(id);
      } catch (failure) {
        waitUi.set(id, { error: failure.message });
        if (owner === token) patchRequests();
        return;
      }
      if (owner === token) patchRequests();
    }
    async function loadFacts(older = false) {
      const owner = token, version = ++factVersion;
      if (dialogKind !== "facts") showDialog("facts", "LoopFact", '正在读取事实 <button data-patrol-facts-open>重试读取</button>');
      const page = await api.facts(loopId, { ...factOptions, before: older ? factPage?.next_before : undefined, signal: lifetime.signal });
      if (owner !== token || version !== factVersion || dialogKind !== "facts") return;
      const merged = older ? [...(page.facts || []), ...(factPage?.facts || [])] : page.facts || [];
      factPage = { ...page, facts: [...new Map(merged.map(item => [item.fact_id, item])).values()] };
      showDialog("facts", "LoopFact", `<div class="patrol-fact-filters"><label>类型<select data-patrol-fact-filter="kind">${["", "run", "test", "context_revision", "artifact", "workspace", "directive"].map(v => `<option value="${v}"${factOptions.kind === v ? " selected" : ""}>${v || "全部"}</option>`).join("")}</select></label><label>事实状态<select data-patrol-fact-filter="status">${root.FocusLoopFactsView.statusOptions.map(([value, label]) => `<option value="${value}"${factOptions.status === value ? " selected" : ""}>${label}</option>`).join("")}</select></label><label>业务结果<select data-patrol-fact-filter="outcomeStatus">${root.FocusLoopFactsView.outcomeOptions.map(([value, label]) => `<option value="${value}"${factOptions.outcomeStatus === value ? " selected" : ""}>${label}</option>`).join("")}</select></label><label>Context<input data-patrol-fact-filter="contextId" value="${view.escape(factOptions.contextId)}" placeholder="精确 Context 身份"></label></div><div class="patrol-facts"><table><tbody>${root.FocusLoopFactsView.renderRows(factPage.facts)}</tbody></table></div><p>${factPage.has_more ? "后续还有事实" : "已到当前查询范围末尾"}</p>${factPage.has_more ? '<button data-patrol-facts-older>加载更早事实</button>' : ""}`);
    }
    function patchHistory(model) {
      const panel = host.querySelector("[data-patrol-input-history]"), list = host.querySelector("[data-patrol-history]");
      const html = view.history(model);
      if (html !== historyHTML) {
        const restoreResults = root.FocusWorkspacePatrolFeedback.preserveInputResults(list);
        const top = panel.scrollTop, edge = panel.getBoundingClientRect().top;
        const visible = top > 0 ? [...list.children].find(row => row.getBoundingClientRect().bottom > edge) : null;
        const offset = visible ? visible.getBoundingClientRect().top - edge : 0;
        const id = visible?.dataset.submissionId;
        list.innerHTML = html; historyHTML = html;
        restoreResults();
        const retained = id && [...list.children].find(row => row.dataset.submissionId === id);
        panel.scrollTop = retained ? panel.scrollTop + retained.getBoundingClientRect().top - edge - offset : top;
      }
      historyReferences?.bind(model);
      const occupied = Boolean(model.draft.content || model.draft.file_refs?.length || model.draft.attachments?.length);
      for (const button of list.querySelectorAll("[data-patrol-edit-failed]")) {
        button.disabled = occupied;
        button.title = occupied ? "先处理当前草稿" : "";
      }
      patchHistoryStatus();
      void loadOlderHistory();
    }
    function patchHistoryStatus() {
      const status = host.querySelector("[data-patrol-history-status]");
      status.textContent = historyFailure ? `读取记录失败：${historyFailure.message || historyFailure}` : historyLoading ? "正在读取记录…" : "";
      status.hidden = !status.textContent;
      host.querySelector("[data-patrol-history-retry]").hidden = !historyFailure;
    }
    function setHistoryOpen(show) {
      if (!host || historyOpen === show) return;
      historyOpen = show;
      const panel = host.querySelector("[data-patrol-input-history]"), button = host.querySelector("[data-patrol-history-open]");
      button.setAttribute("aria-expanded", String(show));
      if (!show && panel.contains(root.document.activeElement)) button.focus({ preventScroll: true });
      const owner = token;
      void root.FocusSurfaceTransition.visible(panel, show).then(() => {
        if (owner !== token || !show || !historyOpen) return;
        panel.scrollTop = 0;
        void loadOlderHistory();
      });
    }
    function finishHistoryLoading() {
      historyLoading = false; patchHistoryStatus();
      if (historyRefreshPending) { historyRefreshPending = false; void loadLatestHistory(); }
      else void loadOlderHistory();
    }
    async function loadLatestHistory() {
      if (historyLoading) { historyRefreshPending = true; return; }
      const current = token, version = ++historyToken, initialize = !historyReady;
      historyLoading = true; historyFailure = null; patchHistoryStatus();
      try {
        const page = initialize ? await api.workspaceInputs(workspace.workspace_id)
          : await root.FocusWorkspacePatrolFeedback.refreshInputs(api, workspace.workspace_id, inputs.get());
        if (current !== token || version !== historyToken) return;
        intake.history(workspace.workspace_id, page, false, initialize);
        if (initialize) await intake.reconcile(workspace.workspace_id, page);
        if (current !== token) return;
        historyReady = true;
      } catch (failure) {
        if (current === token && version === historyToken) { historyFailure = failure; historyFailureBefore = null; }
      } finally { if (current === token) finishHistoryLoading(); }
    }
    async function loadOlderHistory(retry = false) {
      if (!host || !historyOpen || historyLoading || (historyFailure && !retry)) return;
      const panel = host.querySelector("[data-patrol-input-history]"), cursor = inputs.get().cursor;
      if (retry && historyFailure && !historyFailureBefore) return loadLatestHistory();
      if (historyReady && (!cursor || (!retry && panel.scrollHeight - panel.scrollTop - panel.clientHeight > 64))) return;
      const owner = token, initialize = !historyReady;
      const before = initialize ? undefined : cursor;
      historyLoading = true; historyFailure = null; patchHistoryStatus();
      try {
        const page = await api.workspaceInputs(workspace.workspace_id, before);
        if (owner !== token) return;
        historyReady = true;
        intake.history(workspace.workspace_id, page, Boolean(before), initialize);
      } catch (failure) { if (owner === token) { historyFailure = failure; historyFailureBefore = before; } }
      finally {
        if (owner === token) {
          finishHistoryLoading();
        }
      }
    }
    const patchInputs = model => {
      if (!host) return;
      budget?.syncDraft();
      patchHistory(model);
      materialPreview?.render(model.draft);
      const content = host.querySelector("[data-patrol-content]");
      if (content.value !== model.draft.content) content.value = model.draft.content;
      host.querySelector("[data-patrol-file-refs]").innerHTML = (model.draft.file_refs || []).map(file => `<span>${view.escape(file.path)} · ${view.escape((file.sha256 || file.result_key).slice(0, 8))}<button type="button" data-patrol-file-remove="${view.escape(file.result_key)}" aria-label="移除文件引用">×</button></span>`).join("");
      host.querySelector("[data-patrol-type]").value = model.draft.input_type;
      host.querySelector("[data-patrol-target]").textContent = model.draft.request_id ? `回答请求 ${model.draft.request_id} · 版本 ${model.draft.request_revision ?? "未知"}` : "";
      host.querySelector("[data-patrol-clear-target]").hidden = !model.draft.request_id;
      const latest = model.rows[0];
      host.querySelector("[data-patrol-receipt]").textContent = latest ? ({ accepted: "已受理 · 变化以提交进度为准", sending: "发送中", failed: "发送失败 · 可在输入记录重试" }[latest.status] || "") : "";
      patchFileFacts();
    };
    function patchFileFacts(announce = false) {
      if (!host || !projection) return;
      const stream = host.querySelector("[data-patrol-facts]"), top = stream.scrollTop;
      const atEnd = stream.scrollHeight - top - stream.clientHeight < 24;
      const model = inputs.get(), refs = [...model.draft.file_refs, ...model.rows.filter(row => row.status === "accepted").flatMap(row => row.file_refs || [])];
      const facts = root.FocusLoopFactsView.markFileReferences(root.FocusLoopLiveSelectors.selectFacts(projection), refs);
      root.FocusLoopFactsView.reconcileTimeline(stream, facts);
      stream.scrollTop = announce && atEnd ? stream.scrollHeight : top;
      if (announce) host.querySelector("[data-patrol-new-facts]").hidden = atEnd;
    }
    async function refreshLineage(projection) {
      const revisions = Object.values(projection.contexts).filter(item => item.state.current_revision_id).map(item => [item.entity_id, item.state.current_revision_id]).sort((a, b) => a[0].localeCompare(b[0]));
      const key = JSON.stringify([projection.loop?.state?.current_portfolio_revision_id, projection.portfolio, projection.lineage, revisions]);
      if (key === lineageKey) return;
      lineageKey = key;
      const version = ++lineageToken;
      const current = token;
      try {
        const snapshot = await api.committedLineage(projection.loop_id, lifetime.signal);
        if (current === token && version === lineageToken && host) {
          if (snapshot.complete !== true) throw new Error("来源关系尚未完整返回");
          lineageSnapshot = snapshot;
          host.querySelector("[data-patrol-lineage-error]").hidden = true;
          scheduleGraphPatch(); inspector?.refresh();
        }
      } catch (failure) {
        if (current === token && version === lineageToken && failure.name !== "AbortError") {
          lineageKey = null;
          const notice = host.querySelector("[data-patrol-lineage-error]");
          notice.hidden = false;
          notice.innerHTML = `${lineageSnapshot ? "关系图未更新，保留上次完整结果" : "关系图读取失败"}：${view.escape(failure.message)} <button data-patrol-lineage-retry>重试关系图</button>`;
          if (!lineageSnapshot) host.querySelector("[data-patrol-lineage]").textContent = "尚未取得已提交关系";
        }
      }
    }
    function patchLive({ projection: next, connection: status }) {
      if (!host) return;
      patchRetry();
      if (!next) {
        host.querySelector("[data-patrol-state]").textContent = status?.status === "unavailable" ? "连接不可用 · 正在保留输入" : "正在连接工作状态";
        return;
      }
      if (next.loop_id !== loopId) return;
      const previous = projection;
      projection = next;
      budget?.update(next);
      for (const [id] of waitUi) {
        if (!projection.wait_requests[id] || !["open", "resolving"].includes(projection.wait_requests[id].state.status)) waitUi.delete(id);
      }
      const loop = projection.loop?.state || {};
      const states = { running: "推进中", paused: "已暂停", stopped: "已停止", failed: "执行失败", waiting_user: "等待处理", completed: "已完成当前工作" };
      const connections = { live: "实时更新", syncing: "同步中", connecting: "连接中", reconnecting: "重连中", resyncing: "重新同步", unavailable: "连接不可用" };
      const stateText = loop.status === "waiting_user" && loop.waiting_reason === "awaiting_input" ? "等待新输入" : `${states[loop.status] || "准备中"} · ${connections[status.status] || "同步中"}`;
      host.querySelector("[data-workbench-status]").textContent = stateText;
      host.querySelector("[data-workbench-round]").textContent = projection.round?.state?.number ? `当前 Round ${projection.round.state.number} · Live 事实` : "当前 Live 事实";
      if (["rounds", "runs", "patrol_sessions", "loop", "directives"].some(key => previous?.[key] !== projection[key])) {
        host.querySelector("[data-patrol-rounds]").innerHTML = root.FocusObservationView.rounds(projection);
      }
      const stateNode = host.querySelector("[data-patrol-state]");
      if (stateNode.textContent !== stateText) stateNode.textContent = stateText;
      host.querySelector('[data-patrol-control="pause"]').disabled = !(loop.status === "running" || (loop.status === "waiting_user" && loop.waiting_reason === "awaiting_input"));
      host.querySelector('[data-patrol-control="resume"]').disabled = loop.status !== "paused";
      host.querySelector('[data-patrol-control="stop"]').disabled = !["running", "paused", "waiting_user"].includes(loop.status);
      host.querySelector("[data-patrol-restart]").hidden = !["completed", "stopped", "failed"].includes(loop.status);
      if (previous?.task_progress !== projection.task_progress) {
        const progressNode = host.querySelector("[data-patrol-progress]"), progressHtml = view.progress(projection.task_progress);
        if (progressNode.innerHTML !== progressHtml) progressNode.innerHTML = progressHtml;
      }
      if (previous?.wait_requests !== projection.wait_requests) patchRequests();
      locateAttention();
      const pending = Object.values(projection.wait_requests).filter(item => item.state.status === "open").length;
      const pendingButton = host.querySelector("[data-patrol-pending]");
      pendingButton.hidden = !pending;
      const pendingText = `${pending} 项待处理`;
      if (pendingButton.textContent !== pendingText) pendingButton.textContent = pendingText;
      if (previous?.facts !== projection.facts) {
        patchFileFacts(true);
        artifacts?.update(root.FocusLoopLiveSelectors.selectFacts(projection));
      }
      if (detailsOpen) {
        const topologyChanged = ["loop", "portfolio", "lineage", "contexts"].some(key => previous?.[key] !== projection[key]);
        if (topologyChanged) scheduleGraphPatch(true);
        if (previous?.round !== projection.round) void refreshObservation(true);
        if (lineageSnapshot && ["contexts", "runs", "task_progress"].some(key => previous?.[key] !== projection[key])) scheduleGraphPatch();
      }
      if (!projection.round?.state?.observation_id) {
        observation = null; observationIdentity = null; observationVersion++;
        const node = host.querySelector("[data-patrol-observation]");
        if (node.textContent !== "本轮尚未冻结") node.textContent = "本轮尚未冻结";
        host.querySelector("[data-patrol-observation-open]").disabled = true;
      }
      const key = root.FocusWorkspacePatrolFeedback.inputSignature(projection);
      if (historyKey === null) historyKey = key;
      if (key !== historyKey) {
        historyKey = key;
        void loadLatestHistory();
      }
    }
    function connect(identity) {
      if (loopId !== identity) previews?.stop();
      loopId = identity;
      void connection.start(identity).catch(failure => { if (loopId === identity) error(failure); });
    }
    async function send(row) {
      const bound = workspace.workspace_id;
      const current = token;
      try {
        const receipt = await intake.send(bound, row);
        if (token === current && receipt.loop_id !== loopId) {
          const binding = await api.workspacePatrol(bound);
          if (token === current) connect(binding?.loop_id || receipt.loop_id);
        }
      } catch (failure) { if (token === current) error(failure); }
    }
    async function click(event) {
      const edge = event.target.closest("[data-edge-hit]");
      if (edge) { event.stopPropagation(); inspectEdges(edge.dataset.edgeHit); return; }
      const button = event.target.closest("button");
      if (!button || !host.contains(button)) return;
      if (["show-patrol", "show-map"].includes(button.dataset.action)) return;
      event.stopPropagation();
      button.focus({ preventScroll: true });
      const clickOwner = token;
      try {
        if (onCompose && (button.dataset.patrolAnswer || button.dataset.patrolRetry || button.dataset.patrolEditFailed || button.dataset.patrolFileAdd)) {
          await onCompose({ workspaceId: workspace.workspace_id, ...(button.dataset.patrolAnswer ? { requestId: button.dataset.patrolAnswer, requestRevision: Number(button.dataset.requestRevision) } : {}), ...(button.dataset.patrolFileAdd ? {file_refs:[JSON.parse(button.dataset.patrolFileAdd)]} : {}) });
          return;
        }
        if (button.hasAttribute("data-patrol-files-open")) await artifacts.open();
        else if (button.hasAttribute("data-patrol-file-picker-open")) await openFilePicker();
        else if (button.dataset.patrolFileAdd) addFileReference(JSON.parse(button.dataset.patrolFileAdd));
        else if (button.dataset.patrolFileRemove) inputs.edit({ file_refs: inputs.get().draft.file_refs.filter(item => item.result_key !== button.dataset.patrolFileRemove) });
        else if (button.dataset.patrolEditFailed) {
          intake.editFailed(workspace.workspace_id, button.dataset.patrolEditFailed);
          host.querySelector("[data-patrol-content]").focus();
        }
        else if (button.hasAttribute("data-patrol-file-close")) { fileVersion++; host.querySelector("[data-patrol-file-detail]").hidden = true; }
        else if (button.hasAttribute("data-patrol-new-facts")) { const stream = host.querySelector("[data-patrol-facts]"); stream.scrollTop = stream.scrollHeight; button.hidden = true; }
        else if (button.hasAttribute("data-patrol-details")) toggleDetails();
        else if (button.dataset.workbenchShow) showPanel(button.dataset.workbenchShow);
        else if (button.hasAttribute("data-patrol-related")) { relatedOnly = !relatedOnly; patchGraph(); }
        else if (button.hasAttribute("data-patrol-progress-locate")) await locateProgress(JSON.parse(button.dataset.patrolProgressLocate), button);
        else if (button.dataset.patrolEdgeContext) {
          const id = button.dataset.patrolEdgeContext, revisionId = button.dataset.revisionId;
          await closeDialog(); await inspectContext(id, host.querySelector("[data-patrol-related]"), revisionId);
          showPanel("context");
        }
        else if (button.hasAttribute("data-patrol-dialog-close")) void closeDialog();
        else if (button.hasAttribute("data-patrol-history-open")) setHistoryOpen(!historyOpen);
        else if (button.hasAttribute("data-patrol-history-retry")) void loadOlderHistory(true);
        else if (button.hasAttribute("data-patrol-pending")) showDialog("requests", "待处理工作", "", host.querySelector("[data-patrol-requests]"));
        else if (button.hasAttribute("data-patrol-progress-open")) { progressFilter = "all"; await openProgress(); }
        else if (button.hasAttribute("data-patrol-grant-open")) await openGrant();
        else if (button.hasAttribute("data-patrol-live-retry") && loopId) { previews?.retry(); connect(loopId); }
        else if (button.dataset.action === "loop-revoke-grant") {
          if (!root.confirm("确认撤销当前工作区 Patrol 的授权？现有工作会保留。")) return;
          await api.mutateGrant(loopId, { command: "revoke" });
          await openGrant();
        }
        else if (button.dataset.patrolProgressRetry) {
          await api.retryTaskProgress(loopId, button.dataset.patrolProgressRetry);
          await openProgress(progressInspection?.selected?.progress_id);
        }
        else if (button.dataset.patrolProgressFilter) { progressFilter = button.dataset.patrolProgressFilter; renderProgressInspection(); }
        else if (button.dataset.patrolObservationProgress) await openProgress(button.dataset.patrolObservationProgress);
        else if (button.hasAttribute("data-patrol-observation-open")) {
          inspectionVersion++;
          observationInspection = observation; observationSections = {}; observationLoading = new Set();
          showDialog("observation", "Observation", root.FocusObservationView.render(observationInspection, observationSections));
        }
        else if (button.dataset.patrolObservationSection) await loadObservationSection(button.dataset.patrolObservationSection);
        else if (button.dataset.patrolObservationVersion) await openObservation(button.dataset.patrolObservationVersion);
        else if (button.hasAttribute("data-patrol-observation-retry")) await refreshObservation(true);
        else if (button.hasAttribute("data-patrol-lineage-retry") && projection) { previews?.retry(); await refreshLineage(projection); }
        else if (button.hasAttribute("data-patrol-facts-open")) await loadFacts();
        else if (button.hasAttribute("data-patrol-facts-older")) await loadFacts(true);
        else if (button.dataset.patrolRetry) { const row = inputs.retry(button.dataset.patrolRetry); if (row) void send(row); }
        else if (button.dataset.patrolAnswer) { inputs.edit({ request_id: button.dataset.patrolAnswer, request_revision: Number(button.dataset.requestRevision) }); const owner = token; if (await closeDialog() && owner === token) host.querySelector("textarea").focus(); }
        else if (button.hasAttribute("data-patrol-clear-target")) inputs.edit({ request_id: null, request_revision: null });
        else if (button.hasAttribute("data-patrol-clear-draft")) {
          for (const item of inputs.get().draft.attachments || []) await intake.remove(workspace.workspace_id, item.local_id);
          inputs.edit({ content: "", file_refs: [] }); host.querySelector("[data-patrol-content]").focus();
        }
        else if (button.dataset.patrolControl && loopId) {
          if (button.getAttribute("aria-busy") === "true") return;
          button.setAttribute("aria-busy", "true");
          try { await api.control(loopId, button.dataset.patrolControl); } finally { button.removeAttribute("aria-busy"); }
        }
        else if (button.hasAttribute("data-patrol-restart")) {
          if (button.getAttribute("aria-busy") === "true") return;
          button.setAttribute("aria-busy", "true"); const owner = token;
          try { const loop = await api.restartWorkspacePatrol(workspace.workspace_id); if (owner === token) connect(loop.loop_id); } finally { button.removeAttribute("aria-busy"); }
        }
        else if (button.dataset.waitAction) {
          const card = button.closest("[data-wait-request-id]");
          const request = liveStore.get().projection.wait_requests[card?.dataset.waitRequestId];
          const answer = { action: button.dataset.waitAction };
          if (answer.action === "revise_budget") answer.budgets = Object.fromEntries([...card.querySelectorAll('[name^="budget:"]')].map(input => [input.name.slice(7), Number(input.value)]));
          if (request) await respondWait(request, answer);
        }
        else if (button.dataset.action === "loop-resume-current-mission") {
          const card = button.closest("[data-wait-request-id]");
          const entity = projection.wait_requests[card?.dataset.waitRequestId];
          if (entity) await root.FocusLoopWaitRecovery.confirmAndResume({ request: { request_id: entity.entity_id, revision: entity.revision, ...entity.state }, loopId, confirm: message => root.confirm(message), submit: (id, requestId, body) => api.resumeWithCurrentMission(id, requestId, body) });
        }
        else if (button.dataset.contextId) await inspectContext(button.dataset.contextId, button, button.dataset.revisionId);
        else if (button.dataset.factId) {
          const owner = token, version = ++fileVersion;
          const revision = button.closest("[data-fact-revision]")?.dataset.factRevision;
          const detail = await api.factDetail(loopId, button.dataset.factId, lifetime.signal, revision ? Number(revision) : undefined);
          if (owner === token && version === fileVersion) {
            visibleFact = detail.fact;
            if (detail.file) {
              if (host.querySelector("[data-patrol-dialog]").open) await closeDialog();
              if (owner !== token || version !== fileVersion) return;
              host.querySelector("[data-patrol-file-body]").innerHTML = root.FocusLoopFactsView.renderFile(detail.file);
              host.querySelector("[data-patrol-file-detail]").hidden = false;
              root.FocusFilePreview?.mountHistory(host.querySelector("[data-patrol-file-body]"), detail.file, loopId);
            } else showDialog("fact", "事实与来源", root.FocusLoopFactsView.renderDetail(detail));
          }
        }
      } catch (failure) {
        if (clickOwner !== token) return;
        error(failure);
        if (host && host.querySelector("[data-patrol-dialog]").open) host.querySelector("[data-patrol-dialog-body]").insertAdjacentHTML("afterbegin", `<p role="alert">${view.escape(failure.message)}</p>`);
      }
    }
    async function mount(node, bound) {
      leave();
      host = node; workspace = bound; const current = token;
      lifetime = new AbortController();
      host.innerHTML = view.skeleton(bound, Boolean(onCompose));
      artifacts = root.FocusArtifactDrawer.create({ host: host.querySelector(".workspace-patrol"), api, getLoopId: () => loopId,
        onSource: async id => { artifacts.close(); if (!detailsOpen) toggleDetails(); await inspectContext(id, host.querySelector("[data-patrol-files-open]")); } });
      historyReferences = root.FocusPatrolInputReferences.create({ host, intake, workspaceId: () => bound.workspace_id, onError: error,
        onFile: async (row, file) => {
          const owner = token, version = ++fileVersion;
          const detail = await api.factDetail(row.loop_id || loopId, file.fact_id, lifetime.signal, file.revision);
          if (owner !== token || version !== fileVersion) return;
          if (!detail.file || detail.file.result_key !== file.result_key || (file.sha256 && detail.file.sha256 !== file.sha256)) throw new Error("引用版本不可用，原输入记录已保留");
          host.querySelector("[data-patrol-file-body]").innerHTML = root.FocusLoopFactsView.renderFile({ ...detail.file, fact_id: file.fact_id, revision: file.revision });
          host.querySelector("[data-patrol-file-detail]").hidden = false;
        } });
      const budgetHost = host.querySelector("[data-patrol-budget]");
      if (budgetHost) budget = root.FocusWorkspacePatrolBudget.create({ host: budgetHost, api,
        getDraft: () => inputs.get().draft, editDraft: patch => inputs.edit(patch), onError: error, canChooseInitial: !onCompose });
      if (onCompose) {
        host.querySelector("[data-patrol-composer]").classList.add("is-remote-composer");
        host.querySelector("[data-patrol-type]").hidden = true;
        host.querySelector("[data-patrol-type]").disabled = true;
        host.querySelector('[data-patrol-composer] button[type="submit"]').textContent = "打开机器人输入 ↗";
      } else materialPreview = root.FocusWorkspacePatrolMaterialPreview.create({ host, intake, workspaceId: () => bound.workspace_id, onError: error });
      inspector = root.FocusContextInspector.create({ api, onOpenTask, presentation: "workbench", getManifest: manifest, onSelectionChange: selectionChanged });
      inspector.mount(host.querySelector("[data-patrol-context]"));
      unlisten = inputs.subscribe(patchInputs);
      const options = { signal: lifetime.signal };
      host.querySelector("[data-patrol-composer]").addEventListener("submit", event => {
        event.preventDefault();
        if (onCompose) { void onCompose({ workspaceId: bound.workspace_id }).catch(error); return; }
        try { error(null); void send(inputs.submit(visibleContext())); } catch (failure) { error(failure); }
      }, options);
      const content = host.querySelector("[data-patrol-content]");
      content.addEventListener("input", event => {
        inputs.edit({ content: event.target.value });
        if (event.isComposing) return;
        if (/(?:^|\s)@[^@\n]*$/.test(event.target.value)) {
          if (host.querySelector("[data-patrol-file-picker]").hidden) void openFilePicker().catch(error);
          else renderFilePicker();
        } else host.querySelector("[data-patrol-file-picker]").hidden = true;
      }, options);
      content.addEventListener("keydown", event => { if (event.key === "Enter" && event.ctrlKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); content.form.requestSubmit(); } }, options);
      host.querySelector("[data-patrol-type]").addEventListener("change", event => inputs.edit({ input_type: event.target.value }), options);
      host.addEventListener("click", click, options);
      host.querySelector("[data-patrol-input-history]").addEventListener("scroll", () => { void loadOlderHistory(); }, { ...options, passive: true });
      root.addEventListener("resize", () => { void loadOlderHistory(); }, options);
      host.addEventListener("portfolio-clear-selection", () => { highlightIds = []; closeInspection(); }, options);
      host.addEventListener("keydown", event => {
        if (event.key !== "Escape" || event.isComposing) return;
        const file = host.querySelector("[data-patrol-file-detail]"), picker = host.querySelector("[data-patrol-file-picker]");
        if (!file.hidden || !picker.hidden) {
          fileVersion++; file.hidden = true; picker.hidden = true;
          content.focus({ preventScroll: true }); event.preventDefault();
        } else if (!host.querySelector("[data-patrol-dialog]").open) {
          if (historyOpen) { setHistoryOpen(false); event.preventDefault(); }
          else closeInspection();
        }
      }, options);
      host.addEventListener("change", event => {
        const key = event.target.dataset.patrolFactFilter;
        if (key) { factOptions[key] = event.target.value; void loadFacts().catch(error); }
        if (event.target.hasAttribute("data-patrol-progress-version")) void openProgress(event.target.value).catch(error);
      }, options);
      const saveWaitDraft = event => {
        const form = event.target.closest?.("[data-loop-wait-response]");
        const id = form?.closest("[data-wait-request-id]")?.dataset.waitRequestId;
        if (id) waitDrafts.set(id, root.FocusLoopWaitRequestView.serializeDraft(form));
      };
      host.addEventListener("input", saveWaitDraft, options);
      host.addEventListener("change", saveWaitDraft, options);
      host.addEventListener("submit", async event => {
        const form = event.target;
        if (!["agentLoopGrantForm", "agentLoopBudgetForm"].includes(form.id)) return;
        event.preventDefault(); event.stopPropagation();
        const owner = token;
        const submit = event.submitter;
        if (submit) submit.disabled = true;
        try {
          const body = form.id === "agentLoopGrantForm" ? root.FocusLoopView.readNarrowGrant(form, grantInspection?.grant) : { command: "adjust_budgets", budgets: root.FocusLoopExpansionBudget.readLoop(new FormData(form)) };
          await api.mutateGrant(loopId, body);
          if (owner === token) await openGrant();
        } catch (failure) {
          if (owner === token) form.insertAdjacentHTML("beforeend", `<p role="alert">${view.escape(failure.message)}</p>`);
        } finally { if (submit?.isConnected) submit.disabled = false; }
      }, options);
      const dialog = host.querySelector("[data-patrol-dialog]");
      dialog.addEventListener("cancel", event => { event.preventDefault(); void closeDialog(); }, options);
      dialog.addEventListener("close", () => { if (!dialog.open) restorePanel(); }, options);
      host.querySelector("[data-patrol-requests]").addEventListener("submit", async event => {
        event.preventDefault(); event.stopPropagation();
        const card = event.target.closest("[data-wait-request-id]");
        const request = liveStore.get().projection.wait_requests[card?.dataset.waitRequestId];
        if (!request) return;
        await respondWait(request, root.FocusLoopWaitRequestView.readAnswer(event.target, request.state.response_mode));
      }, options);
      await Promise.all([
        (async () => {
          try {
            const loop = await api.workspacePatrol(bound.workspace_id);
            if (current === token && loop) connect(loop.loop_id);
          } catch (failure) { if (current === token) error(failure); }
        })(),
        loadLatestHistory(),
      ]);
    }
    function leave() {
      artifacts?.dispose(); artifacts = null;
      budget?.dispose(); budget = null;
      if (graphFrame !== null && typeof cancelAnimationFrame === "function") cancelAnimationFrame(graphFrame);
      graphFrame = null; lineagePending = false;
      materialPreview?.dispose(); materialPreview = null;
      historyReferences?.dispose(); historyReferences = null;
      previews?.stop();
      root.document.body.classList.remove("has-patrol-workbench");
      relatedOnly = false; highlightIds = [];
      token++; lineageToken++; historyToken++; observationVersion++; factVersion++; fileVersion++; progressVersion++; inspectionVersion++;
      fileCandidates = [];
      lifetime?.abort(); lifetime = null;
      if (host) for (const element of host.querySelectorAll("[data-surface-managed], [data-patrol-composer]")) root.FocusSurfaceTransition.finish(element);
      host?.querySelector("[data-patrol-dialog]")?.close();
      const history = host?.querySelector("[data-patrol-input-history]");
      if (history) { history.hidden = true; history.inert = true; }
      historyOpen = false; historyReady = false; historyLoading = false; historyRefreshPending = false; historyFailure = null; historyFailureBefore = null; historyHTML = "";
      inspector?.dispose(); inspector = null; observation = null; observationIdentity = null; observationInspection = null;
      movedPanel = null; dialogKind = null; attentionTarget = null; projection = null; detailsOpen = false; detailsScroll = 0; lineageSnapshot = null;
      unlisten?.(); unlisten = null; host = null; workspace = null; loopId = null; lineageKey = null; historyKey = null; connection.stop();
    }
    const unsubscribe = liveStore.subscribe(patchLive);
    return Object.freeze({
      mount, leave,
      showDetails() { if (host && !detailsOpen) toggleDetails(); },
      showAttention(requestId, expectedLoopId) {
        if (!host || !loopId || (expectedLoopId && expectedLoopId !== loopId)) return;
        attentionTarget = requestId;
        showDialog("requests", "待处理工作", "", host.querySelector("[data-patrol-requests]"));
        locateAttention();
      },
      async showContext(contextId, revisionId) {
        if (!host || !loopId) return;
        if (!detailsOpen) toggleDetails();
        await inspectContext(contextId, host.querySelector("[data-patrol-details]"), revisionId);
      },
      isMounted(node, id) { return host === node && workspace?.workspace_id === id && node.contains(node.querySelector("[data-patrol-composer]")); },
      dispose() { leave(); unsubscribe(); },
    });
  }
  return Object.freeze({ create });
});
