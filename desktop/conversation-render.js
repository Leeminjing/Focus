/*
 * 本文件对外提供会话渲染的单元模型、内容签名与渲染缓存：buildUnits/renderConversation（消息 → 带签名的
 * 单元 → 缓存命中的 HTML）、streamingContent/streamingBlocks（流式正文按顶层块渲染并复用已闭合块）、
 * windowStartIndex/WINDOW_MESSAGES（长会话窗口边界）、stats/resetStats（渲染计数，供预算检查）。
 * 输入为 detail、task、渲染依赖（renderMessage、renderDivider、events 归一、expandMessages、markdown 渲染器）
 * 与运行态快照（activeTaskId、streamBuffers、materialHistory、pluginViewCount）；输出为单元列表、会话 HTML、
 * 流式块 HTML 与统计计数。具体工作流为按窗口截取消息 → 逐段归一为单元 → 按内容签名命中缓存 →
 * 未命中才调用传入的渲染函数；流式正文解析全文但只渲染未闭合尾块。示例：
 * `renderConversation({ detail, task, state, markdown, renderMessage, renderDivider, events, expandMessages })`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusConversationRender = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const WINDOW_MESSAGES = 80;
  const STREAMING_HEADER_HTML = `<header class="work-record-header"><span class="ui-badge is-active">生成中</span></header>`;

  const cache = new Map();
  const stats = { built: 0, reused: 0, streamingBuilt: 0, streamingReused: 0 };

  function snapshotStats() {
    return { ...stats };
  }

  function resetStats() {
    stats.built = 0;
    stats.reused = 0;
    stats.streamingBuilt = 0;
    stats.streamingReused = 0;
  }

  function _join(parts) {
    return parts.map(part => (part == null ? "" : String(part))).join("|");
  }

  function _unitHtml(key, signature, build) {
    const cached = cache.get(key);
    if (cached && cached.signature === signature) {
      stats.reused += 1;
      return cached.html;
    }
    const html = build();
    cache.set(key, { signature, html });
    stats.built += 1;
    return html;
  }

  function _messageSignature(message, roleStructured, state) {
    const calls = Array.isArray(message.tool_calls) ? message.tool_calls : [];
    const history = (state.materialHistory?.get(state.activeTaskId) || []);
    const compression = message.compression;
    return _join([
      "message", roleStructured ? "structured" : "flat",
      message.role, message.id || message.message_id,
      typeof message.content === "string" ? message.content.length : JSON.stringify(message.content ?? "").length,
      String(message.reasoning_content || "").length,
      calls.map(call => `${call.id}:${call.name}:${JSON.stringify(call.args ?? {}).length}`).join(","),
      message.tool_call_id, message.name, message.status,
      compression ? `${compression.block_id}:${(compression.source || []).length}:${compression.deleted ? 1 : 0}` : "",
      message.curation_synthetic ? 1 : 0,
      (message.files || []).length,
      state.activeTaskId, history.length, state.pluginViewCount,
    ]);
  }

  function _eventSignature(events) {
    return _join([
      "events",
      events.map(event => `${event.eventKey}:${event.status || ""}:${String(event.content || "").length}:${String(event.result?.content || "").length}`).join(","),
    ]);
  }

  function _dividerSignature(item) {
    return _join(["divider", item.deleted ? 1 : 0, item.block_id || item.count, item.count, String(item.summary || "").length, (item.source || []).length]);
  }

  function _streamingSignature(runId, buffer) {
    return _join(["streaming", runId, buffer.messageId || "", String(buffer.text || "").length, String(buffer.reasoning || "").length]);
  }

  function windowStartIndex(messages, limit = WINDOW_MESSAGES) {
    const list = Array.isArray(messages) ? messages : [];
    if (!limit || limit <= 0 || list.length <= limit) return 0;
    let start = list.length - limit;
    while (start > 0) {
      const message = list[start] || {};
      const isReset = message.role === "human" || message.role === "user";
      const isSpeech = (message.role === "ai" || message.role === "assistant") && String(message.content ?? "").trim().length > 0;
      if (isReset || isSpeech) break;
      start -= 1;
    }
    return start;
  }

  function _messageUnit(message, roleStructured, messageIndex, input) {
    const key = `message:${message.id || message.message_id || `${message.role || "message"}:${messageIndex}`}`;
    const signature = _messageSignature(message, roleStructured, input.state);
    return {
      key,
      html: _unitHtml(key, signature, () => (roleStructured
        ? input.renderMessage(message, { showRoleHeader: true })
        : input.renderMessage(message, { fallbackKey: messageIndex }))),
    };
  }

  function _placeholderUnit(task, escapeHtml) {
    const signature = _join(["placeholder", task.harness_mode || "", task.workspace_path || ""]);
    return {
      key: "placeholder",
      html: _unitHtml("placeholder", signature, () => (task.harness_mode === "assembly"
        ? `<div class="assembly-empty">
          <p class="assembly-empty-title">无工作区模式</p>
          <p>配置全局 skill、mcp tools、插件等</p>
          <p>创造插件</p>
        </div>`
        : `<article class="work-record message system"><header class="work-record-header"><span class="work-record-kicker">READY</span><span class="message-role">任务已就绪</span></header><div class="message-content"><span class="muted">这是该工作区与线程的第一页。输入任务即可开始。</span><details class="message-details"><summary>工作区路径</summary><pre>${escapeHtml(task.workspace_path)}</pre></details></div></article>`)),
    };
  }

  function _earlierUnit(hiddenCount) {
    const key = `earlier:${hiddenCount}`;
    return {
      key,
      html: _unitHtml(key, _join(["earlier", hiddenCount]), () => `<div class="conversation-earlier"><button type="button" class="text-button" data-action="load-earlier-conversation">加载更早的 ${hiddenCount} 条消息</button></div>`),
    };
  }

  function _messageGroupUnits(group, input, roleStructured, state) {
    if (!group.length) return;
    if (roleStructured) {
      for (const message of group) state.units.push(_messageUnit(message, true, state.messageIndex++, input));
      return;
    }
    let eventGroup = [];
    const flushEvents = () => {
      if (!eventGroup.length) return;
      const events = eventGroup;
      const key = `seq:${events[0].eventKey}`;
      state.units.push({
        key,
        html: _unitHtml(key, _eventSignature(events), () => `<section class="conversation-event-sequence" role="group" aria-label="执行过程">${events.map(input.events.renderEvent).join("")}</section>`),
      });
      eventGroup = [];
    };
    for (const item of input.events.normalize(group)) {
      if (item.type !== "message") {
        state.eventIndex.set(item.eventKey, item);
        eventGroup.push(item);
        continue;
      }
      flushEvents();
      state.units.push(_messageUnit(item.message, false, state.messageIndex++, input));
    }
    flushEvents();
  }

  function buildUnits(input) {
    const detail = input.detail || {};
    const task = input.task || {};
    const allMessages = detail.messages || [];
    const start = windowStartIndex(allMessages, input.windowLimit ?? WINDOW_MESSAGES);
    const visible = start > 0 ? allMessages.slice(start) : allMessages;
    const state = { units: [], eventIndex: new Map(), messageIndex: 0 };
    const roleStructured = Boolean(detail.context?.managed_status);
    if (start > 0) state.units.push(_earlierUnit(start));
    let messageGroup = [];
    for (const item of input.expandMessages(visible)) {
      if (!item.divider) { messageGroup.push(item); continue; }
      _messageGroupUnits(messageGroup, input, roleStructured, state);
      messageGroup = [];
      const key = `divider:${item.deleted ? "deleted:" : "block:"}${item.block_id || item.count}`;
      state.units.push({ key, html: _unitHtml(key, _dividerSignature(item), () => input.renderDivider(item)) });
    }
    _messageGroupUnits(messageGroup, input, roleStructured, state);
    if (!allMessages.length) state.units.push(_placeholderUnit(task, input.escapeHtml));
    for (const [runId, buffer] of input.state.streamBuffers || []) {
      if (buffer.taskId !== task.task_id || !(buffer.text || buffer.reasoning)) continue;
      const key = `stream:${runId}`;
      state.units.push({
        key,
        html: _unitHtml(key, _streamingSignature(runId, buffer), () => `<article class="work-record message ai streaming" data-stream-run="${runId}">${streamingContent(input.markdown, input.events, buffer)}</article>`),
      });
    }
    return { units: state.units, eventIndex: state.eventIndex };
  }

  function renderConversation(input) {
    return buildUnits(input).units.map(unit => unit.html).join("");
  }

  function _splitTopLevelTokenBlocks(tokens) {
    const groups = [];
    let current = null;
    for (const token of tokens) {
      if (token.level !== 0) {
        if (current) current.push(token);
        continue;
      }
      if (token.nesting === 0) {
        groups.push([token]);
        current = null;
      } else if (token.nesting === 1) {
        current = [token];
        groups.push(current);
      } else {
        if (current) current.push(token);
        current = null;
      }
    }
    return groups;
  }

  function markdownBlocks(markdown, text) {
    const source = String(text ?? "");
    if (!source) return [];
    const env = {};
    return _splitTopLevelTokenBlocks(markdown.parse(source, env)).map(
      group => markdown.renderer.render(group, markdown.options, env),
    );
  }

  function streamingBlocks(markdown, buffer) {
    const source = String(buffer.text ?? "");
    const entries = [];
    if (source) {
      const env = {};
      const lines = source.split("\n");
      for (const group of _splitTopLevelTokenBlocks(markdown.parse(source, env))) {
        const start = group[0]?.map?.[0] ?? 0;
        const last = group[group.length - 1];
        const end = last?.map?.[1] ?? start + 1;
        entries.push({ source: lines.slice(start, end).join("\n"), html: markdown.renderer.render(group, markdown.options, env) });
      }
    }
    const previous = buffer.blockEntries || [];
    const next = entries.map((entry, index) => {
      const prior = previous[index];
      if (prior && prior.source === entry.source) {
        stats.streamingReused += 1;
        return prior;
      }
      stats.streamingBuilt += 1;
      return entry;
    });
    buffer.blockEntries = next;
    return next;
  }

  function _reasoningSection(events, reasoning) {
    return reasoning
      ? `<section class="conversation-event-sequence" role="group" aria-label="执行过程">${events.renderEvent({ type: "reasoning", content: reasoning }, { previewMode: "latest" })}</section>`
      : "";
  }

  function streamingContent(markdown, events, buffer) {
    const answer = buffer.text
      ? `<div class="message-rich">${streamingBlocks(markdown, buffer).map(entry => entry.html).join("")}</div>`
      : "";
    return `${STREAMING_HEADER_HTML}${_reasoningSection(events, buffer.reasoning)}${answer}`;
  }

  return {
    WINDOW_MESSAGES,
    STREAMING_HEADER_HTML,
    buildUnits,
    renderConversation,
    markdownBlocks,
    streamingBlocks,
    streamingContent,
    windowStartIndex,
    stats: snapshotStats,
    resetStats,
  };
});
