/*
 * 本文件对外提供 Agent Loop 控制台的请求与交互控制器。
 * 输入为 Loop API、Console Store 与重绘回调；输出为加载拓扑、切换 Context、分页、事实筛选、可调布局和三类介入命令。
 * 具体工作流为 Context 切换时取消旧会话请求，事实按作用域、类型和异常状态从服务端游标加载，通知与拖拽通过动画帧合并。
 * 示例：`controller.load(loopId)` 后由 Store 驱动纯视图渲染。
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
    let loadRevision = 0;
    let frame = null;
    let mapRatio = 0.52;
    let resizeCleanup = null;
    let olderLoading = false;
    let factsLoading = false;
    let factsRevision = 0;
    const schedule = () => {
      if (frame !== null) return;
      frame = requestAnimationFrame(() => { frame = null; onChange(store.get()); });
    };
    const unsubscribe = store.subscribe(schedule);

    async function load(nextLoopId) {
      loopId = nextLoopId;
      const revision = ++loadRevision;
      store.begin("console");
      try {
        const manifest = await api.console(loopId);
        if (revision !== loadRevision) return;
        store.loadManifest(manifest);
        await selectContext(store.get().selectedContextId);
        store.complete();
      } catch (error) {
        if (error?.name !== "AbortError") store.fail(error);
      }
    }

    async function refresh() {
      if (!loopId) return;
      try {
        const manifest = await api.console(loopId);
        store.loadManifest(manifest);
        const selected = store.get().selectedContextId;
        await Promise.all([
          selected ? selectContext(selected, { preserveSelection: true }) : Promise.resolve(),
          store.get().factScope === "all" ? loadFacts() : Promise.resolve(),
        ]);
      } catch (error) {
        store.fail(error);
      }
    }

    async function selectContext(contextId, options = {}) {
      if (!loopId || !contextId) return;
      conversationAbort?.abort();
      factsAbort?.abort();
      conversationAbort = new AbortController();
      if (!options.preserveSelection) store.selectContext(contextId);
      try {
        const page = await api.conversation(loopId, contextId, { signal: conversationAbort.signal });
        if (store.get().selectedContextId === contextId) {
          store.loadConversation(page);
          if (store.get().factScope === "current") await loadFacts();
        }
      } catch (error) {
        if (error?.name !== "AbortError") store.fail(error);
      }
    }

    async function loadOlder() {
      const conversation = store.get().conversation;
      if (!loopId || olderLoading || !conversation?.has_more || conversation.next_before == null) return;
      olderLoading = true;
      store.begin("older-messages");
      try {
        const page = await api.conversation(loopId, conversation.context_id, {
          before: conversation.next_before,
          signal: conversationAbort?.signal,
        });
        const current = store.get();
        if (current.selectedContextId === conversation.context_id && current.conversation?.revision?.revision_id === conversation.revision?.revision_id) {
          store.prependConversation(page);
        }
        store.complete();
      } catch (error) {
        if (error?.name !== "AbortError") store.fail(error);
      } finally {
        olderLoading = false;
      }
    }

    async function loadFacts(options = {}) {
      if (!loopId || (options.prepend && factsLoading)) return;
      const requestRevision = ++factsRevision;
      factsLoading = true;
      factsAbort?.abort();
      factsAbort = new AbortController();
      const state = store.get();
      const contextId = state.factScope === "all" ? null : state.selectedContextId;
      try {
        const facts = await api.facts(loopId, {
          contextId,
          kind: state.factFilter === "all" ? null : state.factFilter,
          status: state.factStatus === "all" ? null : state.factStatus,
          before: options.before,
          limit: options.limit || 20,
          signal: factsAbort.signal,
        });
        const current = store.get();
        const currentContextId = current.factScope === "all" ? null : current.selectedContextId;
        if (currentContextId !== contextId) return;
        if (options.prepend) store.prependFacts(facts);
        else store.loadFacts(facts);
      } finally {
        if (requestRevision === factsRevision) factsLoading = false;
      }
    }

    async function loadOlderFacts() {
      const facts = store.get().facts;
      if (!facts?.has_more || facts.next_before == null) return;
      store.begin("older-facts");
      try {
        await loadFacts({ before: facts.next_before, prepend: true });
        store.complete();
      } catch (error) {
        if (error?.name !== "AbortError") store.fail(error);
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
      factsAbort?.abort();
      store.begin("facts");
      try {
        await loadFacts();
        store.complete();
      } catch (error) {
        if (error?.name !== "AbortError") store.fail(error);
      }
    }

    async function submit(content) {
      const state = store.get();
      const contextId = state.selectedContextId;
      const text = String(content || "").trim();
      if (!text || !loopId || !contextId) return;
      store.begin("intervention");
      try {
        if (state.interventionMode === "direct_context_message") {
          await api.directMessage(contextId, text);
        } else {
          await api.intervene(loopId, {
            mode: state.interventionMode,
            content: text,
            context_id: state.interventionMode === "patrol_context_intent" ? contextId : null,
          });
        }
        store.complete();
        await refresh();
      } catch (error) {
        store.fail(error);
        throw error;
      }
    }

    function bind(container) {
      resizeCleanup?.();
      const layout = container?.querySelector?.(".loop-console-main");
      const handle = container?.querySelector?.("[data-loop-console-resizer]");
      if (!layout || !handle) return;
      let resizeFrame = null;
      let historyObserver = null;
      let factsObserver = null;
      let resizeObserver = null;
      const applyRatio = value => {
        mapRatio = Math.min(0.68, Math.max(0.32, value));
        const percent = Math.round(mapRatio * 1000) / 10;
        if (globalThis.innerWidth <= 1100) {
          layout.style.removeProperty("grid-template-columns");
          return;
        }
        layout.style.gridTemplateColumns = `${percent}% 5px calc(${100 - percent}% - 5px)`;
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
      if (typeof ResizeObserver === "function") {
        let observedWidth = 0;
        resizeObserver = new ResizeObserver(entries => {
          const width = entries[0]?.contentRect?.width || 0;
          if (Math.abs(width - observedWidth) < 1) return;
          observedWidth = width;
          if (resizeFrame !== null) cancelAnimationFrame(resizeFrame);
          resizeFrame = requestAnimationFrame(() => {
            resizeFrame = null;
            applyRatio(mapRatio);
          });
        });
        resizeObserver.observe(layout);
      }
      if (typeof IntersectionObserver === "function") {
        const transcript = container.querySelector("[data-loop-transcript]");
        const historySentinel = transcript?.querySelector("[data-loop-history-sentinel]");
        if (transcript && historySentinel) {
          historyObserver = new IntersectionObserver(entries => {
            if (entries.some(entry => entry.isIntersecting)) void loadOlder();
          }, { root: transcript, rootMargin: "240px 0px 0px", threshold: 0.01 });
          historyObserver.observe(historySentinel);
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
        factsObserver?.disconnect();
        resizeObserver?.disconnect();
        if (resizeFrame !== null) cancelAnimationFrame(resizeFrame);
        handle.removeEventListener("pointerdown", onPointerDown);
        handle.removeEventListener("pointermove", onPointerMove);
        handle.removeEventListener("pointerup", onPointerUp);
        handle.removeEventListener("pointercancel", onPointerUp);
        handle.removeEventListener("keydown", onKeyDown);
      };
    }

    function destroy() {
      loadRevision += 1;
      conversationAbort?.abort();
      factsAbort?.abort();
      if (frame !== null) cancelAnimationFrame(frame);
      resizeCleanup?.();
      unsubscribe();
    }

    return Object.freeze({ load, refresh, selectContext, loadOlder, loadOlderFacts, setFactScope, setFactFilter, setFactStatus, submit, bind, destroy });
  }

  return Object.freeze({ create });
});
