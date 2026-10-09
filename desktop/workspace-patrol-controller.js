/* 本文件对外提供工作区 Patrol 页面控制器。
 * 输入为已绑定工作区、共享 Loop API、Live Store/Connection 及输入 store；输出为逐条耐久回执和独立观测更新。
 * 具体工作流为先读取绑定/历史再订阅已有单路 Live，区域更新保留 composer DOM，异步响应按工作区代际隔离；
 * 所有发送均走 workspace intake，查看 Context 不改变目标。示例：await controller.mount(host, workspace)。
 */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusWorkspacePatrolController = api;
})(globalThis, function (root) {
  "use strict";
  function create({ api, inputs, liveStore, connection, view = root.FocusWorkspacePatrolView }) {
    let host = null;
    let workspace = null;
    let loopId = null;
    let token = 0;
    let lineageKey = null;
    let lineageToken = 0;
    let historyKey = null;
    let historyToken = 0;
    let unlisten = null;
    let inspection = null;
    let inspectionToken = 0;
    const error = value => { if (host) host.querySelector("[data-patrol-error]").textContent = value?.message || String(value || ""); };
    const patchInputs = model => {
      if (!host) return;
      host.querySelector("[data-patrol-history]").innerHTML = view.history(model);
      const content = host.querySelector("[data-patrol-content]");
      if (content.value !== model.draft.content) content.value = model.draft.content;
      host.querySelector("[data-patrol-type]").value = model.draft.input_type;
      host.querySelector("[data-patrol-target]").textContent = model.draft.request_id ? "回答指定请求" : "主动输入";
      host.querySelector("[data-patrol-clear-target]").hidden = !model.draft.request_id;
      host.querySelector("[data-patrol-fold]").textContent = model.expanded ? "收起历史" : "展开更早记录";
      host.querySelector("[data-patrol-older]").hidden = !model.expanded || !model.cursor;
    };
    async function refreshLineage(projection) {
      const key = JSON.stringify([projection.portfolio, projection.lineage, Object.values(projection.contexts).map(item => item.state.current_revision_id)]);
      if (key === lineageKey) return;
      lineageKey = key;
      const version = ++lineageToken;
      const current = token;
      try {
        const snapshot = await api.committedLineage(projection.loop_id);
        if (current === token && version === lineageToken && host) host.querySelector("[data-patrol-lineage]").innerHTML = view.lineage(snapshot, projection);
      } catch (failure) { if (current === token) { lineageKey = null; error(failure); } }
    }
    function patchLive({ projection, connection: status }) {
      if (!host || !projection || projection.loop_id !== loopId) return;
      const loop = projection.loop?.state || {};
      const states = { running: "推进中", paused: "已暂停", stopped: "已停止", failed: "执行失败", waiting_user: "等待处理", completed: "已完成当前工作" };
      const connections = { live: "实时更新", syncing: "同步中", connecting: "连接中", reconnecting: "重连中", resyncing: "重新同步", unavailable: "连接不可用" };
      host.querySelector("[data-patrol-state]").textContent = loop.status === "waiting_user" && loop.waiting_reason === "awaiting_input" ? "等待新输入" : `${states[loop.status] || "准备中"} · ${connections[status.status] || "同步中"}`;
      host.querySelector('[data-patrol-control="pause"]').disabled = !(loop.status === "running" || (loop.status === "waiting_user" && loop.waiting_reason === "awaiting_input"));
      host.querySelector('[data-patrol-control="resume"]').disabled = loop.status !== "paused";
      host.querySelector('[data-patrol-control="stop"]').disabled = !["running", "paused", "waiting_user"].includes(loop.status);
      host.querySelector("[data-patrol-restart]").hidden = !["completed", "stopped", "failed"].includes(loop.status);
      host.querySelector("[data-patrol-progress]").innerHTML = view.progress(projection.task_progress);
      host.querySelector("[data-patrol-requests]").innerHTML = view.requests(Object.values(projection.wait_requests).map(item => ({ request_id: item.entity_id, revision: item.revision, ...item.state })));
      const facts = Object.values(projection.facts).map(item => ({ fact_id: item.entity_id, revision: item.revision, ...item.state }));
      root.FocusLoopFactsView?.reconcileRows(host.querySelector("[data-patrol-facts]"), facts);
      void refreshLineage(projection);
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
      loopId = identity;
      void connection.start(identity).catch(error);
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
    async function inspect(contextId, older = false) {
      const version = ++inspectionToken;
      const current = token;
      const options = older && inspection ? { before: inspection.range.start, revisionId: inspection.revision?.revision_id } : {};
      const page = await api.conversation(loopId, contextId, options);
      if (current !== token || version !== inspectionToken) return;
      inspection = { ...page, contextId };
      const panel = host.querySelector("[data-patrol-context]");
      panel.hidden = false; panel.open = true;
      panel.querySelector("[data-patrol-context-content]").innerHTML = root.FocusContextConversationView.renderInspection(page);
    }
    async function click(event) {
      const button = event.target.closest("button");
      if (!button || !host.contains(button)) return;
      if (!button.hasAttribute("data-patrol-switch")) event.stopPropagation();
      try {
        if (button.hasAttribute("data-patrol-fold")) inputs.expand(!inputs.get().expanded);
        else if (button.hasAttribute("data-patrol-older")) inputs.history(await api.workspaceInputs(workspace.workspace_id, inputs.get().cursor), true);
        else if (button.dataset.patrolRetry) { const row = inputs.retry(button.dataset.patrolRetry); if (row) void send(row); }
        else if (button.dataset.patrolAnswer) { inputs.edit({ request_id: button.dataset.patrolAnswer, request_revision: Number(button.dataset.requestRevision) }); host.querySelector("textarea").focus(); }
        else if (button.hasAttribute("data-patrol-clear-target")) inputs.edit({ request_id: null, request_revision: null });
        else if (button.dataset.patrolControl && loopId) await api.control(loopId, button.dataset.patrolControl);
        else if (button.hasAttribute("data-patrol-restart")) { const loop = await api.restartWorkspacePatrol(workspace.workspace_id); connect(loop.loop_id); }
        else if (button.dataset.waitAction) {
          const card = button.closest("[data-wait-request-id]");
          const request = liveStore.get().projection.wait_requests[card?.dataset.waitRequestId];
          const answer = { action: button.dataset.waitAction };
          if (answer.action === "revise_budget") answer.budgets = Object.fromEntries([...card.querySelectorAll('[name^="budget:"]')].map(input => [input.name.slice(7), Number(input.value)]));
          if (request) await api.resolveWait(loopId, request.entity_id, { request_revision: request.revision, idempotency_key: crypto.randomUUID(), answer });
        }
        else if (button.hasAttribute("data-patrol-context-older") && inspection) await inspect(inspection.contextId, true);
        else if (button.dataset.contextId) await inspect(button.dataset.contextId);
      } catch (failure) { error(failure); }
    }
    async function mount(node, bound) {
      leave();
      host = node; workspace = bound; const current = token;
      host.innerHTML = view.skeleton(bound);
      unlisten = inputs.subscribe(patchInputs);
      host.querySelector("[data-patrol-composer]").addEventListener("submit", event => { event.preventDefault(); try { void send(inputs.submit()); } catch (failure) { error(failure); } });
      host.querySelector("[data-patrol-content]").addEventListener("input", event => inputs.edit({ content: event.target.value }));
      host.querySelector("[data-patrol-type]").addEventListener("change", event => inputs.edit({ input_type: event.target.value }));
      host.addEventListener("click", click);
      host.querySelector("[data-patrol-requests]").addEventListener("submit", async event => {
        event.preventDefault(); event.stopPropagation();
        const card = event.target.closest("[data-wait-request-id]");
        const request = liveStore.get().projection.wait_requests[card?.dataset.waitRequestId];
        if (!request) return;
        const values = new FormData(event.target);
        const mode = request.state.response_mode;
        const answer = mode === "text" ? { text: values.get("text") } : mode === "single_choice" ? { choice: values.get("choice") } : mode === "multiple_choice" ? { choices: values.getAll("choice") } : Object.fromEntries(values);
        try { await api.resolveWait(loopId, request.entity_id, { request_revision: request.revision, idempotency_key: crypto.randomUUID(), answer }); } catch (failure) { error(failure); }
      });
      try {
        const [loop, page] = await Promise.all([api.workspacePatrol(bound.workspace_id), api.workspaceInputs(bound.workspace_id)]);
        if (current !== token) return;
        inputs.history(page);
        if (loop) connect(loop.loop_id);
      } catch (failure) { if (current === token) error(failure); }
    }
    function leave() { token++; lineageToken++; historyToken++; inspectionToken++; inspection = null; unlisten?.(); unlisten = null; host?.removeEventListener("click", click); host = null; workspace = null; loopId = null; lineageKey = null; historyKey = null; connection.stop(); }
    const unsubscribe = liveStore.subscribe(patchLive);
    return Object.freeze({ mount, leave, dispose() { leave(); unsubscribe(); } });
  }
  return Object.freeze({ create });
});
