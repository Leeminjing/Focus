/*
 * 关键字快捷压缩命令的唯一语法入口。浏览器通过 FocusKeywordCommand 使用，
 * Node 测试通过 CommonJS 使用；不读写 DOM，也不改变普通消息发送语义。
 */
(function initKeywordCommand(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusKeywordCommand = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createKeywordCommand() {
  "use strict";

  const COMMAND = "@关键字快捷压缩";
  const COMMAND_RE = /^@关键字快捷压缩\s+(.+)$/;
  const HIGHLIGHT_RE = /^@关键字快捷压缩(?=\s)/;

  function parse(value) {
    const match = String(value ?? "").trim().match(COMMAND_RE);
    if (!match) return null;
    const keyword = match[1].trim();
    return keyword || null;
  }

  function highlightHtml(value, escapeHtml) {
    const escaped = escapeHtml(String(value ?? ""));
    return escaped.replace(HIGHLIGHT_RE, match => `<span class="cmd-at">${match}</span>`);
  }

  return { COMMAND, parse, highlightHtml };
});
