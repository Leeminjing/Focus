/*
 * 本文件对外提供会话渲染的单元模型、内容签名与渲染缓存：buildUnits/renderConversation（消息 → 带签名的
 * 单元 → 缓存命中的 HTML）、streamingContent/streamingBlocks/visibleStreamBlocks（流式正文按顶层块渲染
 * 并复用已闭合块；可见正文以 `STREAM_TEXT_LIMIT` 为界，越限不产出正文块）、
 * windowStartIndex/WINDOW_MESSAGES/WINDOW_ROWS（长会话窗口边界：消息条数与已挂载行数两个上限同时生效，
 * 并按"完整工具调用组"回退到安全起点）、stats/resetStats（渲染计数与缓存规模，供预算检查）。
 * 输入为 detail、task、渲染依赖（renderMessage、renderDivider、events 归一、expandMessages、markdown 渲染器）
 * 与运行态快照（activeTaskId、streamBuffers、materialHistory、pluginViewCount）；输出为单元列表、会话 HTML、
 * 流式块 HTML 与统计计数。具体工作流为按窗口截取消息 → 逐段归一为单元 → 按内容签名命中缓存 →
 * 未命中才调用传入的渲染函数；流式正文解析全文但只渲染未闭合尾块。
 * 可见正文边界约定：可见渲染与缓冲语义分离——缓冲照旧累积（完成态判据取正文前缀），而超过
 * `STREAM_TEXT_LIMIT` 的正文不交给 markdown 渲染、也不进入可见 DOM，使越界或超长正文既有界又不会以
 * "助手正在生成"的形态出现在会话里（工具结果只经快照以工具行呈现）。
 * 记忆化粒度：单元级（消息/分界/流式/加载更早）与**行级**（执行过程的事件行）两层。行级缓存以
 * "行身份 + 行签名"为键，因此容器内其它行的变化不会使本行失效——尾部追加一行只重建那一行；
 * 缓存按 `CACHE_LIMIT` 淘汰最久未命中的条目，条目数与会话长度无关。
 * 对账身份约定：单元键与写入侧一一对应，并落到 DOM 的 `data-unit-key` 上——事件序列、加载更早入口与其余
 * 顶层单元都带键，因此写入侧无需靠"有没有事件子节点"反推身份。事件序列的身份取"在单元序列中的序位"
 * （`seq#<序位>`）而非首行事件键：窗口上边界前移会换掉首行，取首行键会让写入侧把同一段序列判成新单元并
 * 整体重建。无消息 id 时回退身份取该消息在**完整消息列表**中的绝对下标（而非窗口内相对序号），使窗口滑动
 * 不改名。流式占位的身份类名由 `STREAMING_PLACEHOLDER_CLASS` 统一提供，供写入侧就地创建占位时保持属性
 * 逐字一致——两者属性不一致会让对账因签名不符把占位整块替换。
 * 示例：`renderConversation({ detail, task, state, markdown, renderMessage, renderDivider, events, expandMessages })`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusConversationRender = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const WINDOW_MESSAGES = 80;
  const WINDOW_ROWS = 90;
  const CACHE_LIMIT = 4000;
  const STREAM_TEXT_LIMIT = 64 * 1024;
  const STREAMING_HEADER_HTML = `<header class="work-record-header"><span class="ui-badge is-active">生成中</span></header>`;
  const STREAMING_PLACEHOLDER_CLASS = "work-record message ai streaming";

  const cache = new Map();
  const stats = { built: 0, reused: 0, rowsBuilt: 0, rowsReused: 0, streamingBuilt: 0, streamingReused: 0 };

  function snapshotStats() {
    return { ...stats, cached: cache.size, cacheLimit: CACHE_LIMIT };
  }

  function resetStats() {
    stats.built = 0;
    stats.reused = 0;
    stats.rowsBuilt = 0;
    stats.rowsReused = 0;
    stats.streamingBuilt = 0;
    stats.streamingReused = 0;
  }

  function _join(parts) {
    return parts.map(part => (part == null ? "" : String(part))).join("|");
  }

  function escapeAttribute(value) {
    return String(value).replace(/[&<>"']/g, char => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[char]);
  }

  // 缓存按容量上限淘汰最久未命中的条目：行级缓存条目数因此与会话长度无关。
  function _evict() {
    while (cache.size > CACHE_LIMIT) {
      const oldest = cache.keys().next();
      if (oldest.done) return;
      cache.delete(oldest.value);
    }
  }

  function _unitHtml(key, signature, build) {
    const cached = cache.get(key);
    if (cached && cached.signature === signature) {
      stats.reused += 1;
      return cached.html;
    }
    const html = build();
    cache.delete(key);
    cache.set(key, { signature, html });
    _evict();
    stats.built += 1;
    return html;
  }

  function _rowHtml(key, signature, build) {
    const cached = cache.get(key);
    if (cached && cached.signature === signature) {
      stats.rowsReused += 1;
      return cached.html;
    }
    const html = build();
    cache.delete(key);
    cache.set(key, { signature, html });
    _evict();
    stats.rowsBuilt += 1;
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

  function _rowSignature(event) {
    return _join([
      "row", event.eventKey,
      event.status || "",
      String(event.content || "").length,
      String(event.result?.content || "").length,
    ]);
  }

  function _eventsSignature(events) {
    return _join(["events", events.map(_rowSignature).join(",")]);
  }

  // 事件行的 HTML 按"行身份 + 行签名"记忆化：容器里其它行的变化不影响本行缓存，
  // 因此"尾部追加一行"只重建那一行。
  function _eventRowHtml(event, input) {
    return _rowHtml(`event:${event.eventKey}`, _rowSignature(event), () => input.events.renderEvent(event));
  }

  function _dividerSignature(item) {
    return _join(["divider", item.deleted ? 1 : 0, item.block_id || item.count, item.count, String(item.summary || "").length, (item.source || []).length]);
  }

  function _streamingSignature(runId, buffer) {
    return _join(["streaming", runId, buffer.messageId || "", String(buffer.text || "").length, String(buffer.reasoning || "").length]);
  }

  // 一条消息在执行序列里占几行：助手消息按"推理 + 正文 + 未决工具调用"计，工具结果各占一行。
  // 未决判定与 events 归一同口径（已被工具结果回应的调用不再在助手消息处计行）。
  function _messageRowCount(message, resolvedCalls) {
    const role = message?.role;
    if (role === "tool") return 1;
    if (role !== "ai" && role !== "assistant") return 1;
    let rows = 0;
    if (message.reasoning_content) rows += 1;
    if (String(message.content ?? "").trim()) rows += 1;
    for (const call of (Array.isArray(message.tool_calls) ? message.tool_calls : [])) {
      if (call?.id && !resolvedCalls.has(call.id)) rows += 1;
    }
    return Math.max(rows, 1);
  }

  function _resolvedCallIds(messages) {
    const resolved = new Set();
    for (const message of messages) {
      if (message?.role === "tool" && message.tool_call_id) resolved.add(message.tool_call_id);
    }
    return resolved;
  }

  // 窗口起点必须落在完整的工具调用组上：起点若是工具结果，其调用消息在窗口之外，
  // 首行就成了"没有来源的工具结果"。人类消息、带正文的助手消息、以及工具调用消息都是完整组起点，
  // 只有连续的工具结果需要连同其调用消息一起纳入（长工具批次的回退量是其长度，不随会话长度增长）。
  function _safeStart(list, from) {
    let start = from;
    while (start > 0 && (list[start] || {}).role === "tool") start -= 1;
    return start;
  }

  // 窗口边界同时受两个上限约束：消息条数（既有口径）与**已挂载行数**（真实工作量口径）。
  // 只按消息计会被"助手消息无可见正文"的形态击穿——上百行事件挤在少数几条消息里。
  function windowStartIndex(messages, limit = WINDOW_MESSAGES, rowLimit = WINDOW_ROWS) {
    const list = Array.isArray(messages) ? messages : [];
    const byMessages = limit && limit > 0 && list.length > limit ? list.length - limit : 0;
    let byRows = 0;
    if (rowLimit && rowLimit > 0) {
      const resolved = _resolvedCallIds(list);
      let rows = 0;
      let index = list.length;
      while (index > 0 && rows < rowLimit) {
        index -= 1;
        rows += _messageRowCount(list[index] || {}, resolved);
      }
      byRows = index;
    }
    return _safeStart(list, Math.max(byMessages, byRows));
  }

  function _messagePositions(messages) {
    const positions = new Map();
    for (const [index, message] of (messages || []).entries()) {
      if (message && typeof message === "object") positions.set(message, index);
    }
    return positions;
  }

  function _fallbackIndex(message, countedIndex, state) {
    const absolute = state.messagePositions?.get(message);
    return absolute === undefined ? countedIndex : absolute;
  }

  function _messageUnit(message, roleStructured, countedIndex, input) {
    const fallbackKey = _fallbackIndex(message, countedIndex, input.state);
    const identity = message.id || message.message_id || `${message.role || "message"}:${fallbackKey}`;
    const key = `message:${identity}`;
    const signature = _messageSignature(message, roleStructured, input.state);
    return {
      key,
      html: _unitHtml(key, signature, () => (roleStructured
        ? input.renderMessage(message, { showRoleHeader: true, fallbackKey })
        : input.renderMessage(message, { fallbackKey }))),
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
      html: _unitHtml(key, _join(["earlier", hiddenCount]), () => `<div class="conversation-earlier" data-unit-key="${escapeAttribute(key)}"><button type="button" class="text-button" data-action="load-earlier-conversation">加载更早的 ${hiddenCount} 条消息</button></div>`),
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
      // 序列身份取"在单元序列中的序位"，不取首行键：窗口上边界前移会换掉首行，
      // 取首行键会把同一段执行序列判成新单元并整体重建（既有契约要求身份稳定的单元不因滑动被重建）。
      const key = `seq#${state.sequenceIndex}`;
      state.sequenceIndex += 1;
      state.units.push({
        key,
        html: _unitHtml(key, _eventsSignature(events), () => `<section class="conversation-event-sequence" data-unit-key="${escapeAttribute(key)}" role="group" aria-label="执行过程">${events.map(event => _eventRowHtml(event, input)).join("")}</section>`),
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
    const start = windowStartIndex(allMessages, input.windowLimit ?? WINDOW_MESSAGES, input.windowRows ?? WINDOW_ROWS);
    const visible = start > 0 ? allMessages.slice(start) : allMessages;
    const state = {
      units: [],
      eventIndex: new Map(),
      messageIndex: start,
      sequenceIndex: 0,
      messagePositions: _messagePositions(allMessages),
    };
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
        html: _unitHtml(key, _streamingSignature(runId, buffer), () => `<article class="${STREAMING_PLACEHOLDER_CLASS}" data-stream-run="${runId}">${streamingContent(input.markdown, input.events, buffer)}</article>`),
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

  // 可见正文的体积边界：越限不产出正文块，使"越界或超长的流式正文"不会把单帧变成解析+插入长任务；
  // 缓冲本身不受影响（"快照是否已携带该正文"的判据取的是正文前缀），完成态仍按正式消息呈现。
  function visibleStreamBlocks(markdown, buffer) {
    const text = String(buffer?.text ?? "");
    if (!text || text.length > STREAM_TEXT_LIMIT) return [];
    return streamingBlocks(markdown, buffer);
  }

  function streamingContent(markdown, events, buffer) {
    const blocks = visibleStreamBlocks(markdown, buffer);
    const answer = blocks.length
      ? `<div class="message-rich">${blocks.map(entry => entry.html).join("")}</div>`
      : "";
    return `${STREAMING_HEADER_HTML}${_reasoningSection(events, buffer.reasoning)}${answer}`;
  }

  return {
    WINDOW_MESSAGES,
    WINDOW_ROWS,
    STREAM_TEXT_LIMIT,
    STREAMING_HEADER_HTML,
    STREAMING_PLACEHOLDER_CLASS,
    buildUnits,
    renderConversation,
    markdownBlocks,
    streamingBlocks,
    visibleStreamBlocks,
    streamingContent,
    windowStartIndex,
    stats: snapshotStats,
    resetStats,
  };
});
