/*
 * 本文件对外提供 Agent Loop 可追溯事实抽屉、renderRows 与 reconcileRows。
 * 输入为物化事实、当前 Context 和筛选；输出为事实表 HTML，或按 fact_id 原位更新的 DOM 行。
 * 具体工作流为只呈现权威物化 revision；增量更新复用未变化行并原位更新状态，避免事实生命周期变化重建整个表和滚动容器。
 * 示例：`FocusLoopFactsView.reconcileRows(tbody, facts)`。
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

  function rowHtml(item) {
    return `<tr data-fact-id="${escape(item.fact_id)}" data-fact-revision="${escape(item.revision || 1)}" class="is-${escape(item.status)}"><td>${escape(timeLabel(item.occurred_at))}</td><td><strong>${escape(item.context_id)}</strong></td><td><span class="fact-kind">${escape(item.kind)}</span></td><td><strong>${escape(item.title)}</strong><span>${escape(item.summary)}</span></td><td><span class="fact-status is-${escape(item.status)}">${escape(item.status)}</span></td><td><code>${escape(evidenceLabel(item))}</code></td></tr>`;
  }

  function renderRows(items) {
    return items.map(rowHtml).join("") || '<tr data-fact-empty><td colspan="6" class="history-boundary">暂无可验证事实</td></tr>';
  }

  function reconcileRows(tbody, items) {
    if (!tbody || typeof document !== "object") return false;
    const existing = new Map([...tbody.querySelectorAll("tr[data-fact-id]")].map(row => [row.dataset.factId, row]));
    const fragment = document.createDocumentFragment();
    for (const item of items) {
      const signature = JSON.stringify(item);
      let row = existing.get(item.fact_id);
      if (!row) {
        const template = document.createElement("template");
        template.innerHTML = rowHtml(item);
        row = template.content.firstElementChild;
      } else {
        existing.delete(item.fact_id);
        if (row.dataset.factSignature !== signature) {
          const template = document.createElement("template");
          template.innerHTML = rowHtml(item);
          const next = template.content.firstElementChild;
          row.className = next.className;
          row.dataset.factRevision = next.dataset.factRevision;
          row.innerHTML = next.innerHTML;
        }
      }
      row.dataset.factSignature = signature;
      fragment.append(row);
    }
    existing.forEach(row => row.remove());
    tbody.querySelector("[data-fact-empty]")?.remove();
    if (!items.length) {
      const template = document.createElement("template");
      template.innerHTML = renderRows(items);
      fragment.append(template.content.firstElementChild);
    }
    tbody.append(fragment);
    return true;
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
    return `<section class="loop-facts-drawer"><header><div><span class="loop-kicker">Live Facts</span><h3>持续演化的事实</h3></div><div class="fact-totals"><strong>${totals.passed}</strong> passed <strong>${totals.failed}</strong> failed <strong>${totals.skipped}</strong> skipped${totals.unknown ? ` · ${totals.unknown} unknown` : ""}</div><div class="fact-filters"><button type="button" data-action="loop-fact-scope" data-scope="${state.factScope === "all" ? "current" : "all"}">${state.factScope === "all" ? "全部 Context" : "当前 Context"}</button>${filters.map(value => `<button type="button" data-action="loop-fact-filter" data-filter="${value}" class="${state.factFilter === value ? "is-active" : ""}">${value}</button>`).join("")}${statuses.map(([value, label]) => `<button type="button" data-action="loop-fact-status" data-status="${value}" class="${factStatus === value ? "is-active" : ""}">${label}</button>`).join("")}</div></header><div class="fact-table-scroll" data-loop-fact-list>${older}<table class="fact-table"><thead><tr><th>时间</th><th>Context</th><th>类型</th><th>事实内容</th><th>状态</th><th>证据</th></tr></thead><tbody>${renderRows(filtered)}</tbody></table></div></section>`;
  }

  return Object.freeze({ render, renderRows, reconcileRows });
});
