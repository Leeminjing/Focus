/*
 * 本文件对外提供 reduce、reduceBatch 与 SequenceGapError。
 * 输入为已验证的 Live Loop projection 和严格递增的 canonical event；输出为结构共享、确定性的下一份 projection。
 * 具体工作流为拒绝 sequence 缺口，同一 identity 按 revision 更新，保留各 Round/Observation/Session，并由当前观察身份派生认知视图；旧 generation 仅保留历史，排除工具事实并保留独立活动。示例：`reduce(state, event)`。
 * intervention 按同一 revision/sequence 规则归约，来源、类型及 delivery state 留在公开活动详情。
 */
(function (root, factory) {
  const api = factory(
    typeof module === "object" && module.exports ? require("./loop-live-schema.js") : root.FocusLoopLiveSchema,
  );
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopLiveReducer = api;
})(typeof globalThis === "object" ? globalThis : this, function (Schema) {
  "use strict";

  const SINGULAR = new Set(Schema.SINGULAR.filter(name => !["round", "patrol_session"].includes(name)));
  const ACTIVITY_ONLY = new Set(["tool", "artifact", "model_call", "loop_activity"]);
  const COLLECTION_BY_ENTITY = Object.freeze({ round: "rounds", observation: "observations", patrol_session: "patrol_sessions", context: "contexts", context_lineage: "lineage", run: "runs", context_run: "runs", curator: "curators", context_expansion: "expansions", directive: "directives", intervention: "interventions", fact: "facts", loop_wait_request: "wait_requests", loop_wait_response: "wait_responses" });

  class SequenceGapError extends Error {
    constructor(expected, actual) {
      super(`Live Loop sequence 缺口：期望 ${expected}，实际 ${actual}`);
      this.name = "SequenceGapError";
      this.expected = expected;
      this.actual = actual;
    }
  }

  function activity(event) {
    return Object.freeze({
      event_id: event.event_id,
      sequence: event.sequence,
      kind: event.kind,
      entity_type: event.entity_type,
      entity_id: event.entity_id,
      summary: String(event.payload.summary || event.payload.status || event.kind).slice(0, 500),
      occurred_at: event.occurred_at || null,
      correlation_id: event.correlation_id || null,
      causation_id: event.causation_id || null,
      detail: Object.freeze(Object.fromEntries(["context_id", "target_context_id", "source_context_id", "run_id", "tool_name", "status", "directive_id", "opportunity_id", "blocker_code", "origin", "intent_kind", "state"].filter(key => event.payload[key] != null).map(key => [key, event.payload[key]]))),
    });
  }

  function normalizedPayload(entityType, payload) {
    if (entityType === "fact" && payload.fact_type) {
      const presentation = payload.presentation || {};
      return Object.freeze({ ...payload, kind: payload.fact_type, status: payload.state, context_id: payload.source_context_id, run_id: payload.source_run_id, title: presentation.title, summary: presentation.summary, metrics: presentation.metrics || {}, outcome_status: presentation.outcome_status });
    }
    if (entityType === "patrol_session") return Object.freeze({ ...payload, safe_summary: payload.safe_summary || payload.summary || null });
    if (entityType === "curator") return Object.freeze({ ...payload, safe_summary: payload.safe_summary || payload.result_summary || payload.summary || null });
    if (entityType === "context_expansion") return Object.freeze({ ...payload, safe_summary: payload.safe_summary || payload.summary || payload.state || null });
    return payload;
  }

  function reduce(projection, input) {
    if (!projection.rounds || !projection.patrol_sessions || !projection.observations) projection = Schema.validateSnapshot(projection);
    const event = Schema.validateEvent(input);
    if (event.loop_id !== projection.loop_id) throw new TypeError("event 不属于当前 Loop projection");
    if (event.sequence <= projection.last_sequence) return projection;
    if (event.sequence !== projection.last_sequence + 1) throw new SequenceGapError(projection.last_sequence + 1, event.sequence);
    const next = { ...projection, last_sequence: event.sequence };
    const normalized = normalizedPayload(event.entity_type, event.payload);
    const payload = event.correlation_id && normalized.correlation_id == null ? Object.freeze({ ...normalized, correlation_id: event.correlation_id }) : normalized;
    if (SINGULAR.has(event.entity_type)) {
      const prior = projection[event.entity_type];
      const same = prior?.entity_id === event.entity_id;
      if (!same || prior.revision < event.entity_revision) {
        next[event.entity_type] = Object.freeze({ entity_id: event.entity_id, revision: event.entity_revision, updated_sequence: event.sequence, state: Object.freeze({ ...(same ? prior.state : {}), ...payload }) });
      }
    } else {
      const field = COLLECTION_BY_ENTITY[event.entity_type];
      if (field === "facts" && (payload.kind || payload.fact_type) === "tool") {
        const facts = { ...projection.facts };
        delete facts[event.entity_id];
        next.facts = Object.freeze(facts);
      } else if (field) {
        const prior = projection[field][event.entity_id];
        const incoming = Object.freeze({ entity_id: event.entity_id, revision: event.entity_revision, updated_sequence: event.sequence, state: Object.freeze({ ...(prior?.state || {}), ...payload }) });
        const staleObservation = event.entity_type === "round" && prior
          && Number(payload.observation_generation || 0) < Number(prior.state.observation_generation || 0);
        if ((!prior || prior.revision < incoming.revision) && !staleObservation) next[field] = Object.freeze({ ...projection[field], [event.entity_id]: incoming });
      } else if (ACTIVITY_ONLY.has(event.entity_type)) {
        const runId = event.payload.run_id;
        const prior = runId && projection.runs[runId];
        if (prior) next.runs = Object.freeze({
          ...projection.runs,
          [runId]: Object.freeze({
            ...prior,
            updated_sequence: event.sequence,
            state: Object.freeze({
              ...prior.state,
              last_activity_at: event.occurred_at || null,
              last_activity_kind: event.kind,
              last_activity_summary: event.payload.summary || event.kind,
            }),
          }),
        });
      } else {
        next.unknown_kinds = Object.freeze([...new Set([...projection.unknown_kinds, event.kind])]);
      }
    }
    next.activity_timeline = Object.freeze([...projection.activity_timeline, activity(event)].slice(-200));
    return Schema.currentEntities(next);
  }

  function reduceBatch(projection, events) {
    return (events || []).reduce(reduce, projection);
  }

  return Object.freeze({ SequenceGapError, reduce, reduceBatch });
});
