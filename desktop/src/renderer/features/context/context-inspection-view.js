/*
 * 本文件对外提供 Patrol Context 的固定 Revision 只读会话检查。
 * 输入为分页会话与固定 Revision，输出为安全 Markdown 正文、工具及来源审计。
 * 具体工作流为复用共享消息阅读器，压缩来源只读展示，历史页通过 Patrol 检查器读取。
 * 示例：FocusContextInspectionView.renderInspection(conversation)。
 */
(function (root, factory) {
  const api = factory(typeof module === "object" && module.exports ? require("../../shared/markdown/message-reading.js") : root.FocusMessageReading);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusContextInspectionView = api;
})(typeof globalThis === "object" ? globalThis : this, function (reading) {
  "use strict";
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }


  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const role = message => String(message?.role || message?.type || "message").toLowerCase();

  function messageCard(item, revision = null) {
    const message = item.message || {};
    const itemRole = role(message);
    const provenance = item.provenance;
    const compression = message.compression || message.additional_kwargs?.compression;
    const compressed = compression ? localize`<details class="compression-audit"><summary>${compression.deleted ? localize("已删除块") : localize("压缩块")} · 原始来源可恢复</summary><pre>${escape(JSON.stringify(compression.source || [], null, 2))}</pre></details>` : "";
    const fileContext = message.file_reference || message.file_context || { workspace_id: message.workspace_id, run_id: message.run_id, slot_id: message.slot_id };
    return `<article class="loop-message is-${escape(itemRole)}" data-message-index="${escape(item.index)}" data-message-role="${escape(itemRole)}" data-file-task-id="${escape(message.task_id || item.context_id || "")}" data-file-context="${escape(JSON.stringify(fileContext))}"><header><strong>${escape(itemRole)}</strong><span>#${item.index + 1}</span></header><div class="loop-message-content">${reading.render(message, { details: true, provenance: provenance || (["human", "user"].includes(itemRole) ? { source_kind: "direct_human" } : null), revision })}${compressed}</div></article>`;
  }

  function renderInspection(conversation) {
    const messages = conversation?.messages || [];
    return localize`<section class="context-conversation is-inspection"><p>Context 检查 · 固定 Revision · ${escape(conversation?.total || 0)} 条消息</p><div class="loop-transcript">${messages.map(item => messageCard({ ...item, context_id: conversation.context_id || item.context_id }, conversation.revision)).join("") || localize("暂无消息")}</div>${conversation?.has_more ? localize`<button data-patrol-context-older>加载更早消息</button>` : ""}</section>`;
  }

  return Object.freeze({ renderInspection });
});
