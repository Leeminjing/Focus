/*
 * 本文件对外提供 reduce、reduceBatch 与 SequenceGapError。
 * 输入为已验证的 Live Loop projection 和严格递增的 canonical event；输出为结构共享、确定性的下一份 projection。
 * 具体工作流为先拒绝跨 Loop 与 sequence 缺口，再按实体 revision 更新单体或集合、追加有界活动；示例：`reduce(state, event)`。
 */
(function (root, factory) {
  const api = factory(
    typeof module === "object" && module.exports ? require("./loop-live-schema.js") : root.FocusLoopLiveSchema,
  );
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopLiveReducer = api;
})(typeof globalThis === "object" ? globalThis : this, function (Schema) {
  "use strict";

  const SINGULAR = new Set(Schema.SINGULAR);
  const COLLECTION_BY_ENTITY = Object.freeze({ context: "contexts", run: "runs", context_run: "runs", curator: "curators", directive: "directives", fact: "facts" });

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
      detail: Object.freeze(Object.fromEntries(["context_id", "run_id", "tool_name", "status", "directive_id"].filter(key => event.payload[key] != null).map(key => [key, event.payload[key]]))),
    });
  }

  function normalizedPayload(entityType, payload) {
    if (entityType === "fact" && payload.fact_type) {
      const presentation = payload.presentation || {};
      return Object.freeze({ ...payload, kind: payload.fact_type, status: payload.state, context_id: payload.source_context_id, run_id: payload.source_run_id, title: presentation.title, summary: presentation.summary, metrics: presentation.metrics || {}, outcome_status: presentation.outcome_status });
    }
    if (entityType === "patrol_session") return Object.freeze({ ...payload, safe_summary: payload.safe_summary || payload.summary || null });
    if (entityType === "curator") return Object.freeze({ ...payload, safe_summary: payload.safe_summary || payload.result_summary || payload.summary || null });
    return payload;
  }

  function reduce(projection, input) {
    const event = Schema.validateEvent(input);
    if (event.loop_id !== projection.loop_id) throw new TypeError("event 不属于当前 Loop projection");
    if (event.sequence <= projection.last_sequence) return projection;
    if (event.sequence !== projection.last_sequence + 1) throw new SequenceGapError(projection.last_sequence + 1, event.sequence);
    const next = { ...projection, last_sequence: event.sequence };
    const normalized = normalizedPayload(event.entity_type, event.payload);
    const payload = event.correlation_id && normalized.correlation_id == null ? Object.freeze({ ...normalized, correlation_id: event.correlation_id }) : normalized;
    if (SINGULAR.has(event.entity_type)) {
      const prior = projection[event.entity_type];
      const incoming = Object.freeze({ entity_id: event.entity_id, revision: event.entity_revision, updated_sequence: event.sequence, state: Object.freeze({ ...(prior?.state || {}), ...payload }) });
      if (!prior || prior.revision < incoming.revision) next[event.entity_type] = incoming;
    } else {
      const field = COLLECTION_BY_ENTITY[event.entity_type];
      if (field) {
        const prior = projection[field][event.entity_id];
        const incoming = Object.freeze({ entity_id: event.entity_id, revision: event.entity_revision, updated_sequence: event.sequence, state: Object.freeze({ ...(prior?.state || {}), ...payload }) });
        if (!prior || prior.revision < incoming.revision) next[field] = Object.freeze({ ...projection[field], [event.entity_id]: incoming });
      } else {
        next.unknown_kinds = Object.freeze([...new Set([...projection.unknown_kinds, event.kind])]);
      }
    }
    next.activity_timeline = Object.freeze([...projection.activity_timeline, activity(event)].slice(-200));
    return Object.freeze(next);
  }

  function reduceBatch(projection, events) {
    return (events || []).reduce(reduce, projection);
  }

  return Object.freeze({ SequenceGapError, reduce, reduceBatch });
});
