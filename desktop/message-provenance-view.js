/*
 * 本文件对外提供 direct/delegated HumanMessage 的用户可见来源审计视图。
 * 输入为模型外 provenance、decision、grant 和 message identity；输出为不改正文的来源标签与详情。
 * 具体工作流为按 message id 外部连接审计事实并单独渲染；示例：`FocusMessageProvenanceView.badge(audit)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusMessageProvenanceView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const escape = value => String(value ?? "").replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  function badge(audit) {
    if (!audit) return '<span class="message-source is-direct">用户直接输入</span>';
    const delegated = audit.source_kind === "delegated_patrol";
    return `<button type="button" class="message-source ${delegated ? "is-delegated" : "is-direct"}" data-provenance-id="${escape(audit.provenance_id || "")}" aria-label="查看消息来源">${delegated ? "Patrol 依据授权生成" : "用户直接输入"}</button>`;
  }
  function details(audit = {}) {
    return `<dl class="provenance-details"><dt>来源</dt><dd>${escape(audit.source_kind || "direct_user")}</dd><dt>Actor</dt><dd>${escape(audit.actor_id || "user")}</dd><dt>Loop / Round</dt><dd>${escape(audit.audit?.loop_id || "—")} / ${escape(audit.audit?.round_id || "—")}</dd><dt>Directive</dt><dd>${escape(audit.directive_id || "—")}</dd></dl>`;
  }
  return Object.freeze({ badge, details });
});
