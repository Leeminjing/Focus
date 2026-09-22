/*
 * 本文件对外提供 validateSnapshot、validateEvent 与 emptyProjection 函数。
 * 输入为 Live Loop API 返回的未知 JSON 值；输出为结构规范、可安全归约的 projection 或带字段路径的 TypeError。
 * 具体工作流为校验根边界与所有实体信封（含 contexts 与 lineage 派生边集合）、复制集合和时间线并冻结顶层结果；示例：`validateSnapshot(await api.liveSnapshot(loopId))`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopLiveSchema = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const COLLECTIONS = Object.freeze(["contexts", "lineage", "runs", "curators", "expansions", "directives", "facts", "wait_requests", "wait_responses"]);
  const SINGULAR = Object.freeze(["loop", "mission", "patrol_session", "round", "portfolio"]);

  function record(value, path) {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError(`${path} 必须是对象`);
    return value;
  }

  function integer(value, path, minimum = 0) {
    if (!Number.isSafeInteger(value) || value < minimum) throw new TypeError(`${path} 必须是大于等于 ${minimum} 的整数`);
    return value;
  }

  function entity(value, path) {
    if (value == null) return null;
    const source = record(value, path);
    if (typeof source.entity_id !== "string" || !source.entity_id) throw new TypeError(`${path}.entity_id 必须是非空字符串`);
    integer(source.revision, `${path}.revision`, 1);
    integer(source.updated_sequence, `${path}.updated_sequence`, 1);
    record(source.state, `${path}.state`);
    return Object.freeze({ ...source, state: Object.freeze({ ...source.state }) });
  }

  function collection(value, path) {
    const source = record(value, path);
    const result = {};
    for (const [key, item] of Object.entries(source)) {
      const normalized = entity(item, `${path}.${key}`);
      if (normalized.entity_id !== key) throw new TypeError(`${path}.${key}.entity_id 与集合键不一致`);
      result[key] = normalized;
    }
    return Object.freeze(result);
  }

  function emptyProjection(loopId = "") {
    return Object.freeze({
      loop_id: loopId,
      last_sequence: 0,
      loop: null,
      mission: null,
      patrol_session: null,
      round: null,
      contexts: Object.freeze({}),
      lineage: Object.freeze({}),
      runs: Object.freeze({}),
      curators: Object.freeze({}),
      expansions: Object.freeze({}),
      directives: Object.freeze({}),
      facts: Object.freeze({}),
      wait_requests: Object.freeze({}),
      wait_responses: Object.freeze({}),
      portfolio: null,
      activity_timeline: Object.freeze([]),
      unknown_kinds: Object.freeze([]),
      diagnostics: Object.freeze({ journal_last_sequence: 0, projector_last_sequence: 0, lag: 0, rebuilt: false, recovery_status: "healthy", degraded_scope: Object.freeze([]), quarantined_units: Object.freeze([]), updated_at: null }),
    });
  }

  function validateSnapshot(value) {
    const source = record(value, "snapshot");
    if (typeof source.loop_id !== "string" || !source.loop_id) throw new TypeError("snapshot.loop_id 必须是非空字符串");
    integer(source.last_sequence, "snapshot.last_sequence");
    const result = { ...emptyProjection(source.loop_id), ...source };
    for (const name of SINGULAR) result[name] = entity(source[name] ?? null, `snapshot.${name}`);
    for (const name of COLLECTIONS) result[name] = collection(source[name] ?? {}, `snapshot.${name}`);
    if (!Array.isArray(source.activity_timeline ?? [])) throw new TypeError("snapshot.activity_timeline 必须是数组");
    if (!Array.isArray(source.unknown_kinds ?? [])) throw new TypeError("snapshot.unknown_kinds 必须是数组");
    result.activity_timeline = Object.freeze([...(source.activity_timeline ?? [])].slice(-200).map(item => Object.freeze({ ...record(item, "snapshot.activity_timeline[]") })));
    result.unknown_kinds = Object.freeze([...(source.unknown_kinds ?? [])]);
    const diagnostics = source.diagnostics || emptyProjection().diagnostics;
    result.diagnostics = Object.freeze({ ...emptyProjection().diagnostics, ...diagnostics, degraded_scope: Object.freeze([...(diagnostics.degraded_scope || [])]), quarantined_units: Object.freeze([...(diagnostics.quarantined_units || [])]) });
    return Object.freeze(result);
  }

  function validateEvent(value) {
    const source = record(value, "event");
    for (const field of ["event_id", "loop_id", "kind", "entity_type", "entity_id"]) {
      if (typeof source[field] !== "string" || !source[field]) throw new TypeError(`event.${field} 必须是非空字符串`);
    }
    integer(source.sequence, "event.sequence", 1);
    integer(source.entity_revision, "event.entity_revision", 1);
    record(source.payload, "event.payload");
    return Object.freeze({ ...source, payload: Object.freeze({ ...source.payload }) });
  }

  return Object.freeze({ COLLECTIONS, SINGULAR, emptyProjection, validateSnapshot, validateEvent });
});
