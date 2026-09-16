/*
 * 本文件对外提供 Agent Loop 控制台的独立客户端 Store。
 * 输入为 Portfolio manifest、选中 Context 的分页会话、事实页、类型/异常筛选与介入状态；输出为不可变快照和订阅通知。
 * 具体工作流为分别归并轻量拓扑、向前追加历史消息并维护选择，不与 Loop 生命周期 Store 混合。
 * 示例：`const store = FocusLoopConsoleStore.create(); store.loadManifest(payload)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopConsoleStore = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const initial = () => Object.freeze({
    manifest: null,
    selectedContextId: null,
    conversation: null,
    facts: null,
    interventionMode: "direct_context_message",
    messageFilter: "all",
    messageSearch: "",
    factFilter: "all",
    factStatus: "all",
    factScope: "current",
    pending: null,
    error: null,
  });

  function create() {
    let current = initial();
    const listeners = new Set();
    function publish(patch) {
      current = Object.freeze({ ...current, ...patch });
      listeners.forEach(listener => listener(current));
      return current;
    }
    return Object.freeze({
      get: () => current,
      subscribe(listener) { listeners.add(listener); listener(current); return () => listeners.delete(listener); },
      reset() { return publish(initial()); },
      loadManifest(manifest) {
        const ids = new Set((manifest?.nodes || []).map(node => node.context_id));
        const selectedContextId = ids.has(current.selectedContextId)
          ? current.selectedContextId
          : manifest?.initial_context_id || manifest?.nodes?.[0]?.context_id || null;
        return publish({ manifest, selectedContextId, error: null });
      },
      selectContext(contextId) {
        return publish({ selectedContextId: contextId, conversation: null, error: null });
      },
      loadConversation(conversation) { return publish({ conversation, error: null }); },
      prependConversation(page) {
        if (!current.conversation || current.conversation.context_id !== page.context_id) {
          return current;
        }
        const existing = new Set(current.conversation.messages.map(item => item.index));
        const messages = [...page.messages.filter(item => !existing.has(item.index)), ...current.conversation.messages];
        return publish({ conversation: { ...current.conversation, ...page, messages, range: { start: page.range.start, end: current.conversation.range.end } }, error: null });
      },
      loadFacts(facts) { return publish({ facts, error: null }); },
      prependFacts(page) {
        if (!current.facts) return publish({ facts: page, error: null });
        const existing = new Set(current.facts.facts.map(item => item.fact_id));
        const facts = [...page.facts.filter(item => !existing.has(item.fact_id)), ...current.facts.facts];
        return publish({
          facts: {
            ...current.facts,
            ...page,
            facts,
            range: {
              start: page.range.start,
              end: current.facts.range.end,
            },
          },
          error: null,
        });
      },
      setMode(interventionMode) { return publish({ interventionMode }); },
      setMessageFilter(messageFilter) { return publish({ messageFilter }); },
      setMessageSearch(messageSearch) { return publish({ messageSearch }); },
      setFactFilter(factFilter) { return publish({ factFilter }); },
      setFactStatus(factStatus) { return publish({ factStatus }); },
      setFactScope(factScope) { return publish({ factScope }); },
      begin(pending) { return publish({ pending, error: null }); },
      complete() { return publish({ pending: null, error: null }); },
      fail(error) { return publish({ pending: null, error: String(error?.message || error) }); },
    });
  }

  return Object.freeze({ create });
});
