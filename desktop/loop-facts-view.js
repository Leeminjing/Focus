/*
 * 本文件对外提供 Agent Loop 可追溯事实抽屉。
 * 输入为 Run、Workspace、Artifact、Tool、Test 事实、当前 Context 与类型筛选；输出为数量摘要、事实清单和证据定位信息。
 * 具体工作流为仅展示权威执行记录与保守解析结果，失败和未知测试状态均按事实契约明确标记。
 * 示例：`FocusLoopFactsView.render(consoleState)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopFactsView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);

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
    return `<section class="loop-facts-drawer"><header><div><span class="loop-kicker">Verified Facts</span><h3>${state.factScope === "all" ? "所有 Context 做过什么" : "该 Context 做过什么"}</h3></div><div class="fact-totals"><strong>${totals.passed}</strong> passed <strong>${totals.failed}</strong> failed <strong>${totals.skipped}</strong> skipped${totals.unknown ? ` · ${totals.unknown} unknown` : ""}</div><div class="fact-filters"><button type="button" data-action="loop-fact-scope" data-scope="${state.factScope === "all" ? "current" : "all"}">${state.factScope === "all" ? "只看当前" : "查看全部"}</button>${filters.map(value => `<button type="button" data-action="loop-fact-filter" data-filter="${value}" class="${state.factFilter === value ? "is-active" : ""}">${value}</button>`).join("")}${statuses.map(([value, label]) => `<button type="button" data-action="loop-fact-status" data-status="${value}" class="${factStatus === value ? "is-active" : ""}">${label}</button>`).join("")}</div></header><div class="fact-list" data-loop-fact-list>${older}${filtered.map(item => `<article class="loop-fact is-${escape(item.status)}"><span class="fact-kind">${escape(item.kind)}</span><div><strong>${escape(item.title)}</strong><p>${escape(item.summary)}</p><small>${escape(item.context_id)} · ${escape(item.evidence?.run_id || item.evidence?.tool_name || item.evidence?.message_id || "evidence")} · ${escape(item.occurred_at || "")}</small></div></article>`).join("") || '<p class="history-boundary">暂无可验证事实</p>'}</div></section>`;
  }

  return Object.freeze({ render });
});
