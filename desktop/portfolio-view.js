/*
 * 本文件对外提供 Context Portfolio、Lane 与 generation 差异视图。
 * 输入为 Portfolio revisions、Lane 状态、来源 frontier、Run 与 adoption 状态；输出为可比较、可导航的安全 HTML。
 * 具体工作流为归一 Lane 身份、计算 create/update/keep/pause/retire 并关联当前执行与物理结果；
 * 示例：`FocusPortfolioView.render(snapshot)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusPortfolioView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const escape = value => String(value ?? "").replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  function diff(previous = {}, current = {}) {
    const before = new Map((previous.lanes || []).map(lane => [lane.lane_id, lane]));
    return (current.lanes || []).map(lane => ({ ...lane, change: lane.action || (!before.has(lane.lane_id) ? "create" : before.get(lane.lane_id).revision_id === lane.revision_id ? "keep" : "update") }));
  }
  function render(snapshot, previous = null) {
    const lanes = diff(previous || {}, snapshot || {});
    return `<section class="portfolio-view"><header><h3>Context Portfolio</h3><span>Generation ${escape(snapshot?.generation || 0)}</span></header><div class="portfolio-lanes">${lanes.map(lane => `<article class="portfolio-lane is-${escape(lane.change)}"><header><strong>${escape(lane.purpose)}</strong><span>${escape(lane.change)}</span></header><p>${escape(lane.summary || "尚无摘要")}</p><dl><dt>Revision</dt><dd>${escape(lane.revision_id || "—")}</dd><dt>Freshness</dt><dd>${escape(lane.freshness || "unknown")}</dd><dt>Sources</dt><dd>${escape((lane.source_frontier || []).length)}</dd><dt>Run</dt><dd>${escape(lane.run_status || "idle")}</dd><dt>Workspace</dt><dd>${escape(lane.adoption_state || "authoritative")}</dd></dl></article>`).join("") || "<p>暂无 Lane</p>"}</div></section>`;
  }
  return Object.freeze({ diff, render });
});
