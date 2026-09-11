(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusCompressionPanel = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  // token 估算：与后端 focus/agents/compression/tokens.py 同公式（CJK=1、其余÷4、
  // 每条消息 +12），仅用于面板 Before/After 展示；触发判定由后端以同一公式执行。
  function estimateRawTokens(raw, messageCount) {
    let cjk = 0;
    let other = 0;
    for (const char of String(raw)) {
      const code = char.codePointAt(0);
      if (code >= 0x3400 && code <= 0x9fff) cjk += 1;
      else if (!/\s/.test(char)) other += 1;
    }
    return cjk + Math.floor((other + 3) / 4) + 12 * (messageCount + 2);
  }

  function messageText(message) {
    const content = message && typeof message === "object" ? message.content : message;
    if (typeof content === "string") return content;
    if (Array.isArray(content)) {
      return content
        .map(item => (typeof item === "string" ? item : item?.text || ""))
        .join("");
    }
    return String(content ?? "");
  }

  function estimateMessagesTokens(messages) {
    const raw = (Array.isArray(messages) ? messages : []).map(messageText).join("\n");
    return estimateRawTokens(raw, messages.length);
  }

  // 选择完全自由：单条切换，不联动、不锁定、不拒绝（拆散 tool-call 组由后端
  // _repair_protocol 兜底修复，保证最终 messages 协议合法）。
  function toggleSelect(selected, index) {
    const next = new Set(selected);
    if (next.has(index)) next.delete(index);
    else next.add(index);
    return next;
  }

  // 连续选中段 = 压缩范围：按消息序号排序后切分为连续段
  function selectionRanges(selected) {
    const sorted = [...selected].sort((a, b) => a - b);
    const ranges = [];
    let start = null;
    let prev = null;
    for (const index of sorted) {
      if (start === null) { start = index; prev = index; continue; }
      if (index === prev + 1) { prev = index; continue; }
      ranges.push({ start, end: prev });
      start = index; prev = index;
    }
    if (start !== null) ranges.push({ start, end: prev });
    return ranges;
  }

  // 按计划重建消息列表（预览用）：与后端 _apply_plan 同语义——restore 原位展开来源原文、
  // 压缩范围在首条来源位置替换为块、delete 范围直接删除、其余消息原样。
  function planAfterMessages(messages, ranges) {
    const byId = new Map((Array.isArray(messages) ? messages : []).map(m => [m.id, m]));
    const removed = new Set();
    const restoreMap = new Map();
    const blockByFirst = new Map();
    (Array.isArray(ranges) ? ranges : []).forEach(range => {
      (range.source_ids || []).forEach(id => removed.add(id));
      if (range.restore) {
        (range.source_ids || []).forEach(id => {
          const block = byId.get(id);
          restoreMap.set(id, block?.compression?.source || []);
        });
        return;
      }
      if (range.delete) {
        // 删除范围在后端表现为墓碑块（模型不可见、来源保留）：预览同步
        blockByFirst.set((range.source_ids || [])[0], {
          role: "human",
          content: "",
          compression: { block_id: "preview", deleted: true },
        });
        return;
      }
      blockByFirst.set((range.source_ids || [])[0], {
        role: "human",
        content: range.replacement,
        compression: { block_id: "preview" },
      });
    });
    const rebuilt = [];
    for (const message of (Array.isArray(messages) ? messages : [])) {
      if (restoreMap.has(message.id)) rebuilt.push(...restoreMap.get(message.id));
      else if (blockByFirst.has(message.id)) rebuilt.push(blockByFirst.get(message.id));
      else if (removed.has(message.id)) continue;
      else rebuilt.push(message);
    }
    return rebuilt;
  }

  function beforeAfter(messages, ranges) {
    const after = planAfterMessages(messages, ranges);
    const beforeTokens = estimateMessagesTokens(messages);
    const afterTokens = estimateMessagesTokens(after);
    const mapping = (Array.isArray(ranges) ? ranges : []).map(range => {
      const indexes = (range.source_ids || [])
        .map(id => (Array.isArray(messages) ? messages : []).findIndex(m => m.id === id))
        .filter(index => index >= 0);
      const label = indexes.length
        ? `消息 ${Math.min(...indexes) + 1}~${Math.max(...indexes) + 1}`
        : "已移除范围";
      return {
        label,
        sourceIds: range.source_ids || [],
        restore: Boolean(range.restore),
        delete: Boolean(range.delete),
      };
    });
    return {
      beforeCount: (Array.isArray(messages) ? messages : []).length,
      afterCount: after.length,
      beforeTokens,
      afterTokens,
      mapping,
    };
  }

  // 后端兜底降级消息（<focus-degraded-message>）解析：返回 {name, content} 或 null，
  // 供对话区按"工具结果"样式渲染而非泄露原始 XML 标签。
  const DEGRADED_RE = /^<focus-degraded-message role="tool" name="([^"]*)">\n([\s\S]*?)\n<\/focus-degraded-message>$/;

  function degradedParts(message) {
    const content = messageText(message);
    const match = typeof content === "string" ? content.match(DEGRADED_RE) : null;
    if (!match) return null;
    return { name: match[1], content: match[2] };
  }

  function isProtocolPlaceholder(message) {
    return Boolean(
      message?.curation_synthetic
      && !(Array.isArray(message.curation_source_message_ids)
        && message.curation_source_message_ids.length),
    );
  }

  // 对话区渲染展开：压缩块原位展开为来源原文（递归），并在块位置插入分界标记；
  // 只跳过执行协议占位；带来源证据的策展总结属于可见正文。
  function expandForConversation(messages) {
    const flat = [];
    const walk = (items, depth) => {
      for (const message of (Array.isArray(items) ? items : [])) {
        if (!message || isProtocolPlaceholder(message)) continue;
        const block = message.compression;
        if (block && Array.isArray(block.source)) {
          flat.push({
            divider: true,
            depth,
            block_id: block.block_id || message.id || null,
            summary: messageText(message),
            count: block.source.length,
            deleted: Boolean(block.deleted),
            // 来源原文：调用方据此还原被压缩内容里的图片材料缩略图（f37）
            source: block.source,
          });
          walk(block.source, depth + 1);
          continue;
        }
        flat.push(message);
      }
    };
    walk(messages, 0);
    return flat;
  }

  // 关键字快捷压缩：与后端 focus/agents/compression/keyword.py 同语义——
  // 子串匹配命中消息 id（默认大小写不敏感），可再组装为压缩范围。
  function hitKeywordMessageIds(messages, keyword, caseSensitive) {
    const needle = caseSensitive ? String(keyword) : String(keyword).toLowerCase();
    const hits = [];
    for (const message of (Array.isArray(messages) ? messages : [])) {
      const id = message && typeof message === "object" ? message.id : null;
      if (!id) continue;
      const text = messageText(message);
      const haystack = caseSensitive ? text : text.toLowerCase();
      if (needle && haystack.includes(needle)) hits.push(id);
    }
    return hits;
  }

  // 与后端 build_keyword_ranges 同语义：groupAll 合并为单条范围，否则每 id 一条。
  function buildKeywordRanges(ids, groupAll) {
    const unique = [...new Set(Array.isArray(ids) ? ids : [])];
    if (!unique.length) return [];
    if (groupAll) return [{ source_ids: unique }];
    return unique.map(id => ({ source_ids: [id] }));
  }

  return {
    estimateRawTokens,
    estimateMessagesTokens,
    messageText,
    degradedParts,
    isProtocolPlaceholder,
    toggleSelect,
    selectionRanges,
    planAfterMessages,
    beforeAfter,
    expandForConversation,
    hitKeywordMessageIds,
    buildKeywordRanges,
  };
});
