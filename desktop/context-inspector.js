/* 本文件对外提供 Patrol 与全图共用的只读 Context 检查器。
 * 输入为 Loop API、精确 Loop/Context 身份、节点摘要和显式任务打开回调；输出为非模态三页签、会话分页及版本来源。
 * 具体工作流为取消旧读请求并按 owner 隔离迟到响应；检查不改变发送身份，只有显式打开任务调用操作回调。
 * 示例：const inspector = FocusContextInspector.create({ api, onOpenTask }); inspector.mount(panel); await inspector.select(loopId, contextId, node, trigger)。
 */
(function (root) {
  "use strict";
  function create({ api, onOpenTask, view = root.FocusWorkspacePatrolView }) {
    let panel = null, active = null, tab = "overview", returnFocus = null;
    let lifetime = null, request = null, generation = 0;
    function render() {
      if (!panel || !active) return;
      panel.hidden = false;
      panel.querySelector("[data-patrol-context-content]").innerHTML = view.context({ ...active.page, context_id: active.contextId }, active.node, tab);
    }
    function failure(error) {
      if (!panel || error.name === "AbortError") return;
      render();
      const body = panel.querySelector(".patrol-context-body");
      body.insertAdjacentHTML("afterbegin", `<p role="alert">${view.escape(error.message)} <button data-context-inspection-retry>重试读取</button></p>`);
    }
    async function read(older = false, revisionId = null) {
      if (!active || !panel) return;
      request?.abort(); request = new AbortController();
      const owner = ++generation, identity = active;
      try {
        const page = await api.conversation(identity.loopId, identity.contextId, {
          signal: request.signal, revisionId: revisionId || (older ? identity.page?.revision?.revision_id : undefined),
          before: older ? identity.page?.next_before ?? identity.page?.range?.start : undefined,
        });
        if (owner !== generation || active !== identity) return;
        active.page = { ...page, messages: older ? [...page.messages, ...(identity.page?.messages || [])] : page.messages };
        render();
        if (tab === "sources") await sources();
      } catch (error) { if (owner === generation) failure(error); }
    }
    async function sources() {
      if (!active || !panel) return;
      const identity = active, page = active.page, owner = generation;
      try {
        const [versions, snapshot] = await Promise.all([
          api.revisions(identity.contextId, request.signal),
          page.revision?.revision_id ? api.revision(page.revision.revision_id, request.signal) : null,
        ]);
        if (owner !== generation || active !== identity || active.page !== page) return;
        active.page = { ...page, versions, sources: snapshot?.revision?.sources || snapshot?.sources || [] };
        render();
      } catch (error) { if (owner === generation) failure(error); }
    }
    function close() {
      if (!panel || panel.hidden) return;
      generation++; request?.abort(); panel.hidden = true;
      if (returnFocus?.isConnected) returnFocus.focus();
    }
    async function click(event) {
      const button = event.target.closest("button");
      if (!button || !panel.contains(button)) return;
      event.stopPropagation();
      if (button.hasAttribute("data-patrol-context-close")) return close();
      if (button.dataset.patrolContextTab) { tab = button.dataset.patrolContextTab; render(); if (tab === "sources") await sources(); }
      else if (button.hasAttribute("data-patrol-context-older")) await read(true);
      else if (button.dataset.patrolInspectRevision) await read(false, button.dataset.patrolInspectRevision);
      else if (button.hasAttribute("data-context-inspection-retry")) await read(false, active.page?.revision?.revision_id);
      else if (button.dataset.patrolOpenTask) { try { await onOpenTask(button.dataset.patrolOpenTask); } catch (error) { failure(error); } }
    }
    function mount(node) {
      dispose(); panel = node; lifetime = new AbortController();
      panel.addEventListener("click", click, { signal: lifetime.signal });
      panel.addEventListener("keydown", event => { if (event.key === "Escape") { event.stopPropagation(); close(); } }, { signal: lifetime.signal });
    }
    async function select(loopId, contextId, node, trigger) {
      active = { loopId, contextId, node: { context_id: contextId, ...node }, page: {} };
      tab = "overview"; returnFocus = trigger; render();
      panel.querySelector("[data-patrol-context-close]").focus();
      await read();
    }
    function dispose() { generation++; lifetime?.abort(); request?.abort(); panel = null; active = null; returnFocus = null; }
    return Object.freeze({ mount, select, close, dispose });
  }
  root.FocusContextInspector = Object.freeze({ create });
})(globalThis);
