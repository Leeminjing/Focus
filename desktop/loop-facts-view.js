/*
 * 本文件对外提供 Agent Loop 可追溯事实抽屉、renderRows、reconcileRows、共用详情及验证状态/业务结果筛选选项。
 * 输入为物化事实、当前 Context 和筛选；输出为事实表 HTML，或按 fact_id 原位更新的 DOM 行。
 * 具体工作流为只呈现领域 revision 并排除历史 tool 条目；未变行不移出 tbody、不改写签名，按实际顺序原位更新状态；稳定 fact_id 来源按钮进入真实证据检查。
 * 示例：`FocusLoopFactsView.reconcileRows(tbody, facts)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopFactsView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);

  const statusOptions = [["", "全部事实状态"], ["observed", "已观察"], ["verifying", "验证中"], ["verified", "已验证"], ["contradicted", "已被反证"], ["superseded", "已替代"]];
  const outcomeOptions = [["", "全部业务结果"], ["success", "成功"], ["failed", "失败"], ["unknown", "未知"]];
  const factStates = Object.fromEntries(statusOptions);
  const outcomeStates = Object.fromEntries(outcomeOptions);
  function evidenceLabel(item) {
    return item.run_id || item.evidence?.[0]?.run_id || item.evidence?.run_id || item.evidence?.tool_name || item.evidence?.message_id || "evidence";
  }

  function timeLabel(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function rowHtml(item) {
    return `<tr data-fact-id="${escape(item.fact_id)}" data-fact-revision="${escape(item.revision || 1)}" class="is-${escape(item.status)}"><td>${escape(timeLabel(item.occurred_at))}</td><td><strong>${escape(item.context_id)}</strong></td><td><span class="fact-kind">${escape(item.kind)}</span></td><td><strong>${escape(item.title)}</strong><span>${escape(item.summary)}</span></td><td><span class="fact-status is-${escape(item.status)}">${escape(factStates[item.status] || item.status)}</span>${item.outcome_status ? `<small>${escape(outcomeStates[item.outcome_status] || item.outcome_status)}</small>` : ""}</td><td><button class="text-button" data-action="inspect-loop-fact" data-fact-id="${escape(item.fact_id)}">${escape(evidenceLabel(item))} ↗</button></td></tr>`;
  }

  function renderRows(items) {
    return items.filter(item => item.kind !== "tool").map(rowHtml).join("") || '<tr data-fact-empty><td colspan="6" class="history-boundary">暂无可验证事实</td></tr>';
  }

  function reconcileRows(tbody, items) {
    items = items.filter(item => item.kind !== "tool");
    if (!tbody || typeof document !== "object") return false;
    const existing = new Map([...tbody.querySelectorAll("tr[data-fact-id]")].map(row => [row.dataset.factId, row]));
    const focusId = tbody.contains(document.activeElement) ? document.activeElement.dataset.factId : null;
    let previous = null;
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
      if (row.dataset.factSignature !== signature) row.dataset.factSignature = signature;
      const reference = previous ? previous.nextSibling : tbody.firstChild;
      if (row !== reference) tbody.insertBefore(row, reference);
      previous = row;
    }
    existing.forEach(row => row.remove());
    if (items.length) tbody.querySelector("[data-fact-empty]")?.remove();
    if (!items.length && !tbody.querySelector("[data-fact-empty]")) {
      const template = document.createElement("template");
      template.innerHTML = renderRows(items);
      tbody.append(template.content.firstElementChild);
    }
    const target = focusId ? tbody.querySelector(`button[data-fact-id="${CSS.escape(focusId)}"]`) : null;
    if (target && document.activeElement !== target) target.focus({ preventScroll: true });
    return true;
  }

  function render(state) {
    const all = (state.facts?.facts || []).filter(item => item.kind !== "tool");
    const factStatus = state.factStatus || "all";
    const filtered = all.filter(item => (state.factFilter === "all" || item.kind === state.factFilter) && (factStatus === "all" || item.status === factStatus || item.outcome_status === factStatus) && (state.factScope === "all" || !state.selectedContextId || item.context_id === state.selectedContextId));
    const tests = filtered.filter(item => item.kind === "test");
    const totals = tests.reduce((result, item) => {
      if (item.metrics?.count_status !== "exact") result.unknown += 1;
      else ["passed", "failed", "skipped"].forEach(key => { result[key] += Number(item.metrics[key] || 0); });
      return result;
    }, { passed: 0, failed: 0, skipped: 0, unknown: 0 });
    const filters = ["all", "run", "test", "workspace", "artifact", "context_revision", "directive"];
    const statuses = [["all", "全部状态"], ["failed", "失败"], ["unknown", "未知"]];
    const older = state.facts?.has_more
      ? '<div class="fact-sentinel" data-loop-fact-sentinel aria-hidden="true"></div><button type="button" class="load-facts" data-action="loop-load-older-facts">加载更早事实</button>'
      : "";
    return `<section class="loop-facts-drawer"><header><div><span class="loop-kicker">Live Facts</span><h3>持续演化的事实</h3></div><div class="fact-totals"><strong>${totals.passed}</strong> passed <strong>${totals.failed}</strong> failed <strong>${totals.skipped}</strong> skipped${totals.unknown ? ` · ${totals.unknown} unknown` : ""}</div><div class="fact-filters"><button type="button" data-action="loop-fact-scope" data-scope="${state.factScope === "all" ? "current" : "all"}">${state.factScope === "all" ? "全部 Context" : "当前 Context"}</button>${filters.map(value => `<button type="button" data-action="loop-fact-filter" data-filter="${value}" class="${state.factFilter === value ? "is-active" : ""}">${value}</button>`).join("")}${statuses.map(([value, label]) => `<button type="button" data-action="loop-fact-status" data-status="${value}" class="${factStatus === value ? "is-active" : ""}">${label}</button>`).join("")}</div></header><div class="fact-table-scroll" data-loop-fact-list>${older}<table class="fact-table"><thead><tr><th>时间</th><th>Context</th><th>类型</th><th>事实内容</th><th>状态</th><th>证据</th></tr></thead><tbody>${renderRows(filtered)}</tbody></table></div></section>`;
  }

  function renderDetail(detail) {
    const fact = detail.fact || {};
    const metrics = fact.metrics || {};
    return `<section class="fact-inspection"><h3>${escape(fact.title || fact.kind)}</h3><p>${escape(fact.summary)}</p><p>事实状态：${escape(factStates[fact.status] || fact.status)} · 业务结果：${escape(outcomeStates[fact.outcome_status] || fact.outcome_status || "未知")}</p>${fact.kind === "test" ? `<p>${metrics.count_status === "exact" ? `${escape(metrics.passed ?? 0)} passed · ${escape(metrics.failed ?? 0)} failed · ${escape(metrics.skipped ?? 0)} skipped` : "测试数量尚未明确，不计为成功"}</p>` : ""}<h3>事实修订</h3><ul>${(detail.revisions || []).map(row => `<li>版本 ${row.revision} · ${escape(factStates[row.state] || row.state)}<p>${escape(row.reason || row.presentation?.summary)}</p></li>`).join("") || "<li>暂无修订历史</li>"}</ul><details><summary>证据与精确来源</summary><pre>${escape(JSON.stringify({ fact_id: fact.fact_id, context_id: fact.context_id, run_id: fact.run_id, evidence: fact.evidence, relationships: detail.relationships }, null, 2))}</pre></details>${fact.evidence ? "" : "<p>证据正文未在当前权限下返回。</p>"}</section>`;
  }

  return Object.freeze({ render, renderRows, reconcileRows, renderDetail, statusOptions, outcomeOptions });
});
