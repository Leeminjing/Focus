/*
 * 本文件对外提供 Loop Control Console 的组合视图和分区增量补丁函数。
 * 输入为 Console Store 快照；输出为左侧 Context Portfolio、右侧完整会话和底部事实抽屉的统一布局。
 * 具体工作流为只组合专用视图，不发请求、不持有领域状态，并按拓扑、会话、事实签名独立补丁且恢复滚动与输入焦点。
 * 示例：首次调用 `FocusLoopConsoleView.render(state)`，后续调用 `patch(container, state)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopConsoleView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const signatures = new WeakMap();
  function render(state) {
    if (!state?.manifest) return '<section class="loop-console-loading">正在建立 Context Portfolio…</section>';
    const map = globalThis.FocusPortfolioMapView?.render(state.manifest, state.selectedContextId) || "";
    const conversation = globalThis.FocusContextConversationView?.render(state) || "";
    const facts = globalThis.FocusLoopFactsView?.render(state) || "";
    return `<section class="loop-console"><div class="loop-console-main"><div class="loop-console-map">${map}</div><div class="loop-console-divider" data-loop-console-resizer role="separator" tabindex="0" aria-orientation="vertical" aria-label="调整 Context 图与完整会话宽度" aria-valuemin="32" aria-valuemax="68" aria-valuenow="52"></div><div class="loop-console-conversation">${conversation}</div></div>${facts}${state.error ? `<p class="loop-error" role="alert">${escape(state.error)}</p>` : ""}</section>`;
  }

  function patch(container, state) {
    const consoleNode = container?.querySelector?.(".loop-console");
    if (!consoleNode || !state?.manifest) return false;
    patchMap(consoleNode, state);
    patchConversation(consoleNode, state);
    patchFacts(consoleNode, state);
    patchError(consoleNode, state.error);
    return true;
  }

  function patchMap(consoleNode, state) {
    const host = consoleNode.querySelector(".loop-console-map");
    const signature = JSON.stringify({ manifest: state.manifest, selected: state.selectedContextId });
    if (!host || signatures.get(host) === signature) return;
    const scroll = host.querySelector(".portfolio-map-scroll");
    const viewport = { top: scroll?.scrollTop || 0, left: scroll?.scrollLeft || 0 };
    host.innerHTML = globalThis.FocusPortfolioMapView?.render(state.manifest, state.selectedContextId) || "";
    const next = host.querySelector(".portfolio-map-scroll");
    if (next) {
      next.scrollTop = viewport.top;
      next.scrollLeft = viewport.left;
    }
    signatures.set(host, signature);
  }

  function patchConversation(consoleNode, state) {
    const host = consoleNode.querySelector(".loop-console-conversation");
    const signature = JSON.stringify({
      context: state.selectedContextId,
      conversation: state.conversation,
      mode: state.interventionMode,
      filter: state.messageFilter,
      search: state.messageSearch,
      pending: state.pending,
    });
    if (!host || signatures.get(host) === signature) return;
    const transcript = host.querySelector("[data-loop-transcript]");
    const search = host.querySelector("[data-loop-message-search]");
    const composer = host.querySelector("#loopInterventionForm textarea");
    const focused = document.activeElement === search ? "search" : document.activeElement === composer ? "composer" : null;
    const viewport = {
      top: transcript?.scrollTop || 0,
      height: transcript?.scrollHeight || 0,
      start: Number(transcript?.dataset.rangeStart || 0),
      nearBottom: transcript ? transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 48 : true,
      search: search?.value || "",
      composer: composer?.value || "",
      focused,
      selectionStart: focused ? document.activeElement.selectionStart : null,
      selectionEnd: focused ? document.activeElement.selectionEnd : null,
    };
    host.innerHTML = globalThis.FocusContextConversationView?.render(state) || "";
    const nextTranscript = host.querySelector("[data-loop-transcript]");
    const nextSearch = host.querySelector("[data-loop-message-search]");
    const nextComposer = host.querySelector("#loopInterventionForm textarea");
    if (nextSearch && viewport.search) nextSearch.value = viewport.search;
    if (nextComposer && viewport.composer) nextComposer.value = viewport.composer;
    if (nextTranscript) {
      const prepended = Number(nextTranscript.dataset.rangeStart || 0) < viewport.start;
      nextTranscript.scrollTop = viewport.nearBottom
        ? nextTranscript.scrollHeight
        : viewport.top + (prepended ? Math.max(0, nextTranscript.scrollHeight - viewport.height) : 0);
    }
    const nextFocused = viewport.focused === "search" ? nextSearch : viewport.focused === "composer" ? nextComposer : null;
    if (nextFocused) {
      nextFocused.focus({ preventScroll: true });
      nextFocused.setSelectionRange(viewport.selectionStart, viewport.selectionEnd);
    }
    signatures.set(host, signature);
  }

  function patchFacts(consoleNode, state) {
    const current = consoleNode.querySelector(".loop-facts-drawer");
    const signature = JSON.stringify({ facts: state.facts, filter: state.factFilter, status: state.factStatus, scope: state.factScope, context: state.selectedContextId });
    if (!current || signatures.get(current) === signature) return;
    const list = current.querySelector("[data-loop-fact-list]");
    const viewport = { left: list?.scrollLeft || 0, width: list?.scrollWidth || 0 };
    const template = document.createElement("template");
    template.innerHTML = globalThis.FocusLoopFactsView?.render(state) || "";
    const replacement = template.content.firstElementChild;
    if (!replacement) return;
    current.replaceWith(replacement);
    const nextList = replacement.querySelector("[data-loop-fact-list]");
    if (nextList) nextList.scrollLeft = viewport.left + Math.max(0, nextList.scrollWidth - viewport.width);
    signatures.set(replacement, signature);
  }

  function patchError(consoleNode, error) {
    const current = consoleNode.querySelector(":scope > .loop-error");
    if (!error) {
      current?.remove();
      return;
    }
    if (current) current.textContent = String(error);
    else consoleNode.insertAdjacentHTML("beforeend", `<p class="loop-error" role="alert">${escape(error)}</p>`);
  }

  return Object.freeze({ render, patch });
});
