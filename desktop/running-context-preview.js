/* 本文件对外提供 FocusRunningContextPreview.create、reduce 与 connection 的纯 Run 输出预览归约。
 * 输入为已核对的 {run_id,context_id,workspace_id,thread_id,message_id} 身份及原始 SSE frame；输出为 role、actor、tool_name、text、status 与 notice 的只读临时视图。
 * 工作流按全部事件游标去重/检测缺口，过滤其他运行与初始历史，按真实消息或工具调用身份切换当前角色；正文只保留 4096 个 Unicode code point，消息指纹最多 256 条。
 * 首个普通 values 默认建立历史基线；本 Run 已接收 tokens 的当前消息及其精确工具调用可确认归属，因此可承接快照与工具结果，旧消息不覆盖后来的角色。
 * 后续实际变化的完整消息覆盖增量；具名角色显式 content_mode/stream_id 区分传输重试，思考、输入、参数和元数据不充当输出。
 * 示例：let preview = create(identity); preview = reduce(preview, {type:"tokens",id:"2",data:JSON.stringify(envelope)})；connection(preview,"reconnecting") 保留真实内容并标记断线。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusRunningContextPreview = api;
})(globalThis, function () {
  "use strict";
  const TEXT_LIMIT = 4096, SEEN_LIMIT = 256;
  const terminal = new Set(["success", "error", "interrupted", "timeout", "cancelled", "stopped"]);
  const text = value => typeof value === "string" ? value : Array.isArray(value)
    ? value.map(block => typeof block === "string" ? block : typeof block?.text === "string" && (!block.type || ["text", "output_text"].includes(block.type)) ? block.text : "").join("") : "";
  const tail = value => Array.from(value).slice(-TEXT_LIMIT).join("");
  const roleName = actor => ({ assistant: "Assistant", supervisor: "Supervisor", worker: "Worker", evaluator: "Evaluator" })[actor] || actor;
  const signature = value => {
    let hash = 2166136261;
    for (let index = 0; index < value.length; index++) hash = Math.imul(hash ^ value.charCodeAt(index), 16777619);
    return `${value.length}:${hash >>> 0}`;
  };
  function create(identity) {
    return Object.freeze({ identity: Object.freeze({ ...identity }), run_id: identity.run_id, role: null, actor: null, tool_name: null, message_id: null,
      text: "", status: "waiting", notice: "", connection_status: "connecting", last_sequence: null, current_key: null,
      seen: Object.freeze({}), tools: Object.freeze({}), baseline: false, ended: false });
  }
  function bounded(collection, key, value) {
    const next = { ...collection };
    delete next[key];
    next[key] = value;
    const keys = Object.keys(next);
    for (const old of keys.slice(0, Math.max(0, keys.length - SEEN_LIMIT))) delete next[old];
    return Object.freeze(next);
  }
  function remember(state, key, fingerprint, fromDelta = false) {
    return { ...state, seen: bounded(state.seen, key, { fingerprint, fromDelta: fromDelta || state.seen[key]?.fromDelta || false }) };
  }
  function output(state, { key, role, actor = null, tool = null, content = "", delta = false, waiting = false, messageId = null }) {
    return { ...state, current_key: key, role, actor, tool_name: tool, message_id: messageId,
      text: tail(delta && state.current_key === key ? state.text + content : content), status: waiting ? "tool_wait" : "streaming" };
  }
  function messages(state, rows, { actor = "assistant", baseline = false, scope = "main" } = {}) {
    let next = state;
    const confirmedKey = baseline && scope === "main" && state.seen[state.current_key]?.fromDelta ? state.current_key : null;
    const confirmedCalls = new Set(rows.filter(message => `${scope}:${actor}:message:${message.id || message.message_id}` === confirmedKey)
      .flatMap(message => (message.tool_calls || []).map(call => call.id).filter(Boolean)));
    for (const [index, message] of rows.entries()) {
      const role = String(message.role || message.type || "").toLowerCase();
      if (!["ai", "assistant", "tool", "aimessage", "toolmessage"].includes(role)) continue;
      const content = text(message.content), tool = role.includes("tool");
      const id = message.id || message.message_id || (tool && message.tool_call_id) || `${index}:${signature(content)}`;
      const key = `${scope}:${actor}:${tool ? "tool" : "message"}:${id}`;
      const fingerprint = signature(`${content}\0${JSON.stringify((message.tool_calls || []).map(call => [call.id, call.name]))}\0${message.status || ""}`);
      const previous = next.seen[key];
      const changed = previous?.fingerprint !== fingerprint;
      const olderStreamedMessage = scope === "main" && !tool && previous?.fromDelta && next.current_key !== key;
      const readable = !baseline || key === confirmedKey || tool && confirmedCalls.has(message.tool_call_id);
      next = remember(next, key, fingerprint);
      if (changed && readable && !olderStreamedMessage && content) next = output(next, { key, role: tool ? "Tool" : roleName(actor), actor, tool: tool ? message.name || next.tools[message.tool_call_id]?.name || "tool" : null, content, messageId: id });
      if (tool && message.tool_call_id) next = { ...next, tools: bounded(next.tools, message.tool_call_id, { name: message.name || next.tools[message.tool_call_id]?.name || "tool", resolved: true }) };
      for (const call of message.tool_calls || []) {
        if (!call.id) continue;
        const prior = next.tools[call.id];
        next = { ...next, tools: bounded(next.tools, call.id, { name: call.name || prior?.name || "tool", resolved: prior?.resolved || false }) };
        if (!prior && readable && !olderStreamedMessage) next = output(next, { key: `call:${call.id}`, role: "Tool", actor, tool: call.name || "tool", waiting: true, messageId: call.id });
      }
    }
    return next;
  }
  function values(state, rows) {
    const anchor = rows.findIndex(message => (message.id || message.message_id) === state.identity.message_id);
    const candidates = anchor >= 0 ? rows.slice(anchor + 1) : rows;
    const next = messages(state, candidates, { baseline: !state.baseline });
    return { ...next, baseline: true };
  }
  function commitment(state, payload) {
    const actor = String(payload.actor || "");
    if (!actor) return state;
    const rows = Array.isArray(payload.messages) ? payload.messages : [];
    if (payload.content_mode !== "delta") return messages(state, rows, { actor, scope: "actor" });
    if (!payload.stream_id) return state;
    let next = state;
    for (const message of rows) {
      if (!["ai", "assistant", "aimessagechunk", "aimessage"].includes(String(message.role || message.type || "").toLowerCase())) continue;
      const content = text(message.content);
      if (!content) continue;
      const key = `actor:${actor}:stream:${payload.stream_id}:${message.id || message.message_id || ""}`;
      next = output(next, { key, role: roleName(actor), actor, content, delta: true, messageId: message.id || message.message_id || payload.stream_id });
    }
    return next;
  }
  function connection(state, status) {
    if (state.ended) return state;
    return Object.freeze({ ...state, connection_status: status,
      status: status === "reconnecting" ? "disconnected" : status === "unavailable" ? "unavailable" : state.status });
  }
  function reduce(state, frame) {
    if (state.ended) return state;
    let envelope;
    try { envelope = typeof frame.data === "string" ? JSON.parse(frame.data) : frame.data; }
    catch { return Object.freeze({ ...state, status: "unavailable", notice: "输出事件无法读取" }); }
    if (!envelope || envelope.run_id !== state.identity.run_id) return state;
    if (frame.type !== "end" && (envelope.workspace_id !== state.identity.workspace_id || envelope.thread_id !== state.identity.thread_id)) return state;
    const numeric = /^\d+$/.test(String(frame.id || "")) ? Number(frame.id) : null;
    if (numeric !== null && state.last_sequence !== null && numeric <= state.last_sequence) return state;
    let next = { ...state, last_sequence: numeric ?? state.last_sequence };
    if (numeric !== null && (state.last_sequence === null ? numeric > 1 : numeric > state.last_sequence + 1)) {
      next = { ...next, role: null, actor: null, tool_name: null, text: "", current_key: null, message_id: null, status: "waiting", notice: "已重新接收，早前片段不可用" };
    }
    const payload = envelope.data || {};
    if (frame.type === "end" || frame.type === "metadata" && terminal.has(payload.status)) {
      return Object.freeze({ ...next, role: null, actor: null, tool_name: null, text: "", current_key: null, status: "ended", ended: true, connection_status: "ended" });
    }
    if (frame.type === "tokens") {
      const content = text(payload.content), id = payload.message_id;
      if (content && id && !String(id).startsWith("commitment-stage-")) {
        const key = `main:assistant:message:${id}`;
        next = output(remember(next, key, next.seen[key]?.fingerprint, true), { key, role: "Assistant", actor: "assistant", content, delta: true, messageId: id });
      }
    } else if (frame.type === "events") {
      if (payload.type === "commitment_messages") next = commitment(next, payload);
      else if (Array.isArray(payload.messages)) next = values(next, payload.messages);
    } else if (frame.type === "error") next = { ...next, status: "unavailable", notice: String(payload.error || "输出不可用") };
    return Object.freeze(next);
  }
  return Object.freeze({ create, reduce, connection, TEXT_LIMIT });
});
