/*
 * 本文件对外提供 Live Loop projection 的只读选择器。
 * 输入为规范化 projection 与可选 Context identity；输出为 Patrol、图活动、Context 卡片、因果链、事实和摘要指标。
 * 具体工作流为仅从实体 state 派生稳定展示模型，不持有领域状态；示例：`selectContextCards(projection)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopLiveSelectors = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const values = collection => Object.values(collection || {});
  const stateOf = entity => entity?.state || null;
  const MAX_VISIBLE_GRAPH_ACTIVITY = 80;
  const MAX_VISIBLE_FACTS = 200;

  function selectPatrol(projection) {
    const session = stateOf(projection?.patrol_session);
    if (!session) return null;
    return Object.freeze({ id: projection.patrol_session.entity_id, revision: projection.patrol_session.revision, ...session, safe_summary: session.safe_summary || session.summary || null });
  }

  function selectGraphActivity(projection) {
    const directives = values(projection?.directives).sort((left, right) => right.updated_sequence - left.updated_sequence).slice(0, MAX_VISIBLE_GRAPH_ACTIVITY).map(entity => ({ id: entity.entity_id, revision: entity.revision, ...entity.state }));
    const correlated = new Map(values(projection?.runs).filter(entity => entity.state.directive_id).map(entity => [entity.state.directive_id, entity]));
    return Object.freeze(directives.map(item => Object.freeze({ ...item, run: stateOf(correlated.get(item.id)) })));
  }

  function selectContextCards(projection) {
    const runs = values(projection?.runs);
    return Object.freeze(values(projection?.contexts).map(entity => {
      const contextRuns = runs.filter(run => run.state.context_id === entity.entity_id).sort((left, right) => right.updated_sequence - left.updated_sequence);
      const activeRun = contextRuns.find(run => ["queued", "pending", "running"].includes(run.state.status)) || null;
      const runIds = new Set(contextRuns.map(run => run.entity_id));
      const liveAction = [...(projection?.activity_timeline || [])].reverse().find(item => item.detail?.context_id === entity.entity_id || runIds.has(item.detail?.run_id));
      return Object.freeze({ id: entity.entity_id, revision: entity.revision, ...entity.state, active_run: activeRun ? { id: activeRun.entity_id, ...activeRun.state } : null, latest_run: contextRuns[0] ? { id: contextRuns[0].entity_id, ...contextRuns[0].state } : null, live_action: liveAction || null });
    }));
  }

  function selectCausality(projection, contextId) {
    const directives = values(projection?.directives).filter(entity => !contextId || entity.state.target_context_id === contextId);
    const ids = new Set(directives.map(entity => entity.entity_id));
    const runs = values(projection?.runs).filter(entity => ids.has(entity.state.directive_id) || (!entity.state.directive_id && (!contextId || entity.state.context_id === contextId)));
    const correlations = new Set([...directives, ...runs].map(entity => entity.state.correlation_id).filter(Boolean));
    return Object.freeze(projection.activity_timeline.filter(item => ids.has(item.entity_id) || correlations.has(item.correlation_id)));
  }

  function selectFacts(projection, options = {}) {
    return Object.freeze(values(projection?.facts)
      .filter(entity => !options.contextId || entity.state.context_id === options.contextId)
      .filter(entity => !options.kind || entity.state.kind === options.kind)
      .filter(entity => !options.status || entity.state.status === options.status)
      .sort((left, right) => right.updated_sequence - left.updated_sequence)
      .slice(0, MAX_VISIBLE_FACTS)
      .map(entity => Object.freeze({ fact_id: entity.entity_id, revision: entity.revision, ...entity.state })));
  }

  function selectSummary(projection) {
    const loop = stateOf(projection?.loop) || {};
    const activeRuns = values(projection?.runs).filter(entity => ["queued", "pending", "running"].includes(entity.state.status));
    return Object.freeze({
      status: loop.status || "idle",
      health: loop.health || "unknown",
      round: stateOf(projection?.round)?.number || 0,
      active_contexts: new Set(activeRuns.map(entity => entity.state.context_id)).size,
      context_count: values(projection?.contexts).length,
      curator_count: values(projection?.curators).filter(entity => !["consumed", "failed", "cancelled"].includes(entity.state.state)).length,
      last_sequence: projection?.last_sequence || 0,
    });
  }

  return Object.freeze({ MAX_VISIBLE_GRAPH_ACTIVITY, MAX_VISIBLE_FACTS, selectPatrol, selectGraphActivity, selectContextCards, selectCausality, selectFacts, selectSummary });
});
