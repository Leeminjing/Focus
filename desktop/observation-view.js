/* 本文件对外提供冻结 Observation 的摘要与分页检查 HTML。
 * 输入为同一 Observation 白名单响应和已读取 section；输出为人类可读依据、独立前序与折叠精确身份。
 * 具体工作流为只按冻结内容呈现来源/状态和分页，分别显示来源、前序历史、关系覆盖与分页完整性，保留 legacy/权限边界，不以实时状态填充历史。
 * 示例：FocusObservationView.render(summary, { sources: page })。
 */
(function (root) {
  "use strict";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const short = value => escape(value ? String(value).slice(0, 10) : "不可用");
  const sourceNames = { user_revision: "用户要求", run_outcome: "执行结果", test: "测试事实", artifact: "产物", workspace: "工作区结果" };
  const states = { completed: "已验证", in_progress: "进行中", not_started: "待开展", blocked: "阻塞", not_applicable: "被替代", unknown: "待判断", conflicted: "存在冲突", success: "已确认", failed: "失败" };
  function time(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? escape(value) : escape(date.toLocaleString([], { hour12: false }));
  }
  function identities(item) {
    const entries = Object.entries(item).filter(([key]) => key !== "evidence");
    return `<details class="observation-audit"><summary>精确身份与版本</summary><dl>${entries.map(([key, value]) => `<div><dt>${escape(key)}</dt><dd>${escape(typeof value === "object" ? JSON.stringify(value) : value)}</dd></div>`).join("")}</dl></details>`;
  }
  function itemView(item, section) {
    if (section === "previous_progress") return `<li><strong>${escape(item.description || item.item_id)}</strong><p>${states[item.state] || escape(item.state)} · ${item.support === "supported" ? "有证据" : item.support === "asserted" ? "已报告" : "待验证"}</p>${item.blockers?.length ? `<p>${escape(item.blockers.join("；"))}</p>` : ""}${identities(item)}</li>`;
    if (section === "lineage") return `<li><strong>${item.kind === "edge" ? `${short(item.source_context_id)} → ${short(item.target_context_id)}` : `Context ${short(item.context_id)} · R${escape(item.generation)}`}</strong>${identities(item)}</li>`;
    const evidence = item.evidence;
    const metrics = evidence?.metrics;
    return `<li><strong>${sourceNames[item.kind] || escape(item.kind)} · ${short(item.source_id)}</strong>${evidence ? `<p>${escape(evidence.summary || evidence.title || states[evidence.status] || evidence.status || "保留原来源状态")}</p>${metrics ? `<p>${metrics.count_status === "exact" ? `${escape(metrics.passed ?? 0)} passed · ${escape(metrics.failed ?? 0)} failed · ${escape(metrics.skipped ?? 0)} skipped` : "测试数量尚未明确"}</p>` : ""}` : "<p>证据正文未在当前权限下返回。</p>"}${identities(item)}</li>`;
  }
  function coverage(summary) {
    const fields = [["冻结来源覆盖", summary.sources?.complete], ["前序历史覆盖", summary.previous_progress?.history_complete], ["来源关系覆盖", summary.lineage?.complete]];
    return `<p class="observation-coverage">${fields.map(([label, value]) => `${label}：${value === true ? "完整" : value === false ? "不完整" : "未确认"}`).join(" · ")}</p>`;
  }
  function card(summary) {
    if (!summary) return "本轮尚未冻结";
    return `<h3>Round ${escape(summary.round_number ?? "—")} 看到了什么</h3><p>本轮冻结 ${summary.sources.total} 个任务来源与 ${summary.lineage.node_count} 个 Revision。</p><small>基于前序 ${short(summary.previous_progress.progress_id)} · ${time(summary.frozen_at)}</small>${coverage(summary)}`;
  }
  function render(summary, sections = {}) {
    if (!summary) return "尚未取得冻结输入";
    const titles = { previous_progress: "本轮前序 Task Progress", sources: "本轮冻结来源", lineage: "精确的 Context frontier" };
    return `<div class="observation-inspection"><div class="observation-meta"><span>不可变快照 · Round ${escape(summary.round_number ?? "—")}</span><span>冻结于 ${time(summary.frozen_at)}</span></div><h3 class="observation-question">这一轮，基于什么作决定？</h3><p>本轮观察与上一轮进度一起进入决策。新的表达仍可随时提交，但不会改写已经冻结的输入。</p>${coverage(summary)}${summary.availability === "legacy" ? '<p role="status">旧记录没有完整冻结读面；不使用当前 Progress 回填。</p>' : ""}<div class="observation-identities"><section><h3>上一轮 Task Progress</h3><strong>${short(summary.previous_progress.progress_id)}</strong><p>${summary.previous_progress.item_count} 个冻结条目</p>${summary.previous_progress.progress_id ? `<button data-patrol-observation-progress="${escape(summary.previous_progress.progress_id)}">检查本轮前序版本 ↗</button>` : ""}</section><section><h3>本轮 Observation</h3><strong>${short(summary.observation_id)}</strong><p>${summary.sources.total} 个任务来源 · ${summary.lineage.node_count} 个 Revision</p></section></div><p class="observation-frontier">精确 Context frontier · ${summary.lineage.node_count} 个冻结 Revision · ${summary.lineage.edge_count ?? 0} 条来源关系</p>${!summary.evidence_visible ? '<p>当前权限不包含证据正文，仅显示允许的身份与状态。</p>' : ""}${Object.entries(titles).map(([key, title]) => {
      const page = sections[key];
      return `<section><h3>${title}</h3>${page ? `<p>当前冻结清单已读取 ${page.items.length} / ${page.total} 项${page.has_more ? " · 仍有后续" : ""}</p><ul class="observation-source-list">${page.items.map(item => itemView(item, key)).join("") || "<li>此冻结范围没有条目</li>"}</ul>${page.has_more ? `<button data-patrol-observation-section="${key}">加载下一页</button>` : ""}` : `<button data-patrol-observation-section="${key}">读取冻结条目</button>`}</section>`;
    }).join("")}${identities({ observation_id: summary.observation_id, observation_hash: summary.observation_hash, inputs_hash: summary.inputs_hash, previous_progress_id: summary.previous_progress.progress_id, previous_progress_hash: summary.previous_progress.content_hash, goal_revision: summary.goal_revision, authority_revision: summary.authority_revision })}<p>Observation 是本轮冻结输入，LoopFact 是持续更新的事实记录。</p></div>`;
  }
  root.FocusObservationView = Object.freeze({ card, render });
  if (typeof module === "object" && module.exports) module.exports = root.FocusObservationView;
})(globalThis);
