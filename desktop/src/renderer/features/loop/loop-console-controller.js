/*
 * 本文件对外提供 Agent Loop 控制台的请求与交互控制器。
 * 输入为 Loop API、Console Store 与重绘回调；输出为加载拓扑、切换或强制刷新 Context、双向分页、视口恢复、事实筛选、可调布局和三类介入命令。
 * 具体工作流为切换前保存当前 Context 视口，通常优先恢复 revision 缓存，来源恢复后可强制读取新 revision；事实验证状态和业务结果分开过滤，查询、通知与拖拽分别按游标和动画帧协调。
 * 示例：`controller.load(loopId)` 后由 Store 驱动纯视图渲染。
 * 宿主可向 load 传入 isCurrent 导航守卫；拓扑、会话、事实及完成/错误写入共同遵守该身份。
 * 直接提交复用在途 Promise；失败重试同一目标原文保留 request identity，成功刷新后释放，避免重复交付。
 * 所有控制台读取按 Loop 身份和加载代次隔离；切换或销毁立即失效旧请求，迟到结果、错误和完成回调均不得写入当前状态。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopConsoleController = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function create({ api, store, onChange = () => {} }) {
    let loopId = null;
    let conversationAbort = null;
    let factsAbort = null;
    let loopGeneration = 0;
    let frame = null;
    let mapRatio = 0.52;
    let compactMapCollapsed = false;
    let compactMapViewport = null;
    let resizeCleanup = null;
    let paginationRequest = null;
    let factsLoading = false;
    let factsRevision = 0;
    let boundContainer = null;
    let directRequest = null;
    let submission = null;
    const requestIdentity = () => ({ loopId, generation: loopGeneration });
    const isCurrent = request => request.loopId === loopId && request.generation === loopGeneration;
    function invalidateRequests() {
      loopGeneration += 1;
      conversationAbort?.abort();
      factsAbort?.abort();
      factsRevision += 1;
      paginationRequest = null;
      factsLoading = false;
    }
    const schedule = () => {
      if (frame !== null) return;
      frame = requestAnimationFrame(() => { frame = null; onChange(store.get()); });
    };
    const unsubscribe = store.subscribe(schedule);

    async function load(nextLoopId, { isCurrent: isHostCurrent = () => true } = {}) {
      if (!isHostCurrent()) return;
      const switched = loopId !== nextLoopId;
      invalidateRequests();
      loopId = nextLoopId;
      const request = requestIdentity();
      const isActive = () => isCurrent(request) && isHostCurrent();
      if (switched) store.reset();
      store.begin("console");
      try {
        const manifest = await api.console(request.loopId);
        if (!isActive()) return;
        store.loadManifest(manifest);
        await selectContext(store.get().selectedContextId, { isCurrent: isActive });
        if (isActive()) store.complete();
      } catch (error) {
        if (isActive() && error?.name !== "AbortError") store.fail(error);
      }
    }

    async function refresh() {
      if (!loopId) return;
      const request = requestIdentity();
      try {
        const manifest = await api.console(request.loopId);
        if (!isCurrent(request)) return;
        store.loadManifest(manifest);
        const selected = store.get().selectedContextId;
        await Promise.all([
          selected ? selectContext(selected, { preserveSelection: true }) : Promise.resolve(),
          store.get().factScope === "all" ? loadFacts() : Promise.resolve(),
        ]);
      } catch (error) {
        if (isCurrent(request) && error?.name !== "AbortError") store.fail(error);
      }
    }

    async function selectContext(contextId, options = {}) {
      const isHostCurrent = options.isCurrent || (() => true);
      if (!loopId || !contextId || !isHostCurrent()) return;
      const request = requestIdentity();
      if (!options.preserveSelection) void globalThis.focusContextBrowser?.select(contextId);
      captureViewport();
      conversationAbort?.abort();
      factsAbort?.abort();
      conversationAbort = new AbortController();
      const signal = conversationAbort.signal;
      const isSelected = () => isCurrent(request) && isHostCurrent() && !signal.aborted && store.get().selectedContextId === contextId;
      paginationRequest = null;
      if (!options.preserveSelection) store.selectContext(contextId);
      const cached = store.get().conversation;
      if (cached?.context_id === contextId && !options.force) {
        if (store.get().factScope === "current") await loadFacts({ isCurrent: isSelected });
        return;
      }
      try {
        const page = await api.conversation(request.loopId, contextId, { signal });
        if (isSelected()) {
          store.loadConversation(page);
          if (store.get().factScope === "current") await loadFacts({ isCurrent: isSelected });
        }
      } catch (error) {
        if (isSelected() && error?.name !== "AbortError") store.fail(error);
      }
    }

    async function loadOlder() {
      const conversation = store.get().conversation;
      if (!loopId || paginationRequest || !conversation?.has_more || conversation.next_before == null) return;
      const request = requestIdentity();
      const signal = conversationAbort?.signal;
      const isActive = () => isCurrent(request) && !signal?.aborted;
      paginationRequest = request;
      store.begin("older-messages");
      try {
        const page = await api.conversation(request.loopId, conversation.context_id, {
          before: conversation.next_before,
          signal,
        });
        if (!isActive()) return;
        const current = store.get();
        if (current.selectedContextId === conversation.context_id && current.conversation?.revision?.revision_id === conversation.revision?.revision_id) {
          store.mergeConversation(page, "older");
        }
        store.complete();
      } catch (error) {
        if (isActive() && error?.name !== "AbortError") store.fail(error);
      } finally {
        if (paginationRequest === request) paginationRequest = null;
      }
    }

    async function loadNewer() {
      const conversation = store.get().conversation;
      if (!loopId || paginationRequest || !conversation?.has_newer || conversation.next_after == null) return;
      const request = requestIdentity();
      const signal = conversationAbort?.signal;
      const isActive = () => isCurrent(request) && !signal?.aborted;
      paginationRequest = request;
      store.begin("newer-messages");
      try {
        const pageSize = Math.max(24, conversation.range.end - conversation.range.start);
        const page = await api.conversation(request.loopId, conversation.context_id, {
          before: Math.min(conversation.total, conversation.next_after + pageSize),
          limit: pageSize,
          signal,
        });
        if (!isActive()) return;
        const current = store.get();
        if (current.selectedContextId === conversation.context_id && current.conversation?.revision?.revision_id === conversation.revision?.revision_id) {
          store.mergeConversation(page, "newer");
        }
        store.complete();
      } catch (error) {
        if (isActive() && error?.name !== "AbortError") store.fail(error);
      } finally {
        if (paginationRequest === request) paginationRequest = null;
      }
    }

    async function loadFacts(options = {}) {
      if (!loopId || (options.prepend && factsLoading)) return;
      const request = requestIdentity();
      const requestRevision = ++factsRevision;
      factsLoading = true;
      factsAbort?.abort();
      factsAbort = new AbortController();
      const signal = factsAbort.signal;
      const isActive = () => isCurrent(request) && requestRevision === factsRevision && !signal.aborted
        && (!options.isCurrent || options.isCurrent());
      const state = store.get();
      const contextId = state.factScope === "all" ? null : state.selectedContextId;
      try {
        const facts = await api.facts(request.loopId, {
          contextId,
          kind: state.factFilter === "all" ? null : state.factFilter,
          status: ["all", "failed", "unknown", "success"].includes(state.factStatus) ? null : state.factStatus,
          outcomeStatus: ["failed", "unknown", "success"].includes(state.factStatus) ? state.factStatus : null,
          before: options.before,
          limit: options.limit || 20,
          signal,
        });
        const current = store.get();
        const currentContextId = current.factScope === "all" ? null : current.selectedContextId;
        if (!isActive() || currentContextId !== contextId || current.authorityRevision !== state.authorityRevision) return;
        if (options.prepend) store.prependFacts(facts);
        else store.loadFacts(facts);
        return true;
      } catch (error) {
        if (isActive()) throw error;
      } finally {
        if (isCurrent(request) && requestRevision === factsRevision) factsLoading = false;
      }
    }

    async function loadOlderFacts() {
      const facts = store.get().facts;
      if (!facts?.has_more || facts.next_before == null) return;
      const request = requestIdentity();
      store.begin("older-facts");
      try {
        const loaded = await loadFacts({ before: facts.next_before, prepend: true });
        if (isCurrent(request) && loaded) store.complete();
      } catch (error) {
        if (isCurrent(request) && error?.name !== "AbortError") store.fail(error);
      }
    }

    async function setFactScope(scope) {
      store.setFactScope(scope);
      return reloadFacts();
    }

    async function setFactFilter(filter) {
      store.setFactFilter(filter);
      return reloadFacts();
    }

    async function setFactStatus(status) {
      store.setFactStatus(status);
      return reloadFacts();
    }

    async function reloadFacts() {
      const request = requestIdentity();
      factsAbort?.abort();
      store.begin("facts");
      try {
        const loaded = await loadFacts();
        if (isCurrent(request) && loaded) store.complete();
      } catch (error) {
        if (isCurrent(request) && error?.name !== "AbortError") store.fail(error);
      }
    }

    async function submit(content) {
      if (submission) return submission;
      submission = submitIntent(content);
      try {
        return await submission;
      } finally {
        submission = null;
      }
    }

    async function submitIntent(content) {
      const state = store.get();
      const contextId = state.selectedContextId;
      const text = String(content || "");
      if (!text.trim() || !loopId || !contextId) return;
      store.begin("intervention");
      try {
        if (state.interventionMode === "direct_context_message") {
          const signature = JSON.stringify([loopId, contextId, text]);
          if (directRequest?.signature !== signature) directRequest = { signature, id: crypto.randomUUID() };
          await api.directMessage(contextId, text, directRequest.id);
        } else {
          await api.intervene(loopId, {
            mode: state.interventionMode,
            content: text,
            context_id: state.interventionMode === "patrol_context_intent" ? contextId : null,
          });
        }
        store.complete();
        await refresh();
        directRequest = null;
      } catch (error) {
        store.fail(error);
        throw error;
      }
    }

    function bind(container) {
      resizeCleanup?.();
      boundContainer = container || null;
      const layout = container?.querySelector?.(".loop-console-main");
      const handle = container?.querySelector?.("[data-loop-console-resizer]");
      if (!layout || !handle) return;
      const mapToggle = container.querySelector("[data-loop-console-map-toggle]");
      const applyMapVisibility = () => {
        layout.classList?.toggle("is-map-collapsed", compactMapCollapsed);
        if (!mapToggle) return;
        mapToggle.setAttribute("aria-expanded", String(!compactMapCollapsed));
        const label = compactMapCollapsed ? "展开图谱与事实" : "收起图谱与事实";
        mapToggle.textContent = globalThis.FocusI18n?.text ? globalThis.FocusI18n.text(label) : label;
        if (!compactMapCollapsed && compactMapViewport) {
          const scroll = container.querySelector(".portfolio-map-scroll");
          if (scroll) { scroll.scrollTop = compactMapViewport.top; scroll.scrollLeft = compactMapViewport.left; }
          compactMapViewport = null;
        }
      };
      const onMapToggle = () => {
        const scroll = container.querySelector(".portfolio-map-scroll");
        if (!compactMapCollapsed && scroll) compactMapViewport = { top: scroll.scrollTop, left: scroll.scrollLeft };
        compactMapCollapsed = !compactMapCollapsed;
        applyMapVisibility();
      };
      mapToggle?.addEventListener("click", onMapToggle);
      applyMapVisibility();
      let historyObserver = null;
      let newerObserver = null;
      let factsObserver = null;
      const applyRatio = value => {
        mapRatio = Math.min(0.68, Math.max(0.32, value));
        const percent = Math.round(mapRatio * 1000) / 10;
        layout.style.setProperty("--loop-map-ratio", `${percent}fr`);
        layout.style.setProperty("--loop-conversation-ratio", `${100 - percent}fr`);
        handle.setAttribute("aria-valuenow", String(Math.round(percent)));
      };
      applyRatio(mapRatio);
      const onPointerMove = event => {
        if (!handle.hasPointerCapture(event.pointerId)) return;
        const bounds = layout.getBoundingClientRect();
        applyRatio((event.clientX - bounds.left) / Math.max(bounds.width, 1));
      };
      const onPointerUp = event => {
        if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
      };
      const onPointerDown = event => {
        handle.setPointerCapture(event.pointerId);
        onPointerMove(event);
      };
      const onKeyDown = event => {
        if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
        event.preventDefault();
        applyRatio(mapRatio + (event.key === 'ArrowLeft' ? -0.03 : 0.03));
      };
      handle.addEventListener("pointerdown", onPointerDown);
      handle.addEventListener("pointermove", onPointerMove);
      handle.addEventListener("pointerup", onPointerUp);
      handle.addEventListener("pointercancel", onPointerUp);
      handle.addEventListener("keydown", onKeyDown);
      if (typeof IntersectionObserver === "function") {
        const transcript = container.querySelector("[data-loop-transcript]");
        const historySentinel = transcript?.querySelector("[data-loop-history-sentinel]");
        const newerSentinel = transcript?.querySelector("[data-loop-newer-sentinel]");
        if (transcript && historySentinel) {
          historyObserver = new IntersectionObserver(entries => {
            if (entries.some(entry => entry.isIntersecting)) void loadOlder();
          }, { root: transcript, rootMargin: "240px 0px 0px", threshold: 0.01 });
          historyObserver.observe(historySentinel);
        }
        if (transcript && newerSentinel) {
          newerObserver = new IntersectionObserver(entries => {
            if (entries.some(entry => entry.isIntersecting)) void loadNewer();
          }, { root: transcript, rootMargin: "0px 0px 240px", threshold: 0.01 });
          newerObserver.observe(newerSentinel);
        }
        const factList = container.querySelector("[data-loop-fact-list]");
        const factSentinel = factList?.querySelector("[data-loop-fact-sentinel]");
        if (factList && factSentinel) {
          factsObserver = new IntersectionObserver(entries => {
            if (entries.some(entry => entry.isIntersecting)) void loadOlderFacts();
          }, { root: factList, rootMargin: "0px 0px 0px 240px", threshold: 0.01 });
          factsObserver.observe(factSentinel);
        }
      }
      resizeCleanup = () => {
        historyObserver?.disconnect();
        newerObserver?.disconnect();
        factsObserver?.disconnect();
        handle.removeEventListener("pointerdown", onPointerDown);
        handle.removeEventListener("pointermove", onPointerMove);
        handle.removeEventListener("pointerup", onPointerUp);
        handle.removeEventListener("pointercancel", onPointerUp);
        handle.removeEventListener("keydown", onKeyDown);
        mapToggle?.removeEventListener("click", onMapToggle);
      };
    }

    function captureViewport() {
      const transcript = boundContainer?.querySelector?.("[data-loop-transcript]");
      const contextId = transcript?.dataset?.contextId || store.get().selectedContextId;
      if (transcript && contextId) store.saveViewport(contextId, { scrollTop: transcript.scrollTop });
    }

    function destroy() {
      invalidateRequests();
      loopId = null;
      if (frame !== null) cancelAnimationFrame(frame);
      resizeCleanup?.();
      boundContainer = null;
      unsubscribe();
    }

    return Object.freeze({ load, refresh, selectContext, loadOlder, loadNewer, loadOlderFacts, reloadFacts, setFactScope, setFactFilter, setFactStatus, submit, bind, destroy });
  }

  return Object.freeze({ create });
});
