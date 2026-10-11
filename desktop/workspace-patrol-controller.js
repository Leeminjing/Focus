/* 本文件对外提供工作区 Patrol 页面控制器。
 * 输入为已绑定工作区、共享 Loop API、Live Store/Connection 及输入 store；输出为逐条耐久回执和图主导工作台更新；选择身份由原 ContextInspector 持有。
 * 具体工作流为先读取绑定/历史再订阅已有单路 Live，依据投影结构共享只更新变化区域，保留 composer、进度和未变等待表单 DOM，异步响应按工作区代际隔离；
 * 主输入走 workspace intake，查看 Context 不改变目标；清空仅编辑未提交正文并返回输入焦点，不触及回答目标、历史或运行。
 * 显隐复用 SurfaceTransition 保留输入节点，运行预览复用共享 Run 观察器，折叠/离开释放订阅；原生 dialog 管理焦点，AbortController 清理监听与读请求。示例：await controller.mount(host, workspace)。
 */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusWorkspacePatrolController = api;
})(globalThis, function (root) {
  "use strict";
  function create({ api, inputs, liveStore, connection, onOpenTask, createRunPreviews, view = root.FocusWorkspacePatrolView }) {
    let host = null;
    let workspace = null;
    let loopId = null;
    let token = 0;
    let lineageKey = null;
    let lineageSnapshot = null;
    let lineageToken = 0;
    let historyKey = null;
    let historyToken = 0;
    let unlisten = null;


    let lifetime = null;
    let inspector = null;
    let detailsOpen = false;
    let detailsScroll = 0;
    let relatedOnly = false;
    let highlightIds = [];
    let projection = null;
    let observation = null;
    let observationIdentity = null;
    let observationVersion = 0;
    let observationSections = {};
    let observationInspection = null;
    let observationLoading = new Set();
    let dialogKind = null;
    let movedPanel = null;



    let factPage = null;
    let factOptions = { kind: "", status: "", outcomeStatus: "", contextId: "" };
    let factVersion = 0;
    let progressInspection = null;
    let progressVersion = 0;
    let progressFilter = "all";
    let grantInspection = null;
    const waitDrafts = root.FocusLoopWaitRequestView.createDraftStore();
    const waitUi = new Map();
    const previews = createRunPreviews?.((values, structural) => {
      if (!host || !detailsOpen) return;
      if (structural) patchGraph();
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
      dialog.querySelector("h2").textContent = title;
      const body = dialog.querySelector("[data-patrol-dialog-body]");
      body.innerHTML = html;
      if (panel) { movedPanel = panel; panel.hidden = false; body.append(panel); }
      if (!dialog.open || dialog.inert) void root.FocusSurfaceTransition.visible(dialog, true, { modal: true, source: root.document.activeElement });
    }
    function closeDialog() {
      dialogKind = null; progressVersion++; factVersion++; observationInspection = null;
      return root.FocusSurfaceTransition.visible(host.querySelector("[data-patrol-dialog]"), false, { modal: true });
    }
    function toggleDetails() {
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
    function renderProgressInspection() {
      const page = progressInspection;
      if (!page || dialogKind !== "progress") return;
      const selected = page.selected || page.current;
      const versions = [...(page.history || [])];
      if (selected && !versions.some(item => item.progress_id === selected.progress_id)) versions.unshift(selected);
      const toolbar = `<label>查看已提交版本<select data-patrol-progress-version>${versions.map(item => `<option value="${view.escape(item.progress_id)}"${item.progress_id === selected?.progress_id ? " selected" : ""}>P${item.generation}${item.progress_id === page.current?.progress_id ? " · 当前" : ""}</option>`).join("")}</select></label>${page.history_has_more ? "<p>列表为最近 32 个版本；Observation 可读取本轮精确前序。</p>" : ""}<p>版本 ${view.escape(selected?.progress_id || "尚未提交")}，不会随实时事件改写此份检查。</p>`;
      showDialog("progress", "Task Progress · 已提交版本", toolbar + view.progress(selected, true, progressFilter) + (page.work || []).map(work => `<p>进度沉淀：${view.escape(work.state)}${work.failure_kind ? ` · ${view.escape(work.failure_kind)}` : ""}${work.state === "blocked" ? ` <button data-patrol-progress-retry="${view.escape(work.observation_id)}">重试这份冻结进度</button>` : ""}</p>`).join(""));
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
    const patchInputs = model => {
      if (!host) return;
      host.querySelector("[data-patrol-history]").innerHTML = view.history(model);
      const content = host.querySelector("[data-patrol-content]");
      if (content.value !== model.draft.content) content.value = model.draft.content;
      host.querySelector("[data-patrol-type]").value = model.draft.input_type;
      host.querySelector("[data-patrol-target]").textContent = model.draft.request_id ? `回答请求 ${model.draft.request_id} · 版本 ${model.draft.request_revision ?? "未知"}` : "";
      host.querySelector("[data-patrol-clear-target]").hidden = !model.draft.request_id;
      host.querySelector("[data-patrol-fold]").textContent = model.expanded ? "收起历史" : "展开更早记录";
      host.querySelector("[data-patrol-older]").hidden = !model.expanded || !model.cursor;
      const latest = model.visible[0];
      host.querySelector("[data-patrol-receipt]").textContent = latest ? ({ accepted: "已受理 · 变化以提交进度为准", sending: "发送中", failed: "发送失败 · 可在输入记录重试" }[latest.status] || "") : "";
    };
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
          patchGraph(); inspector?.refresh();
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
      for (const [id] of waitUi) {
        if (!projection.wait_requests[id] || !["open", "resolving"].includes(projection.wait_requests[id].state.status)) waitUi.delete(id);
      }
      const loop = projection.loop?.state || {};
      const states = { running: "推进中", paused: "已暂停", stopped: "已停止", failed: "执行失败", waiting_user: "等待处理", completed: "已完成当前工作" };
      const connections = { live: "实时更新", syncing: "同步中", connecting: "连接中", reconnecting: "重连中", resyncing: "重新同步", unavailable: "连接不可用" };
      const stateText = loop.status === "waiting_user" && loop.waiting_reason === "awaiting_input" ? "等待新输入" : `${states[loop.status] || "准备中"} · ${connections[status.status] || "同步中"}`;
      host.querySelector("[data-workbench-status]").textContent = stateText;
      host.querySelector("[data-workbench-round]").textContent = projection.round?.state?.number ? `当前 Round ${projection.round.state.number} · Live 事实` : "当前 Live 事实";
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
      const pending = Object.values(projection.wait_requests).filter(item => item.state.status === "open").length;
      const pendingButton = host.querySelector("[data-patrol-pending]");
      pendingButton.hidden = !pending;
      const pendingText = `${pending} 项待处理`;
      if (pendingButton.textContent !== pendingText) pendingButton.textContent = pendingText;
      if (previous?.facts !== projection.facts) root.FocusLoopFactsView?.reconcileTimeline(host.querySelector("[data-patrol-facts]"), root.FocusLoopLiveSelectors.selectFacts(projection));
      if (detailsOpen) {
        if (["loop", "portfolio", "lineage", "contexts"].some(key => previous?.[key] !== projection[key])) void refreshLineage(projection);
        if (previous?.round !== projection.round) void refreshObservation();
        if (lineageSnapshot && ["contexts", "runs", "task_progress"].some(key => previous?.[key] !== projection[key])) patchGraph();
      }
      if (!projection.round?.state?.observation_id) {
        observation = null; observationIdentity = null; observationVersion++;
        const node = host.querySelector("[data-patrol-observation]");
        if (node.textContent !== "本轮尚未冻结") node.textContent = "本轮尚未冻结";
        host.querySelector("[data-patrol-observation-open]").disabled = true;
      }
      const key = JSON.stringify(projection.interventions);
      if (key !== historyKey) {
        historyKey = key;
        const current = token;
        const version = ++historyToken;
        api.workspaceInputs(workspace.workspace_id).then(page => {
          if (current === token && version === historyToken) inputs.history(page);
        }).catch(failure => { if (current === token) error(failure); });
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
        const receipt = await api.submitWorkspaceInput(bound, row.request);
        inputs.accepted(row.submission_id, receipt);
        if (token === current && receipt.loop_id !== loopId) {
          const binding = await api.workspacePatrol(bound);
          if (token === current) connect(binding?.loop_id || receipt.loop_id);
        }
      } catch (failure) { inputs.failed(row.submission_id, failure); }
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
        if (button.hasAttribute("data-patrol-details")) toggleDetails();
        else if (button.dataset.workbenchShow) showPanel(button.dataset.workbenchShow);
        else if (button.hasAttribute("data-patrol-related")) { relatedOnly = !relatedOnly; patchGraph(); }
        else if (button.hasAttribute("data-patrol-progress-locate")) await locateProgress(JSON.parse(button.dataset.patrolProgressLocate), button);
        else if (button.dataset.patrolEdgeContext) {
          const id = button.dataset.patrolEdgeContext, revisionId = button.dataset.revisionId;
          await closeDialog(); await inspectContext(id, host.querySelector("[data-patrol-related]"), revisionId);
          showPanel("context");
        }
        else if (button.hasAttribute("data-patrol-dialog-close")) void closeDialog();
        else if (button.hasAttribute("data-patrol-history-open")) showDialog("history", "输入记录", "", host.querySelector("[data-patrol-input-history]"));
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
          observationInspection = observation; observationSections = {}; observationLoading = new Set();
          showDialog("observation", "Observation", root.FocusObservationView.render(observationInspection, observationSections));
        }
        else if (button.dataset.patrolObservationSection) await loadObservationSection(button.dataset.patrolObservationSection);
        else if (button.hasAttribute("data-patrol-observation-retry")) await refreshObservation(true);
        else if (button.hasAttribute("data-patrol-lineage-retry") && projection) { previews?.retry(); await refreshLineage(projection); }
        else if (button.hasAttribute("data-patrol-facts-open")) await loadFacts();
        else if (button.hasAttribute("data-patrol-facts-older")) await loadFacts(true);
        else if (button.hasAttribute("data-patrol-fold")) inputs.expand(!inputs.get().expanded);
        else if (button.hasAttribute("data-patrol-older")) { const owner = token; const page = await api.workspaceInputs(workspace.workspace_id, inputs.get().cursor); if (owner === token) inputs.history(page, true); }
        else if (button.dataset.patrolRetry) { const row = inputs.retry(button.dataset.patrolRetry); if (row) void send(row); }
        else if (button.dataset.patrolAnswer) { inputs.edit({ request_id: button.dataset.patrolAnswer, request_revision: Number(button.dataset.requestRevision) }); const owner = token; if (await closeDialog() && owner === token) host.querySelector("textarea").focus(); }
        else if (button.hasAttribute("data-patrol-clear-target")) inputs.edit({ request_id: null, request_revision: null });
        else if (button.hasAttribute("data-patrol-clear-draft")) { inputs.edit({ content: "" }); host.querySelector("[data-patrol-content]").focus(); }
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
          const owner = token;
          const detail = await api.factDetail(loopId, button.dataset.factId, lifetime.signal);
          if (owner === token) showDialog("fact", "事实与来源", root.FocusLoopFactsView.renderDetail(detail));
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
      host.innerHTML = view.skeleton(bound);
      inspector = root.FocusContextInspector.create({ api, onOpenTask, presentation: "workbench", getManifest: manifest, onSelectionChange: selectionChanged });
      inspector.mount(host.querySelector("[data-patrol-context]"));
      unlisten = inputs.subscribe(patchInputs);
      const options = { signal: lifetime.signal };
      host.querySelector("[data-patrol-composer]").addEventListener("submit", event => { event.preventDefault(); try { error(null); void send(inputs.submit()); } catch (failure) { error(failure); } }, options);
      const content = host.querySelector("[data-patrol-content]");
      content.addEventListener("input", event => inputs.edit({ content: event.target.value }), options);
      content.addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); content.form.requestSubmit(); } }, options);
      host.querySelector("[data-patrol-type]").addEventListener("change", event => inputs.edit({ input_type: event.target.value }), options);
      host.addEventListener("click", click, options);
      host.addEventListener("portfolio-clear-selection", () => { highlightIds = []; closeInspection(); }, options);
      host.addEventListener("keydown", event => { if (event.key === "Escape" && !host.querySelector("[data-patrol-dialog]").open) closeInspection(); }, options);
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
      try {
        const [loop, page] = await Promise.all([api.workspacePatrol(bound.workspace_id), api.workspaceInputs(bound.workspace_id)]);
        if (current !== token) return;
        inputs.history(page);
        if (loop) connect(loop.loop_id);
      } catch (failure) { if (current === token) error(failure); }
    }
    function leave() {
      previews?.stop();
      root.document.body.classList.remove("has-patrol-workbench");
      relatedOnly = false; highlightIds = [];
      token++; lineageToken++; historyToken++; observationVersion++; factVersion++; progressVersion++;
      lifetime?.abort(); lifetime = null;
      if (host) for (const element of host.querySelectorAll("[data-surface-managed], [data-patrol-composer]")) root.FocusSurfaceTransition.finish(element);
      host?.querySelector("[data-patrol-dialog]")?.close();
      inspector?.dispose(); inspector = null; observation = null; observationIdentity = null; observationInspection = null;
      movedPanel = null; dialogKind = null; projection = null; detailsOpen = false; detailsScroll = 0; lineageSnapshot = null;
      unlisten?.(); unlisten = null; host = null; workspace = null; loopId = null; lineageKey = null; historyKey = null; connection.stop();
    }
    const unsubscribe = liveStore.subscribe(patchLive);
    return Object.freeze({ mount, leave, isMounted(node, id) { return host === node && workspace?.workspace_id === id && node.contains(node.querySelector("[data-patrol-composer]")); }, dispose() { leave(); unsubscribe(); } });
  }
  return Object.freeze({ create });
});
