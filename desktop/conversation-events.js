/*
 * 本文件把 reasoning、pending tool call 与 ToolMessage 归一为带稳定键和本地图标的紧凑会话事件。
 * 输入为已序列化消息；输出为无重复的逻辑记录及安全 HTML，不持有应用状态。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusConversationEvents = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function escapeHtml(value = "") {
    return String(value).replace(/[&<>"']/g, char => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[char]);
  }

  function text(value) {
    if (typeof value === "string") return value;
    if (Array.isArray(value)) return value.map(item => typeof item === "string" ? item : item?.text || "").join("");
    return value == null ? "" : JSON.stringify(value);
  }

  function preview(value, limit = 110) {
    const compact = text(value).replace(/\s+/g, " ").trim();
    return compact.length > limit ? `${compact.slice(0, limit - 1)}…` : compact;
  }

  function argumentSummary(args) {
    if (!args || typeof args !== "object") return preview(args);
    for (const key of ["path", "file", "url", "query", "command", "directory", "name"]) {
      if (args[key] != null && String(args[key]).trim()) return preview(args[key]);
    }
    return preview(JSON.stringify(args));
  }

  function toolIcon(name) {
    const value = String(name || "").toLowerCase();
    if (/(bash|shell|terminal|command|powershell)/.test(value)) return "terminal";
    if (/(search|web|query|find)/.test(value)) return "search";
    if (/(list|directory|folder)/.test(value)) return "folder";
    if (/(file|read|write|edit|document)/.test(value)) return "file-text";
    return "wrench";
  }

  function statusIcon(status) {
    return { pending: "loader-circle", success: "circle-check", error: "circle-x" }[status] || "triangle-alert";
  }

  function normalize(messages) {
    const list = Array.isArray(messages) ? messages : [];
    const calls = new Map();
    const resolved = new Set();
    for (const message of list) {
      for (const call of (Array.isArray(message?.tool_calls) ? message.tool_calls : [])) {
        if (call?.id) calls.set(call.id, call);
      }
      if (message?.role === "tool" && message.tool_call_id) resolved.add(message.tool_call_id);
    }

    const records = [];
    for (const [index, message] of list.entries()) {
      if (["ai", "assistant"].includes(message?.role)) {
        if (message.reasoning_content) {
          records.push({ type: "reasoning", content: message.reasoning_content, messageId: message.id || "", eventKey: `reasoning:${message.id || message.message_id || index}` });
        }
        if (text(message.content).trim() || message.files?.length) records.push({ type: "message", message });
        for (const call of (Array.isArray(message.tool_calls) ? message.tool_calls : [])) {
          if (!resolved.has(call.id)) records.push({ type: "tool", call, result: null, status: "pending", eventKey: `tool:${call.id || `${index}:${call.name || "tool"}`}` });
        }
      } else if (message?.role === "tool") {
        const call = calls.get(message.tool_call_id) || {
          id: message.tool_call_id, name: message.name || "tool", args: {},
        };
        records.push({
          type: "tool",
          call,
          result: message,
          status: message.status === "error" ? "error" : "success",
          eventKey: `tool:${call.id || message.tool_call_id || `${index}:${call.name || "tool"}`}`,
        });
      } else {
        records.push({ type: "message", message });
      }
    }
    return records;
  }

  function renderReasoning(event) {
    const full = text(event.content);
    return `<details class="conversation-event is-reasoning" data-event-key="${escapeHtml(event.eventKey || "reasoning")}">
      <summary><span class="conversation-event-mark" aria-hidden="true"><span class="ui-icon is-sm icon-brain-circuit"></span></span><strong>Think</strong><span class="conversation-event-preview">${escapeHtml(preview(full))}</span></summary>
      <div class="conversation-event-detail"><pre>${escapeHtml(full)}</pre></div>
    </details>`;
  }

  function renderTool(event) {
    const call = event.call || {};
    const args = call.args || {};
    const output = text(event.result?.content);
    const status = event.status || "pending";
    const statusLabel = { pending: "调用中", success: "完成", error: "失败" }[status] || status;
    const error = status === "error" ? preview(output, 140) : "";
    const name = call.name || event.result?.name || "tool";
    return `<details class="conversation-event is-tool is-${escapeHtml(status)}" data-event-key="${escapeHtml(event.eventKey || `tool:${call.id || name}`)}">
      <summary><span class="conversation-event-mark" aria-hidden="true"><span class="ui-icon is-sm icon-${toolIcon(name)}"></span></span><strong>${escapeHtml(name)}</strong><span class="conversation-event-preview">${escapeHtml(argumentSummary(args) || error)}</span><span class="conversation-event-status"><span class="ui-icon icon-${statusIcon(status)}" aria-hidden="true"></span><span>${escapeHtml(statusLabel)}</span></span>${error ? `<span class="conversation-event-error">${escapeHtml(error)}</span>` : ""}</summary>
      <div class="conversation-event-detail"><dl><div><dt>参数</dt><dd><pre>${escapeHtml(JSON.stringify(args, null, 2))}</pre></dd></div>${event.result ? `<div><dt>输出</dt><dd><pre>${escapeHtml(output)}</pre></dd></div>` : ""}</dl></div>
    </details>`;
  }

  function renderEvent(event) {
    if (event?.type === "reasoning") return renderReasoning(event);
    if (event?.type === "tool") return renderTool(event);
    return "";
  }

  return { normalize, renderEvent, preview };
});
