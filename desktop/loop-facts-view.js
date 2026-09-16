/*
 * 本文件对外提供 Agent Loop 可追溯事实抽屉。
 * 输入为 Run、Workspace、Artifact、Tool、Test 事实、当前 Context 与类型筛选；输出为数量摘要、可筛选事实表和证据定位信息。
 * 具体工作流为仅展示权威执行记录与保守解析结果，按时间逐行关联 Context/Run/消息证据，失败和未知测试状态均按事实契约明确标记。
 * 示例：`FocusLoopFactsView.render(consoleState)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopFactsView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);

  function evidenceLabel(item) {
    return item.evidence?.run_id || item.evidence?.tool_name || item.evidence?.message_id || "evidence";
  }

  function timeLabel(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function render(state) {
    const all = state.facts?.facts || [];
    const factStatus = state.factStatus || "all";
    const filtered = all.filter(item => (state.factFilter === "all" || item.kind === state.factFilter) && (factStatus === "all" || item.status === factStatus) && (state.factScope === "all" || !state.selectedContextId || item.context_id === state.selectedContextId));
    const tests = filtered.filter(item => item.kind === "test");
    const totals = tests.reduce((result, item) => {
      if (item.metrics?.count_status !== "exact") result.unknown += 1;
      else ["passed", "failed", "skipped"].forEach(key => { result[key] += Number(item.metrics[key] || 0); });
      return result;
    }, { passed: 0, failed: 0, skipped: 0, unknown: 0 });
    const filters = ["all", "run", "test", "tool", "workspace", "artifact"];
    const statuses = [["all", "全部状态"], ["failed", "失败"], ["unknown", "未知"]];
    const older = state.facts?.has_more
      ? '<div class="fact-sentinel" data-loop-fact-sentinel aria-hidden="true"></div><button type="button" class="load-facts" data-action="loop-load-older-facts">加载更早事实</button>'
      : "";
    const rows = filtered.map(item => `<tr class="is-${escape(item.status)}"><td>${escape(timeLabel(item.occurred_at))}</td><td><strong>${escape(item.context_id)}</strong></td><td><span class="fact-kind">${escape(item.kind)}</span></td><td><strong>${escape(item.title)}</strong><span>${escape(item.summary)}</span></td><td><span class="fact-status is-${escape(item.status)}">${escape(item.status)}</span></td><td><code>${escape(evidenceLabel(item))}</code></td></tr>`).join("");
    return `<section class="loop-facts-drawer"><header><div><span class="loop-kicker">Verified Facts</span><h3>已验证的事实</h3></div><div class="fact-totals"><strong>${totals.passed}</strong> passed <strong>${totals.failed}</strong> failed <strong>${totals.skipped}</strong> skipped${totals.unknown ? ` · ${totals.unknown} unknown` : ""}</div><div class="fact-filters"><button type="button" data-action="loop-fact-scope" data-scope="${state.factScope === "all" ? "current" : "all"}">${state.factScope === "all" ? "全部 Context" : "当前 Context"}</button>${filters.map(value => `<button type="button" data-action="loop-fact-filter" data-filter="${value}" class="${state.factFilter === value ? "is-active" : ""}">${value}</button>`).join("")}${statuses.map(([value, label]) => `<button type="button" data-action="loop-fact-status" data-status="${value}" class="${factStatus === value ? "is-active" : ""}">${label}</button>`).join("")}</div></header><div class="fact-table-scroll" data-loop-fact-list>${older}<table class="fact-table"><thead><tr><th>时间</th><th>Context</th><th>类型</th><th>事实内容</th><th>状态</th><th>证据</th></tr></thead><tbody>${rows || '<tr><td colspan="6" class="history-boundary">暂无可验证事实</td></tr>'}</tbody></table></div></section>`;
  }

  return Object.freeze({ render });
});
