/*
 * 本文件对外提供 Agent Loop 控制台的界面状态与分页会话缓存。
 * 输入为一次性 Portfolio manifest、权威 Live projection 派生的 Context/Run/Fact 与 Context 派生边、分页会话、视口和筛选；输出为现有控制台视图的不可变读模型。
 * 具体工作流为 Live projection 更新拓扑活动、事实与派生边（已有快照时以投影 lineage 取代 manifest 那份一次性边集合），Store 仅保留选择、筛选、草稿相关状态及有界会话窗口，不再拥有 Loop 领域状态。
 * 示例：`const store = FocusLoopConsoleStore.create(); store.loadManifest(payload)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopConsoleStore = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const MAX_CONVERSATION_MESSAGES = 144;

  const initial = () => Object.freeze({
    manifest: null,
    selectedContextId: null,
    conversation: null,
    conversationViewport: null,
    facts: null,
    patrol: null,
    graphActivity: Object.freeze([]),
    causality: Object.freeze([]),
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
    const conversations = new Map();
    const viewports = new Map();
    const revisionId = value => value?.revision?.revision_id || "current";
    const key = (contextId, revision = "current") => `${contextId}:${revision}`;
    const expectedKey = contextId => {
      const node = current.manifest?.nodes?.find(item => item.context_id === contextId);
      return key(contextId, revisionId(node));
    };
    const conversationKey = value => key(value.context_id, revisionId(value));
    const normalizeConversation = value => {
      const ordered = [...(value?.messages || [])].sort((left, right) => left.index - right.index);
      const messages = ordered.slice(-MAX_CONVERSATION_MESSAGES);
      const range = messages.length
        ? { start: messages[0].index, end: messages[messages.length - 1].index + 1 }
        : { start: value?.range?.start || 0, end: value?.range?.end || 0 };
      const total = Number(value?.total ?? range.end);
      return Object.freeze({
        ...value,
        messages,
        total,
        range,
        has_more: range.start > 0,
        has_newer: range.end < total,
        next_before: range.start > 0 ? range.start : null,
        next_after: range.end < total ? range.end : null,
      });
    };
    function publish(patch) {
      current = Object.freeze({ ...current, ...patch });
      listeners.forEach(listener => listener(current));
      return current;
    }
    function mergeConversation(page, direction = "older") {
      if (!current.conversation || current.conversation.context_id !== page.context_id) {
        return current;
      }
      if (revisionId(current.conversation) !== revisionId(page)) return current;
      const merged = new Map(current.conversation.messages.map(item => [item.index, item]));
      for (const item of page.messages || []) merged.set(item.index, item);
      const ordered = [...merged.values()].sort((left, right) => left.index - right.index);
      const messages = ordered.length <= MAX_CONVERSATION_MESSAGES
        ? ordered
        : direction === "newer"
          ? ordered.slice(-MAX_CONVERSATION_MESSAGES)
          : ordered.slice(0, MAX_CONVERSATION_MESSAGES);
      const conversation = normalizeConversation({ ...current.conversation, ...page, messages, total: page.total ?? current.conversation.total });
      conversations.set(conversationKey(conversation), conversation);
      return publish({ conversation, error: null });
    }
    return Object.freeze({
      get: () => current,
      subscribe(listener) { listeners.add(listener); listener(current); return () => listeners.delete(listener); },
      reset() { conversations.clear(); viewports.clear(); return publish(initial()); },
      loadManifest(manifest) {
        const ids = new Set((manifest?.nodes || []).map(node => node.context_id));
        const selectedContextId = ids.has(current.selectedContextId)
          ? current.selectedContextId
          : manifest?.initial_context_id || manifest?.nodes?.[0]?.context_id || null;
        const validKeys = new Set((manifest?.nodes || []).map(node => key(node.context_id, revisionId(node))));
        for (const cacheKey of conversations.keys()) if (!validKeys.has(cacheKey)) conversations.delete(cacheKey);
        for (const cacheKey of viewports.keys()) if (!validKeys.has(cacheKey)) viewports.delete(cacheKey);
        const selectedKey = key(
          selectedContextId,
          revisionId(manifest?.nodes?.find(node => node.context_id === selectedContextId)),
        );
        return publish({
          manifest,
          selectedContextId,
          conversation: conversations.get(selectedKey) || null,
          conversationViewport: viewports.get(selectedKey) || null,
          error: null,
        });
      },
      projectLive(projection, selectors) {
        if (!projection || !selectors) return current;
        const cards = selectors.selectContextCards(projection);
        const priorNodes = new Map((current.manifest?.nodes || []).map(node => [node.context_id, node]));
        const nodes = cards.map(card => {
          const prior = priorNodes.get(card.id) || {};
          return {
            ...prior,
            context_id: card.id,
            title: card.title || prior.title || card.id,
            topic: prior.topic || card.title || card.id,
            purpose: prior.purpose || card.role || "Context",
            role: card.role || prior.role || "worker",
            status: card.status || prior.status || "active",
            lane_id: card.lane_id || prior.lane_id || null,
            revision: card.current_revision_id ? { ...(prior.revision || {}), revision_id: card.current_revision_id } : prior.revision,
            latest_run: card.latest_run ? { ...(prior.latest_run || {}), run_id: card.latest_run.id, ...card.latest_run } : prior.latest_run || null,
          };
        });
        const loop = projection.loop?.state || {};
        const round = projection.round?.state || null;
        const edges = projection.last_sequence > 0 && typeof selectors.selectLineage === "function"
          ? selectors.selectLineage(projection)
          : (current.manifest?.edges || []);
        const manifest = current.manifest ? {
          ...current.manifest,
          loop_id: projection.loop_id,
          status: loop.status,
          health: loop.health,
          waiting_reason: loop.waiting_reason,
          current_round: round,
          current_round_id: round?.round_id || projection.round?.entity_id || loop.current_round_id,
          initial_context_id: loop.initial_context_id || current.manifest.initial_context_id,
          nodes,
          edges,
        } : null;
        const contextId = current.factScope === "all" ? null : current.selectedContextId;
        const kind = current.factFilter === "all" ? null : current.factFilter;
        const status = current.factStatus === "all" ? null : current.factStatus;
        const factRows = selectors.selectFacts(projection, { contextId, kind, status });
        const facts = { facts: factRows, range: { start: 0, end: factRows.length }, has_more: false, next_before: null, total: factRows.length };
        return publish({
          manifest,
          facts,
          patrol: selectors.selectPatrol(projection),
          graphActivity: selectors.selectGraphActivity(projection),
          causality: selectors.selectCausality(projection, current.selectedContextId),
          error: null,
        });
      },
      selectContext(contextId) {
        const cacheKey = expectedKey(contextId);
        return publish({
          selectedContextId: contextId,
          conversation: conversations.get(cacheKey) || null,
          conversationViewport: viewports.get(cacheKey) || null,
          error: null,
        });
      },
      loadConversation(conversation) {
        const normalized = normalizeConversation(conversation);
        conversations.set(conversationKey(normalized), normalized);
        return publish({ conversation: normalized, error: null });
      },
      mergeConversation,
      prependConversation(page) { return mergeConversation(page, "older"); },
      saveViewport(contextId, viewport) {
        if (!contextId) return current;
        const cacheKey = expectedKey(contextId);
        const saved = Object.freeze({ scrollTop: Math.max(0, Number(viewport?.scrollTop) || 0) });
        viewports.set(cacheKey, saved);
        return current.selectedContextId === contextId
          ? publish({ conversationViewport: saved })
          : current;
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

  return Object.freeze({ create, MAX_CONVERSATION_MESSAGES });
});
