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
 * 停止/失败在 composer 直接提供重新启用；与详情入口共享在途保护，保存回执和新 Loop 的 Live 生效分开呈现。
 * 事实投影诊断按当前 Loop 分页，修复复用服务端重放并刷新 Live；失败、重复点击与迟到响应不改变业务发送目标。
 */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusWorkspacePatrolController = api;
})(globalThis, function (root) {
  "use strict";
  // 源文案在渲染时取当前语言；模板插值始终保留原文。
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }

  function create({ api, inputs, intake, onCompose, liveStore, connection, onOpenTask, createRunPreviews, view = root.FocusWorkspacePatrolView }) {
    let host = null;
    let workspace = null;
    let loopId = null;
    let token = 0;
    let lineageKey = null;
    let lineageSnapshot = null;
    let lineageToken = 0;
    let historyRefresh = root.FocusWorkspacePatrolFeedback.createInputRefresh();
    let historyToken = 0;
    let historyOpen = false;
    let historyReady = false;
    let historyLoading = false;
    let historyRefreshPending = false;
    let historyFailure = null;
    let historyFailureBefore = null;
    let historyHTML = "";
    let renderedHistoryRevision = null, draftOccupied = null;
    let historyFileRefs = [], fileRefs = [], fileRefsKey = "", draftFileRefsKey = "";
    let unlisten = null;
    let materialPreview = null;
    let historyReferences = null;
    let artifacts = null;
    let timeline = null;


    let lifetime = null;
    let inspector = null;
    let detailsOpen = false;
    let detailsScroll = 0;
    let relatedOnly = false;
    let highlightIds = [];
    let projection = null;
    let restart = null;
    let deliveryRequested = false;
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
    let projectionPage = null, projectionVersion = 0, projectionRepair = null, projectionIncludeResolved = false;
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
      dialogKind = null; progressVersion++; factVersion++; inspectionVersion++; projectionVersion++; observationInspection = null;
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
      composer.querySelector("[data-patrol-details]").textContent = detailsOpen ? localize("收起详情 ↙") : localize("查看工作详情 ↗");
      if (!detailsOpen) { previews?.stop(); closeInspection(); }
      else if (projection) { patchGraph(); void refreshLineage(projection); void refreshObservation(); }
    }
    function closeInspection() { inspector?.close(); }
    function bindPanelSplit() {
      const panels = host.querySelector("[data-patrol-panel-split]");
      const handle = panels.querySelector("[data-patrol-panel-resizer]");
      const progress = panels.querySelector(".patrol-progress-card");
      const observation = panels.querySelector(".patrol-observation-card");
      const storageKey = "focus-patrol-panel-split-v1", options = { signal: lifetime.signal };
      let ratio = .6, drag = null;
      try {
        const stored = Number(root.localStorage?.getItem(storageKey));
        if (stored > 0 && stored < 1) ratio = stored;
      } catch { /* 本地存储不可用时仍可调整当前视图。 */ }
      const bounds = () => {
        if (!handle.getBoundingClientRect().height) return null;
        const style = root.getComputedStyle(panels);
        const minimum = parseFloat(style.getPropertyValue("--patrol-progress-min"));
        const observationMinimum = parseFloat(style.getPropertyValue("--patrol-observation-min"));
        const available = Math.max(panels.clientHeight - handle.getBoundingClientRect().height, minimum + observationMinimum);
        return { available, minimum, maximum: available - observationMinimum };
      };
      const sync = () => {
        const limits = bounds();
        if (!limits) return;
        for (const [name, value] of Object.entries({ min: limits.minimum, max: limits.maximum, now: progress.getBoundingClientRect().height })) {
          handle.setAttribute(`aria-value${name}`, String(Math.round(value / limits.available * 100)));
        }
      };
      const apply = () => {
        panels.style.setProperty("--patrol-progress-weight", `${ratio}fr`);
        panels.style.setProperty("--patrol-observation-weight", `${1 - ratio}fr`);
        sync();
      };
      const persist = () => { try { root.localStorage?.setItem(storageKey, String(ratio)); } catch { /* 调整无需依赖存储。 */ } };
      const resize = height => {
        const limits = bounds();
        if (!limits || limits.minimum >= limits.maximum) return;
        ratio = Math.max(limits.minimum, Math.min(limits.maximum, height)) / limits.available;
        apply();
      };
      handle.addEventListener("pointerdown", event => {
        if (event.button !== 0 || !bounds() || drag) return;
        event.preventDefault();
        handle.focus({ preventScroll: true });
        drag = { id: event.pointerId, y: event.clientY, height: progress.getBoundingClientRect().height, ratio };
        handle.setPointerCapture(event.pointerId);
      }, options);
      handle.addEventListener("pointermove", event => {
        if (drag?.id === event.pointerId) resize(drag.height + event.clientY - drag.y);
      }, options);
      const finish = event => {
        if (drag?.id !== event.pointerId) return;
        if (event.type === "pointerup") persist();
        else { ratio = drag.ratio; apply(); }
        drag = null;
        if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
      };
      for (const event of ["pointerup", "pointercancel", "lostpointercapture"]) handle.addEventListener(event, finish, options);
      handle.addEventListener("keydown", event => {
        const limits = bounds();
        if (!limits || !["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const delta = (event.key === "ArrowUp" ? -1 : 1) * (event.shiftKey ? 64 : 24);
        resize(event.key === "Home" ? limits.minimum : event.key === "End" ? limits.maximum : progress.getBoundingClientRect().height + delta);
        persist();
      }, options);
      handle.addEventListener("dblclick", () => { ratio = .6; apply(); persist(); }, options);
      apply();
      if (root.ResizeObserver) {
        const observer = new root.ResizeObserver(sync);
        observer.observe(panels); observer.observe(progress); observer.observe(observation);
        lifetime.signal.addEventListener("abort", () => observer.disconnect(), { once: true });
      }
    }
    async function openFilePicker() {
      if (!loopId) return;
      const owner = token, version = ++fileVersion;
      const picker = host.querySelector("[data-patrol-file-picker]");
      picker.hidden = false;
      picker.textContent = localize("正在读取文件版本…");
      const page = await api.facts(loopId, { kind: "artifact", limit: 100, signal: lifetime.signal });
      if (owner !== token || version !== fileVersion) return;
      fileCandidates = page.facts || [];
      renderFilePicker();
    }
    function renderFilePicker() {
      const query = inputs.get().draft.content.match(/(?:^|\s)@([^@\n]*)$/)?.[1]?.toLowerCase() || "";
      const candidates = fileCandidates.map(root.FocusLoopFactsView.fileReference).filter(file => file && file.path.toLowerCase().includes(query));
      host.querySelector("[data-patrol-file-picker]").innerHTML = candidates.map(file => `<button type="button" data-patrol-file-add="${view.escape(JSON.stringify(root.FocusLoopFactsView.inputFileReference(file)))}">${view.escape(file.path)} · ${view.escape((file.sha256 || file.result_key).slice(0, 8))}</button>`).join("") || localize("没有可引用的文件版本");
    }
    function addFileReference(file) {
      const draft = inputs.get().draft;
      const refs = [...(draft.file_refs || []).filter(item => item.result_key !== file.result_key), file];
      if (refs.length > 32) throw new Error(localize("一次输入最多引用 32 个文件版本"));
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
      button.textContent = relatedOnly ? localize("恢复全部关系") : localize("仅看所选关联");
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
      timeline?.selectContext(selected?.contextId);
      if (!selected) relatedOnly = false;
      patchGraph();
    }
    async function inspectContext(contextId, trigger, revisionId) {
      const node = manifest().nodes.find(node => node.context_id === contextId) || projection?.contexts?.[contextId]?.state || {};
      showPanel("context");
      await inspector.select(loopId, contextId, node, trigger, revisionId || node.current_revision_id);
    }
    async function locateProgress(ids, trigger) {
      highlightIds = ids;
      relatedOnly = false;
      const notice = host.querySelector("[data-workbench-location]");
      notice.hidden = false;
      const available = ids.filter(id => manifest().nodes.some(node => node.context_id === id));
      notice.textContent = !ids.length ? localize("此条目尚未关联工作线") : !available.length ? localize("关联工作线尚未发布到当前关系图") : localize`${available.length} 条关联工作线已突出显示，请选择查看`;
      showPanel("graph"); patchGraph();
      if (available.length === 1) await inspectContext(available[0], trigger);
      host.querySelector(`[data-patrol-lineage] [data-context-id="${root.CSS.escape(available[0] || "")}"]`)?.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
    function inspectEdges(identity) {
      showDialog("relationship", localize("已提交来源关系"), root.FocusPortfolioMapView.relationship(manifest(), identity));
    }
    async function refreshObservation(force = false) {
      const id = projection?.round?.state?.observation_id;
      if (!id || (!force && id === observationIdentity)) return;
      observationIdentity = id;
      observation = null;
      const owner = token, version = ++observationVersion;
      host.querySelector("[data-patrol-observation]").textContent = localize("正在读取本轮冻结输入");
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
          host.querySelector("[data-patrol-observation]").innerHTML = localize`读取失败：${view.escape(failure.message)} <button data-patrol-observation-retry>重试</button>`;
        }
      }
    }
    async function loadObservationSection(section) {
      const selected = observationInspection, owner = token, loading = observationLoading;
      if (!selected || loading.has(section)) return;
      loading.add(section);
      const old = section === "patrol_history" && !observationSections[section]?.has_more ? null : observationSections[section];
      try {
        const page = await api.observation(loopId, selected.observation_id, { section, cursor: old?.next_cursor, signal: lifetime.signal });
        if (owner !== token || selected !== observationInspection) return;
        observationSections[section] = { ...page, items: [...(old?.items || []), ...page.items] };
        if (dialogKind === "observation") showDialog("observation", "Observation", root.FocusObservationView.render(selected, observationSections));
      } catch (failure) {
        if (section === "patrol_history" && failure.status === 409 && owner === token && selected === observationInspection) {
          delete observationSections[section];
          if (dialogKind === "observation") showDialog("observation", "Observation", root.FocusObservationView.render(selected, observationSections));
        }
        throw failure;
      } finally { loading.delete(section); }
    }
    async function openObservation(id) {
      const owner = token, version = ++inspectionVersion;
      observationInspection = null; observationSections = {}; observationLoading = new Set();
      showDialog("observation", "Observation", localize("正在读取所选冻结版本"));
      const value = await api.observation(loopId, id, { signal: lifetime.signal });
      if (owner !== token || version !== inspectionVersion || dialogKind !== "observation") return;
      observationInspection = value;
      showDialog("observation", "Observation", root.FocusObservationView.render(value, observationSections));
    }
    function visibleContext() {
      if (!projection || projection.loop_id !== loopId) return null;
      const shown = selector => detailsOpen && Boolean(host.querySelector(selector)?.getClientRects().length);
      const facts = [...(shown("[data-patrol-facts]") ? timeline ? timeline.visibleFacts() : root.FocusLoopLiveSelectors.selectFacts(projection) : []),
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
      const toolbar = localize`<label>查看已提交版本<select data-patrol-progress-version>${versions.map(item => `<option value="${view.escape(item.progress_id)}"${item.progress_id === selected?.progress_id ? " selected" : ""}>P${item.generation}${item.progress_id === page.current?.progress_id ? localize(" · 当前") : ""}</option>`).join("")}</select></label>${page.history_has_more ? localize("<p>列表为最近 32 个版本；Observation 可读取本轮精确前序。</p>") : ""}<p>版本 ${view.escape(selected?.progress_id || localize("尚未提交"))}，不会随实时事件改写此份检查。</p>`;
      showDialog("progress", localize("Task Progress · 已提交版本"), toolbar + view.progress(selected, true, progressFilter) + (page.work || []).map(work => localize`<p>进度沉淀：${view.escape(work.state)}${work.failure_kind ? ` · ${view.escape(work.failure_kind)}` : ""}${work.state === "blocked" ? localize` <button data-patrol-progress-retry="${view.escape(work.work_id || work.observation_id)}">重试这份冻结进度</button>` : ""}</p>`).join(""));
    }
    async function openProgress(progressId = null) {
      const owner = token, version = ++progressVersion;
      showDialog("progress", "Task Progress", localize('正在读取已提交版本 <button data-patrol-progress-open>重试读取</button>'));
      const page = await api.taskProgress(loopId, progressId, lifetime.signal);
      if (owner !== token || version !== progressVersion || dialogKind !== "progress") return;
      progressInspection = page;
      renderProgressInspection();
    }
    async function openGrant() {
      const owner = token;
      showDialog("grant", localize("工作区授权与预算"), localize('正在读取当前授权 <button data-patrol-grant-open>重试</button>'));
      const value = await api.get(loopId);
      if (owner !== token || dialogKind !== "grant") return;
      grantInspection = value;
      showDialog("grant", localize("工作区授权与预算"), root.FocusLoopGrantView.grantControls(value) || localize("当前没有可修改的有效授权。历史工作仍可检查。"));
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
      if (dialogKind !== "facts") showDialog("facts", "LoopFact", localize('正在读取事实 <button data-patrol-facts-open>重试读取</button>'));
      const page = await api.facts(loopId, { ...factOptions, before: older ? factPage?.next_before : undefined, signal: lifetime.signal });
      if (owner !== token || version !== factVersion || dialogKind !== "facts") return;
      const merged = older ? [...(page.facts || []), ...(factPage?.facts || [])] : page.facts || [];
      factPage = { ...page, facts: [...new Map(merged.map(item => [item.fact_id, item])).values()] };
      showDialog("facts", "LoopFact", localize`<div class="patrol-fact-filters"><label>类型<select data-patrol-fact-filter="kind">${["", "run", "test", "context_revision", "artifact", "workspace", "directive"].map(v => `<option value="${v}"${factOptions.kind === v ? " selected" : ""}>${v || localize("全部")}</option>`).join("")}</select></label><label>事实状态<select data-patrol-fact-filter="status">${root.FocusLoopFactsView.statusOptions.map(([value, label]) => `<option value="${value}"${factOptions.status === value ? " selected" : ""}>${label}</option>`).join("")}</select></label><label>业务结果<select data-patrol-fact-filter="outcomeStatus">${root.FocusLoopFactsView.outcomeOptions.map(([value, label]) => `<option value="${value}"${factOptions.outcomeStatus === value ? " selected" : ""}>${label}</option>`).join("")}</select></label><label>Context<input data-patrol-fact-filter="contextId" value="${view.escape(factOptions.contextId)}" placeholder="精确 Context 身份"></label></div><div class="patrol-facts"><table><tbody>${root.FocusLoopFactsView.renderRows(factPage.facts)}</tbody></table></div><p>${factPage.has_more ? localize("后续还有事实") : localize("已到当前查询范围末尾")}</p>${factPage.has_more ? localize('<button data-patrol-facts-older>加载更早事实</button>') : ""}`);
    }

    async function openProjectionFailures(older = false) {
      if (!loopId || projectionRepair) return;
      const owner = token, identity = loopId, version = ++projectionVersion;
      const before = older ? projectionPage?.next_before : null;
      showDialog("projection", localize("事实投影诊断"), localize('正在读取失败记录 <button data-patrol-projection-refresh>重试读取</button>'));
      const page = await api.projectionFailures(identity, { before, includeResolved: projectionIncludeResolved, signal: lifetime.signal });
      if (owner !== token || identity !== loopId || version !== projectionVersion || dialogKind !== "projection") return;
      projectionPage = older ? { ...page, items: [...projectionPage.items, ...page.items] } : page;
      showDialog("projection", localize("事实投影诊断"), view.projectionFailures(projectionPage, { includeResolved: projectionIncludeResolved }));
    }

    async function repairProjection(failureId) {
      if (!loopId || projectionRepair) return;
      const operation = { loopId, failureId }, owner = token, version = ++projectionVersion;
      projectionRepair = operation;
      showDialog("projection", localize("事实投影诊断"), view.projectionFailures(projectionPage, { pendingId: failureId, includeResolved: projectionIncludeResolved }));
      try {
        const result = await api.repairProjectionFailure(operation.loopId, failureId);
        if (owner !== token || operation.loopId !== loopId) return;
        projectionPage = { ...projectionPage, items: projectionPage.items.map(row => row.failure_id === failureId ? result.failure : row) };
        if (version === projectionVersion && dialogKind === "projection") showDialog("projection", localize("事实投影诊断"), view.projectionFailures(projectionPage, {
          includeResolved: projectionIncludeResolved,
          message: result.failure.status === "resolved" ? localize("事实投影已修复。") : localize("重新生成仍失败，记录已更新。请修复来源或实现后再次尝试。"),
        }));
        // 已物化事实和诊断不一定产生新业务事件，主动重取权威快照。
        connection.stop(); connect(operation.loopId);
      } catch (failure) {
        if (owner === token && operation.loopId === loopId && version === projectionVersion && dialogKind === "projection") {
          showDialog("projection", localize("事实投影诊断"), view.projectionFailures(projectionPage, {
            includeResolved: projectionIncludeResolved, message: localize`修复请求未确认：${failure.message}。可刷新诊断核对结果。`,
          }));
        }
      } finally {
        if (projectionRepair === operation) {
          projectionRepair = null;
          if (owner === token && dialogKind === "projection") {
            for (const button of host.querySelectorAll("[data-patrol-projection-repair], [data-patrol-projection-refresh], [data-patrol-projection-more], [data-patrol-projection-resolved]")) button.disabled = false;
          }
        }
      }
    }
    function patchHistory(model) {
      if (renderedHistoryRevision === model.historyRevision) return;
      renderedHistoryRevision = model.historyRevision;
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
      patchHistoryActions(model, true);
      patchHistoryStatus();
      void loadOlderHistory();
    }
    function patchHistoryActions(model, force = false) {
      const occupied = Boolean(model.draft.content || model.draft.file_refs?.length || model.draft.attachments?.length);
      if (!force && draftOccupied === occupied) return;
      draftOccupied = occupied;
      for (const button of host.querySelectorAll("[data-patrol-edit-failed]")) {
        button.disabled = occupied;
        button.title = occupied ? localize("先处理当前草稿") : "";
      }
    }
    function patchHistoryStatus() {
      const status = host.querySelector("[data-patrol-history-status]");
      status.textContent = historyFailure ? localize`读取记录失败：${historyFailure.message || historyFailure}` : historyLoading ? localize("正在读取记录…") : "";
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
      const refresh = initialize ? null : historyRefresh.take();
      historyLoading = true; historyFailure = null; patchHistoryStatus();
      try {
        const page = initialize ? await api.workspaceInputs(workspace.workspace_id)
          : await root.FocusWorkspacePatrolFeedback.refreshInputs(api, workspace.workspace_id, inputs.get(), refresh);
        if (current !== token || version !== historyToken) return;
        intake.history(workspace.workspace_id, page, false, initialize);
        if (initialize) await intake.reconcile(workspace.workspace_id, page);
        if (current !== token) return;
        historyReady = true;
      } catch (failure) {
        if (current === token && version === historyToken) {
          if (refresh) historyRefresh.restore(refresh);
          historyFailure = failure; historyFailureBefore = null;
        }
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
      const historyChanged = renderedHistoryRevision !== model.historyRevision;
      patchHistory(model);
      patchHistoryActions(model);
      materialPreview?.render(model.draft);
      const content = host.querySelector("[data-patrol-content]");
      if (content.value !== model.draft.content) content.value = model.draft.content;
      host.querySelector("[data-patrol-file-refs]").innerHTML = (model.draft.file_refs || []).map(file => localize`<span>${view.escape(file.path)} · ${view.escape((file.sha256 || file.result_key).slice(0, 8))}<button type="button" data-patrol-file-remove="${view.escape(file.result_key)}" aria-label="移除文件引用">×</button></span>`).join("");
      host.querySelector("[data-patrol-type]").value = model.draft.input_type;
      host.querySelector("[data-patrol-target]").textContent = model.draft.request_id ? localize`回答请求 ${model.draft.request_id} · 版本 ${model.draft.request_revision ?? localize("未知")}` : "";
      host.querySelector("[data-patrol-clear-target]").hidden = !model.draft.request_id;
      patchRecovery();
      if (historyChanged) historyFileRefs = model.rows.filter(row => row.status === "accepted").flatMap(row => row.file_refs || []);
      const draftRefsKey = JSON.stringify(model.draft.file_refs);
      if (historyChanged || draftRefsKey !== draftFileRefsKey) {
        draftFileRefsKey = draftRefsKey;
        const refs = [...model.draft.file_refs, ...historyFileRefs], refsKey = JSON.stringify(refs);
        if (refsKey !== fileRefsKey) {
          fileRefs = refs; fileRefsKey = refsKey;
          patchFileFacts();
        }
      }
    };
    function patchRecovery() {
      if (!host) return;
      const current = projection?.loop_id === loopId ? projection : null;
      const recovery = root.FocusWorkspacePatrolFeedback.recovery(current, inputs.get());
      if (restart?.phase === "accepted" && current?.loop_id === restart.loopId) {
        if (recovery) restart = null;
        else if (current.loop?.state?.status) restart.phase = "applied";
      }
      if (restart?.phase === "applied" && recovery) restart = null;
      const pending = restart && restart.phase !== "applied";
      host.querySelector("[data-patrol-recovery]").hidden = !recovery && !restart;
      host.querySelector("[data-patrol-recovery-message]").textContent = restart
        ? ({ requesting: localize("正在重新启用…"), accepted: localize("重新启用请求已受理，正在确认工作状态…"), applied: localize("工作已重新启用。") }[restart.phase]) : recovery?.text || "";
      for (const button of host.querySelectorAll("[data-patrol-restart]")) {
        const inline = !!button.closest("[data-patrol-recovery]");
        button.hidden = inline ? !recovery && !pending : !["completed", "stopped", "failed"].includes(current?.loop?.state?.status);
        button.disabled = !!pending || liveStore.get().connection.status !== "live";
        button.setAttribute("aria-busy", String(!!pending));
        if (inline) button.textContent = recovery?.label || localize("重新启用");
      }
      const latest = inputs.get().rows[0];
      host.querySelector("[data-patrol-receipt]").textContent = latest?.status === "accepted" && recovery ? localize("已保存")
        : ({ accepted: localize("已受理 · 变化以提交进度为准"), sending: localize("发送中"), failed: localize("发送失败 · 可在输入记录重试") }[latest?.status] || "");
    }
    function patchFileFacts(announce = false) {
      if (timeline) { timeline.setFileReferences(fileRefs); return; }
      if (!host || !projection) return;
      const stream = host.querySelector("[data-patrol-facts]"), top = stream.scrollTop;
      const atEnd = stream.scrollHeight - top - stream.clientHeight < 24;
      const facts = root.FocusLoopFactsView.markFileReferences(root.FocusLoopLiveSelectors.selectFacts(projection), fileRefs);
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
          if (snapshot.complete !== true) throw new Error(localize("来源关系尚未完整返回"));
          lineageSnapshot = snapshot;
          host.querySelector("[data-patrol-lineage-error]").hidden = true;
          scheduleGraphPatch(); inspector?.refresh();
        }
      } catch (failure) {
        if (current === token && version === lineageToken && failure.name !== "AbortError") {
          lineageKey = null;
          const notice = host.querySelector("[data-patrol-lineage-error]");
          notice.hidden = false;
          notice.innerHTML = localize`${lineageSnapshot ? localize("关系图未更新，保留上次完整结果") : localize("关系图读取失败")}：${view.escape(failure.message)} <button data-patrol-lineage-retry>重试关系图</button>`;
          if (!lineageSnapshot) host.querySelector("[data-patrol-lineage]").textContent = localize("尚未取得已提交关系");
        }
      }
    }
    function patchLive({ projection: next, connection: status }) {
      if (!host) return;
      patchRetry();
      if (!next) {
        patchRecovery();
        host.querySelector("[data-patrol-state]").textContent = status?.status === "unavailable" ? localize("连接不可用 · 正在保留输入") : localize("正在连接工作状态");
        return;
      }
      if (next.loop_id !== loopId) return;
      const previous = projection;
      projection = next;
      timeline?.sync(loopId, next);
      patchRecovery();
      for (const [id] of waitUi) {
        if (!projection.wait_requests[id] || !["open", "resolving"].includes(projection.wait_requests[id].state.status)) waitUi.delete(id);
      }
      const loop = projection.loop?.state || {};
      const states = { running: localize("推进中"), paused: localize("已暂停"), stopped: localize("已停止"), failed: localize("执行失败"), waiting_user: localize("等待处理"), completed: localize("已完成当前工作") };
      const connections = { live: localize("实时更新"), syncing: localize("同步中"), connecting: localize("连接中"), reconnecting: localize("重连中"), resyncing: localize("重新同步"), unavailable: localize("连接不可用") };
      const stateText = loop.status === "waiting_user" && loop.waiting_reason === "awaiting_input" ? root.FocusWorkspacePatrolFeedback.status(projection, status, inputs.get()).text : `${states[loop.status] || localize("准备中")} · ${connections[status.status] || localize("同步中")}`;
      host.querySelector("[data-workbench-status]").textContent = stateText;
      if (!timeline) host.querySelector("[data-timeline-scope]").textContent = localize("跨 Round 工作历史");
      host.querySelector("[data-timeline-connection]").textContent = connections[status.status] || localize("同步中");
      if (["rounds", "runs", "patrol_sessions", "loop", "directives"].some(key => previous?.[key] !== projection[key])) {
        host.querySelector("[data-patrol-rounds]").innerHTML = root.FocusObservationView.rounds(projection);
      }
      const stateNode = host.querySelector("[data-patrol-state]");
      if (stateNode.textContent !== stateText) stateNode.textContent = stateText;
      const deliveryNode = host.querySelector("[data-patrol-delivery]"), deliveryHtml = view.delivery(projection, workspace);
      deliveryNode.hidden = !deliveryHtml;
      if (deliveryNode.innerHTML !== deliveryHtml) deliveryNode.innerHTML = deliveryHtml;
      if (deliveryRequested && deliveryHtml) {
        deliveryRequested = false;
        deliveryNode.scrollIntoView?.({ block: "nearest" });
        deliveryNode.querySelector("button")?.focus({ preventScroll: true });
      }
      host.querySelector('[data-patrol-control="pause"]').disabled = !(loop.status === "running" || (loop.status === "waiting_user" && loop.waiting_reason === "awaiting_input"));
      host.querySelector('[data-patrol-control="resume"]').disabled = loop.status !== "paused";
      host.querySelector('[data-patrol-control="stop"]').disabled = !["running", "paused", "waiting_user"].includes(loop.status);
      if (previous?.task_progress !== projection.task_progress) {
        const progressNode = host.querySelector("[data-patrol-progress]"), progressHtml = view.progress(projection.task_progress);
        if (progressNode.innerHTML !== progressHtml) progressNode.innerHTML = progressHtml;
      }
      if (previous?.wait_requests !== projection.wait_requests) patchRequests();
      locateAttention();
      const pending = Object.values(projection.wait_requests).filter(item => item.state.status === "open").length;
      const pendingButton = host.querySelector("[data-patrol-pending]");
      pendingButton.hidden = !pending;
      const pendingText = localize`${pending} 项待处理`;
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
        if (node.textContent !== localize("本轮尚未冻结")) node.textContent = localize("本轮尚未冻结");
        host.querySelector("[data-patrol-observation-open]").disabled = true;
      }
      if (historyRefresh.observe(projection, inputs.get())) {
        void loadLatestHistory();
      }
    }
    function connect(identity) {
      if (loopId !== identity) previews?.stop();
      loopId = identity;
      timeline?.sync(identity, null);
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
        else if (button.dataset.patrolTimelineIntent) {
          if (!detailsOpen) toggleDetails();
          showPanel("facts");
          timeline?.followIntent(button.dataset.patrolTimelineIntent);
        }
        else if (button.dataset.patrolDeliveryContext) {
          if (!detailsOpen) toggleDetails();
          await inspectContext(button.dataset.patrolDeliveryContext, button, button.dataset.revisionId);
        }
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
        else if (button.hasAttribute("data-patrol-pending")) showDialog("requests", localize("待处理工作"), "", host.querySelector("[data-patrol-requests]"));
        else if (button.hasAttribute("data-patrol-progress-open")) { progressFilter = "all"; await openProgress(); }
        else if (button.hasAttribute("data-patrol-grant-open")) await openGrant();
        else if (button.hasAttribute("data-patrol-projection-open") || button.hasAttribute("data-patrol-projection-refresh")) await openProjectionFailures();
        else if (button.hasAttribute("data-patrol-projection-more")) await openProjectionFailures(true);
        else if (button.dataset.patrolProjectionRepair) await repairProjection(button.dataset.patrolProjectionRepair);
        else if (button.hasAttribute("data-patrol-live-retry") && loopId) { previews?.retry(); connect(loopId); }
        else if (button.dataset.action === "loop-revoke-grant") {
          if (!root.confirm(localize("确认撤销当前工作区 Patrol 的授权？现有工作会保留。"))) return;
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
          if (button.disabled || button.getAttribute("aria-busy") === "true") return;
          const owner = token, operation = { phase: "requesting", loopId: null };
          restart = operation; error(null); patchRecovery();
          try {
            const loop = await api.restartWorkspacePatrol(workspace.workspace_id);
            if (owner === token && restart === operation) {
              operation.phase = "accepted"; operation.loopId = loop.loop_id;
              connect(loop.loop_id);
            }
          } catch (failure) { if (owner === token) restart = null; throw failure; }
          finally { if (owner === token) patchRecovery(); }
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
            } else showDialog("fact", localize("事实与来源"), root.FocusLoopFactsView.renderDetail(detail));
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
      if (root.FocusWorkTimeline && api.timeline) timeline = root.FocusWorkTimeline.create({
        host: host.querySelector(".patrol-facts-card"), api, signal: lifetime.signal,
      });
      artifacts = root.FocusArtifactDrawer.create({ host: host.querySelector(".workspace-patrol"), api, getLoopId: () => loopId,
        onSource: async id => { artifacts.close(); if (!detailsOpen) toggleDetails(); await inspectContext(id, host.querySelector("[data-patrol-files-open]")); } });
      historyReferences = root.FocusPatrolInputReferences.create({ host, intake, workspaceId: () => bound.workspace_id, onError: error,
        onFile: async (row, file) => {
          const owner = token, version = ++fileVersion;
          const detail = await api.factDetail(row.loop_id || loopId, file.fact_id, lifetime.signal, file.revision);
          if (owner !== token || version !== fileVersion) return;
          if (!detail.file || detail.file.result_key !== file.result_key || (file.sha256 && detail.file.sha256 !== file.sha256)) throw new Error(localize("引用版本不可用，原输入记录已保留"));
          host.querySelector("[data-patrol-file-body]").innerHTML = root.FocusLoopFactsView.renderFile({ ...detail.file, fact_id: file.fact_id, revision: file.revision });
          host.querySelector("[data-patrol-file-detail]").hidden = false;
        } });
      if (onCompose) {
        host.querySelector("[data-patrol-composer]").classList.add("is-remote-composer");
        host.querySelector("[data-patrol-type]").hidden = true;
        host.querySelector("[data-patrol-type]").disabled = true;
        host.querySelector('[data-patrol-composer] button[type="submit"]').textContent = localize("打开机器人输入 ↗");
      } else materialPreview = root.FocusWorkspacePatrolMaterialPreview.create({ host, intake, workspaceId: () => bound.workspace_id, onError: error });
      inspector = root.FocusContextInspector.create({ api, onOpenTask, presentation: "workbench", getManifest: manifest, onSelectionChange: selectionChanged });
      inspector.mount(host.querySelector("[data-patrol-context]"));
      unlisten = inputs.subscribe(patchInputs);
      const options = { signal: lifetime.signal };
      bindPanelSplit();
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
        if (event.target.hasAttribute("data-patrol-projection-resolved")) {
          projectionIncludeResolved = event.target.checked;
          void openProjectionFailures().catch(error);
        }
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
          const body = form.id === "agentLoopGrantForm" ? root.FocusLoopGrantView.readNarrowGrant(form, grantInspection?.grant) : { command: "adjust_budgets", budgets: root.FocusLoopExpansionBudget.readLoop(new FormData(form)) };
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
      timeline?.dispose(); timeline = null;
      artifacts?.dispose(); artifacts = null;
      if (graphFrame !== null && typeof cancelAnimationFrame === "function") cancelAnimationFrame(graphFrame);
      graphFrame = null; lineagePending = false;
      materialPreview?.dispose(); materialPreview = null;
      historyReferences?.dispose(); historyReferences = null;
      previews?.stop();
      root.document.body.classList.remove("has-patrol-workbench");
      relatedOnly = false; highlightIds = [];
      token++; lineageToken++; historyToken++; observationVersion++; factVersion++; fileVersion++; progressVersion++; inspectionVersion++;
      projectionVersion++; projectionPage = null; projectionRepair = null; projectionIncludeResolved = false;
      fileCandidates = [];
      lifetime?.abort(); lifetime = null;
      if (host) for (const element of host.querySelectorAll("[data-surface-managed], [data-patrol-composer]")) root.FocusSurfaceTransition.finish(element);
      host?.querySelector("[data-patrol-dialog]")?.close();
      const history = host?.querySelector("[data-patrol-input-history]");
      if (history) { history.hidden = true; history.inert = true; }
      historyOpen = false; historyReady = false; historyLoading = false; historyRefreshPending = false; historyFailure = null; historyFailureBefore = null; historyHTML = "";
      renderedHistoryRevision = null; draftOccupied = null; historyFileRefs = []; fileRefs = []; fileRefsKey = ""; draftFileRefsKey = "";
      historyRefresh = root.FocusWorkspacePatrolFeedback.createInputRefresh();
      inspector?.dispose(); inspector = null; observation = null; observationIdentity = null; observationInspection = null;
      movedPanel = null; dialogKind = null; attentionTarget = null; projection = null; restart = null; deliveryRequested = false; detailsOpen = false; detailsScroll = 0; lineageSnapshot = null;
      unlisten?.(); unlisten = null; host = null; workspace = null; loopId = null; lineageKey = null; connection.stop();
    }
    const unsubscribe = liveStore.subscribe(patchLive);
    return Object.freeze({
      mount, leave,
      showDetails() { if (host && !detailsOpen) toggleDetails(); },
      showDelivery() {
        if (!host) return;
        if (detailsOpen) toggleDetails();
        deliveryRequested = true;
        if (projection) patchLive(liveStore.get());
      },
      showAttention(requestId, expectedLoopId) {
        if (!host || !loopId || (expectedLoopId && expectedLoopId !== loopId)) return;
        attentionTarget = requestId;
        showDialog("requests", localize("待处理工作"), "", host.querySelector("[data-patrol-requests]"));
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
