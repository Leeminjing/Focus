/* 本文件对外提供 Patrol 与全图共用的只读 Context 检查器。
 * 输入为 Loop API、精确 Loop/Context 身份、节点摘要和显式任务打开回调；输出为非模态三页签或工作台摘要、只读 selection 和变更通知；manifest 回调仅提供已返回关系。
 * 具体工作流为取消旧读请求并按 owner 隔离迟到响应，保留表面与阅读位置、可逆退出后归还焦点；检查不改变发送身份，只有显式打开任务调用操作回调。
 * 示例：const inspector = FocusContextInspector.create({ api, onOpenTask }); inspector.mount(panel); await inspector.select(loopId, contextId, node, trigger)。
 */
(function (root) {
  "use strict";
  function create({ api, onOpenTask, view = root.FocusWorkspacePatrolView, presentation = "default", getManifest = () => null, onSelectionChange = () => {} }) {
    let panel = null, active = null, tab = "overview", returnFocus = null;
    let lifetime = null, request = null, generation = 0;
    const selection = () => active ? Object.freeze({ contextId: active.contextId, revisionId: active.page?.revision?.revision_id || active.revisionId }) : null;
    function render() {
      if (!panel || !active) return;
      const content = panel.querySelector("[data-patrol-context-content]");
      const focused = root.document.activeElement;
      const selector = panel.contains(focused) ? [...focused.attributes].filter(item => item.name.startsWith("data-")).map(item => `[${item.name}="${root.CSS.escape(item.value)}"]`).join("") : "";
      const scroll = panel.scrollTop, bodyScroll = panel.querySelector(".patrol-context-body")?.scrollTop || 0;
      content.innerHTML = view.context({ ...active.page, context_id: active.contextId }, active.node, tab, { presentation, manifest: getManifest(), revisionId: active.revisionId });
      panel.classList.toggle("is-expanded", presentation === "workbench" && tab !== "overview");
      if (active.readError) content.querySelector(".patrol-context-body")?.insertAdjacentHTML("afterbegin", `<p role="alert">完整会话不可用：${view.escape(active.readError)} <button data-context-inspection-retry>重试读取</button></p>`);
      panel.scrollTop = scroll;
      const body = panel.querySelector(".patrol-context-body");
      if (body) body.scrollTop = bodyScroll;
      if (selector) content.querySelector(selector)?.focus({ preventScroll: true });
    }
    function failure(error) {
      if (!panel || error.name === "AbortError") return;
      active.readError = error.message;
      render();
    }
    async function read(older = false, revisionId = null) {
      if (!active || !panel) return;
      request?.abort(); request = new AbortController();
      const owner = ++generation, identity = active;
      if (revisionId) identity.revisionId = revisionId;
      identity.readError = null;
      try {
        const page = await api.conversation(identity.loopId, identity.contextId, {
          signal: request.signal, revisionId: revisionId || identity.page?.revision?.revision_id || identity.revisionId,
          before: older ? identity.page?.next_before ?? identity.page?.range?.start : undefined,
        });
        if (owner !== generation || active !== identity) return;
        const scroller = panel.querySelector(".patrol-context-body") || panel;
        const scroll = scroller.scrollTop, height = scroller.scrollHeight;
        active.page = { ...page, messages: older ? [...page.messages, ...(identity.page?.messages || [])] : page.messages };
        active.revisionId = page.revision?.revision_id || identity.revisionId;
        render(); onSelectionChange(selection());
        if (older) {
          const scroller = panel.querySelector(".patrol-context-body") || panel;
          scroller.scrollTop = scroll + scroller.scrollHeight - height;
        }
        if (tab === "sources") await sources();
      } catch (error) { if (owner === generation) failure(error); }
    }
    async function sources() {
      if (!active || !panel || active.readError) return;
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
      generation++; request?.abort(); active = null;
      if (presentation === "workbench") { panel.classList.remove("is-expanded"); panel.querySelector("[data-patrol-context-content]").innerHTML = view.contextEmpty(); }
      else void root.FocusSurfaceTransition.visible(panel, false, { axis: "x", distance: 40 });
      onSelectionChange(null);
      if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
      else root.document.querySelector("[data-patrol-content], #app")?.focus({ preventScroll: true });
    }
    async function click(event) {
      const button = event.target.closest("button");
      if (!button || !panel.contains(button)) return;
      event.stopPropagation();
      if (button.hasAttribute("data-patrol-context-close")) return close();
      if (button.dataset.patrolContextTab) { tab = button.dataset.patrolContextTab; render(); if (tab === "sources") await sources(); }
      else if (button.dataset.contextRelated) {
        const node = getManifest()?.nodes.find(node => node.context_id === button.dataset.contextRelated);
        await select(active.loopId, button.dataset.contextRelated, node, button, button.dataset.revisionId);
      }
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
    async function select(loopId, contextId, node, trigger, revisionId = null) {
      active = { loopId, contextId, revisionId, node: { context_id: contextId, ...node }, page: {} };
      tab = "overview"; returnFocus = trigger; render(); onSelectionChange(selection());
      if (panel.hidden || panel.inert) void root.FocusSurfaceTransition.visible(panel, true, { axis: "x", distance: 40 });
      panel.querySelector("[data-patrol-context-close]").focus({ preventScroll: true });
      await read(false, revisionId);
    }
    function dispose() { generation++; lifetime?.abort(); request?.abort(); if (panel) root.FocusSurfaceTransition.finish(panel); panel = null; active = null; returnFocus = null; }
    return Object.freeze({ mount, select, close, dispose, selection, refresh: render });
  }
  root.FocusContextInspector = Object.freeze({ create });
})(globalThis);
