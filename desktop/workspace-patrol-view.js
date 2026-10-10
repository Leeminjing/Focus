/* 本文件对外提供 Patrol 的输入、按需详情、历史、进度及只读 Context HTML。
 * 输入为已绑定工作区与真实查询/Live 投影；输出为转义的独立区域。
 * 具体工作流为默认只呈现 composer，详情复用图/事实/会话；检查与输入身份分离。
 * 示例：host.innerHTML = FocusWorkspacePatrolView.skeleton(workspace)。
 */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusWorkspacePatrolView = api;
})(globalThis, function (root) {
  "use strict";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const labels = { information: "普通信息", outcome: "最终结果", boundary: "执行边界", completion_check: "完成检查" };
  function composer(workspace) {
    return `<form class="glass-surface" data-patrol-composer><div class="patrol-composer-identity"><strong>◎ Patrol</strong><span>整个工作区</span><button type="button" data-action="show-patrol" data-patrol-switch>▢ ${escape(workspace?.display_name || "选择工作区")}</button></div>
      <textarea data-patrol-content rows="3" aria-label="向 Patrol 输入信息" placeholder="说说你的想法，剩下的交给 Patrol。"${workspace ? "" : " disabled"}></textarea>
      <p data-patrol-error role="alert" hidden></p><div class="patrol-composer-footer"><div><select data-patrol-type aria-label="输入类型">${Object.entries(labels).map(([key, label]) => `<option value="${key}">${label}</option>`).join("")}</select><span data-patrol-target></span><button type="button" data-patrol-clear-target hidden>改为主动输入</button></div>
      <div><span data-patrol-receipt role="status"></span><button type="button" data-patrol-pending hidden>待处理</button><button type="button" data-patrol-live-retry hidden>重连工作状态</button><button type="button" data-patrol-history-open>输入记录</button><button type="button" data-patrol-details aria-expanded="false" aria-controls="patrolWorkDetails">查看工作详情 ↗</button><button type="submit" aria-label="发送给工作区 Patrol"${workspace ? "" : " disabled"}>↑</button></div></div><small data-patrol-state role="status">${workspace ? "正在读取工作状态" : "先选择工作区文件夹，确认后即可输入"}</small></form>`;
  }
  function skeleton(workspace) {
    return `<section class="workspace-patrol is-quiet"><div id="patrolWorkDetails" data-patrol-details-content hidden inert><header class="patrol-page-heading"><div><small>${escape(workspace.display_name)} / Patrol</small><h1>Patrol</h1><p>一个入口，持续推进。</p></div><button data-patrol-details aria-expanded="false" aria-controls="patrolWorkDetails">收起详情 ↙</button><details class="patrol-control-menu"><summary>工作控制</summary><button data-patrol-control="pause">暂停执行</button><button data-patrol-control="resume">继续执行</button><button data-patrol-control="stop">停止执行</button><button data-patrol-grant-open>授权与预算</button><button data-patrol-restart hidden>重新启用</button></details></header>
      <div class="patrol-observations"><section class="patrol-graph-card"><header><h2>上下文集合</h2><button data-action="show-map">打开全图 ↗</button></header><p>独立推进，在需要时交汇。</p><div data-patrol-lineage>首线程尚未提交</div></section><section class="patrol-progress-card"><header><h2>Task Progress</h2><button data-patrol-progress-open aria-label="检查已提交进度">↗</button></header><div data-patrol-progress>尚无已提交进度</div></section><section class="patrol-observation-card"><header><h2>Observation</h2><span>本轮冻结</span></header><div data-patrol-observation>尚未冻结</div><button data-patrol-observation-open disabled>检查本轮依据 →</button></section><section class="patrol-facts-card"><header><h2>LoopFact</h2><span>工作留下的实时事实</span><button data-patrol-facts-open>全部事实 ↗</button></header><div class="patrol-facts"><table><thead><tr><th>时间</th><th>Context</th><th>类型</th><th>事实</th><th>状态</th><th>来源</th></tr></thead><tbody data-patrol-facts></tbody></table></div><small>当前 Live 窗口，完整历史可按需读取</small></section></div></div>
      ${composer(workspace)}<aside data-patrol-context class="patrol-context-drawer" aria-label="只读 Context 检查" hidden><div data-patrol-context-content></div></aside>
      <dialog data-patrol-dialog class="patrol-dialog glass-surface" aria-labelledby="patrolDialogTitle"><header><h2 id="patrolDialogTitle"></h2><button type="button" data-patrol-dialog-close aria-label="关闭详情">×</button></header><div data-patrol-dialog-body></div></dialog>
      <section data-patrol-requests hidden aria-label="待处理工作"></section><section data-patrol-input-history hidden><p>已受理不代表目标已生效，变化以已提交进度为准。</p><div data-patrol-history></div><button data-patrol-fold>展开更早记录</button><button data-patrol-older hidden>加载更早记录</button></section></section>`;
  }
  function history(model) {
    const status = { accepted: "已受理", sending: "发送中", failed: "发送失败" };
    return model.visible.map(row => `<article data-submission-id="${escape(row.submission_id)}"><header><strong>${labels[row.input_type]}</strong><span>${status[row.status]}</span>${row.request_id ? "<span>指定请求的回答</span>" : ""}</header><pre>${escape(row.content)}</pre>${row.status === "failed" ? `<p role="alert">${escape(row.error)}</p><button data-patrol-retry="${escape(row.submission_id)}">重试</button>` : ""}</article>`).join("") || "尚无用户输入";
  }
  function progress(entity, full = false, filter = "all") {
    const state = entity?.state || entity;
    const document = state?.document;
    if (!document) return "尚无已提交进度";
    const items = document.items || [];
    const states = { not_started: "待开展", in_progress: "进行中", completed: "已验证", blocked: "受阻", unknown: "待判断", conflicted: "存在冲突", not_applicable: "被替代" };
    const support = { asserted: "已报告", supported: "有证据", unknown: "待验证" };
    const verified = items.filter(item => item.state === "completed" && item.support === "supported").length;
    const selected = filter === "all" ? items : items.filter(item => item.state === filter);
    return `<div class="patrol-progress-number">${verified}<small> / ${items.length}</small></div><p>${verified ? "已验证条目有独立证据" : "暂无独立验证条目"}；不是项目完成百分比</p><small>已提交 ${escape(state.progress_id || "")} · 版本 ${escape(state.generation ?? "—")}</small>${full ? `<nav class="patrol-tabs">${[["all", "全部"], ["completed", "已验证"], ["in_progress", "进行中"], ["blocked", "阻塞"], ["not_applicable", "被替代"]].map(([value, label]) => `<button data-patrol-progress-filter="${value}" aria-pressed="${value === filter}">${label}</button>`).join("")}</nav>` : ""}<ul>${(full ? selected : selected.slice(0, 3)).map(item => `<li><strong>${escape(item.description)}</strong><span>${states[item.state] || "待判断"} · ${support[item.support] || "待验证"}</span>${full ? `<small>${escape((item.context_ids || []).join(" · "))}</small><p>${escape((item.blockers || []).join("；"))}</p>` : ""}</li>`).join("") || "<li>暂无条目</li>"}</ul>${!full && items.length > 3 ? `<small>另有 ${items.length - 3} 项，打开完整检查</small>` : ""}`;
  }
  function requests(items, ui = () => ({})) {
    return items.filter(item => item.status === "open").map(item => item.scope?.information_only
      ? `<article data-request-id="${escape(item.request_id)}"><h3>待补充信息</h3><p>${escape(item.prompt)}</p><button data-patrol-answer="${escape(item.request_id)}" data-request-revision="${item.revision}">回答此请求</button></article>`
      : (root.FocusLoopWaitRequestView?.render(item, ui(item.request_id)) || `<article><h3>待处理授权</h3><p>${escape(item.prompt)}</p></article>`)).join("");
  }
  function lineageManifest(snapshot, projection) {
    const contexts = new Map();
    for (const node of snapshot.nodes) {
      if (!contexts.has(node.context_id) || contexts.get(node.context_id).generation < node.generation) contexts.set(node.context_id, node);
    }
    const cards = new Map((root.FocusLoopLiveSelectors?.selectContextCards(projection) || []).map(item => [item.id, item]));
    const nodes = [...contexts.values()].map(node => ({ ...cards.get(node.context_id), ...node, title: projection?.contexts?.[node.context_id]?.state.title || node.context_id, status: projection?.contexts?.[node.context_id]?.state.status, current_revision_id: node.revision_id, revision: { revision_id: node.revision_id, generation: node.generation } }));
    return { nodes, edges: snapshot.edges };
  }
  function lineage(snapshot, projection) {
    const manifest = lineageManifest(snapshot, projection);
    return root.FocusPortfolioMapView?.render(manifest, null, root.FocusLoopLiveSelectors?.selectGraphActivity(projection)) || manifest.nodes.map(node => `<p>${escape(node.title)}</p>`).join("");
  }
  function context(page, node, tab) {
    const id = page.context_id || node?.context_id;
    const overview = `${page.revision ? "" : '<p role="status">正在读取已提交版本</p>'}<h3>这条工作线要解决什么</h3><p>${escape(node?.purpose || "暂无独立目的说明")}</p><h3>当前做到哪里了</h3><p>${escape(node?.status || page.revision?.projection_status || "以真实运行与版本为准")}</p><p>正在检查 Revision ${escape(page.revision?.revision_id || "—")}</p>`;
    const sources = `<h3>精确来源与版本</h3><p>正在检查 ${escape(page.revision?.revision_id || "当前版本")}</p>${(page.versions || []).map(item => `<button data-patrol-inspect-revision="${escape(item.revision_id)}" aria-pressed="${item.revision_id === page.revision?.revision_id}">R${escape(item.generation)} · ${escape(item.origin_kind)}${item.current ? " · 当前" : ""}</button>`).join("")}<h3>本版本精确来源</h3>${page.sources ? page.sources.map(item => `<p>${escape(item.source?.context_id || item.context_id)} · ${escape(item.source?.revision_id || item.revision_id)}</p>`).join("") || "<p>本版本没有外部来源</p>" : "<p>正在按需读取来源与版本</p>"}`;
    return `<header><div><h2>${escape(node?.title || page.title || id)}</h2><small>${escape(id)} · 只读检查</small></div><button data-patrol-context-close aria-label="关闭 Context 检查">×</button></header><nav class="patrol-tabs">${[["overview", "概要"], ["conversation", "会话"], ["sources", "来源与版本"]].map(([key, label]) => `<button data-patrol-context-tab="${key}" aria-pressed="${tab === key}">${label}</button>`).join("")}</nav><div class="patrol-context-body">${tab === "conversation" ? root.FocusContextConversationView.renderInspection(page) : tab === "sources" ? sources : overview}</div><footer><p>查看 Context 不会改变 Patrol 的发送目标。</p><button data-patrol-open-task="${escape(id)}">在任务中打开 ↗</button></footer>`;
  }
  return Object.freeze({ skeleton, composer, history, progress, requests, lineageManifest, lineage, context, escape });
});
