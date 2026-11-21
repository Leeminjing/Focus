/* 本文件提供普通会话和 Patrol 共用的只读历史适配。
 * 输入为历史消息，输出为可读正文、工具降级包装和递归压缩来源；不生成编辑计划或改写输入。
 * 协议占位不显示，有来源的策展总结与历史图片保持可读。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusConversationHistory = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

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

  return Object.freeze({ messageText, degradedParts, isProtocolPlaceholder, expandForConversation });
});
