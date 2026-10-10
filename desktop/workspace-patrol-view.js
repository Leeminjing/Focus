/* 本文件对外提供 Patrol 的输入、按需详情、历史、进度及只读 Context HTML。
 * 输入为已绑定工作区与真实查询/Live 投影；输出为转义的工作台、进度定位与精确版本关系摘要。
 * 具体工作流为安静态呈现标题与大输入，详情以图为主、左右读面承接原图/事实/会话；清空只作用于草稿，检查与输入身份分离。
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
    return `<header class="patrol-quiet-heading"><div><h1>Patrol</h1><p data-i18n="patrol.tagline">说出你的想法，剩下的交给 Patrol。</p></div><button class="quiet-button" type="button" data-patrol-clear-draft${workspace ? "" : " disabled"} data-i18n="patrol.clear_draft">清空输入</button></header><form class="glass-surface" data-patrol-composer><i class="workbench-composer-icon" aria-hidden="true"><span class="ui-icon icon-brain-circuit"></span></i><span class="workbench-composer-label">Patrol · ${escape(workspace?.display_name || "工作区")}</span>
      <textarea data-patrol-content rows="3" aria-label="向 Patrol 输入信息" data-i18n-placeholder="patrol.placeholder" placeholder="描述你的任务、问题或想法…"${workspace ? "" : " disabled"}></textarea>
      <p data-patrol-error role="alert" hidden></p><div class="patrol-composer-footer"><div><button type="button" data-action="show-patrol" data-patrol-switch>▢ ${escape(workspace?.display_name || "选择工作区")}</button><select data-patrol-type aria-label="输入类型">${Object.entries(labels).map(([key, label]) => `<option value="${key}">${label}</option>`).join("")}</select><span data-patrol-target></span><button type="button" data-patrol-clear-target hidden>改为主动输入</button></div>
      <div><span data-patrol-receipt role="status"></span><button type="button" data-patrol-pending hidden>待处理</button><button type="button" data-patrol-live-retry hidden>重连工作状态</button><button type="button" data-patrol-history-open>输入记录</button><button type="button" data-patrol-details aria-expanded="false" aria-controls="patrolWorkDetails">查看工作详情 ↗</button><button type="submit" aria-label="发送给工作区 Patrol"${workspace ? "" : " disabled"}>↑</button></div></div><small data-patrol-state role="status">${workspace ? "正在读取工作状态" : "先选择工作区文件夹，确认后即可输入"}</small></form>`;
  }
  function skeleton(workspace) {
    return `<section class="workspace-patrol is-quiet" data-workbench-panel="graph"><div id="patrolWorkDetails" data-patrol-details-content hidden inert>
      <header class="patrol-page-heading"><button data-patrol-details aria-expanded="false" aria-controls="patrolWorkDetails" aria-label="退出工作详情">← 返回</button><button data-action="show-patrol" class="workbench-workspace">${escape(workspace.display_name)} / Patrol</button><span data-workbench-status></span><button data-action="show-map">全图 ↗</button><details class="patrol-control-menu"><summary>工作控制</summary><button data-patrol-control="pause">暂停执行</button><button data-patrol-control="resume">继续执行</button><button data-patrol-control="stop">停止执行</button><button data-patrol-grant-open>授权与预算</button><button data-patrol-restart hidden>重新启用</button></details></header>
      <nav class="workbench-panel-tabs" aria-label="工作台视图">${[["graph", "关系图"], ["progress", "进度"], ["observation", "观察"], ["facts", "事实"], ["context", "所选工作线"]].map(([key, title]) => `<button data-workbench-show="${key}" aria-pressed="${key === "graph"}">${title}</button>`).join("")}</nav>
      <div class="patrol-observations">
        <section class="patrol-graph-card"><header><span>当前工作区 · 已提交来源</span><button data-patrol-related aria-pressed="false" disabled>仅看所选关联</button></header><div data-patrol-lineage-error role="status" hidden></div><div data-patrol-lineage>首线程尚未提交</div><p class="workbench-legend"><span>已提交来源关系</span><span>所选 Context 的直接关联</span><span>Run 状态以节点为准</span></p><p data-workbench-location role="status" hidden></p></section>
        <section class="patrol-progress-card workbench-panel glass-surface"><header><h2 data-i18n="patrol.progress">任务进度</h2><button data-patrol-progress-open aria-label="检查已提交进度">↗</button></header><small class="workbench-kicker">TASK PROGRESS</small><div data-patrol-progress>尚无已提交进度</div></section>
        <section class="patrol-observation-card workbench-panel glass-surface"><header><h2 data-i18n="patrol.observation">本轮观察</h2></header><div data-patrol-observation>尚未冻结</div><button data-patrol-observation-open disabled>检查本轮冻结依据 →</button></section>
        <section class="patrol-facts-card workbench-panel glass-surface"><header><h2>LoopFact</h2><span class="workbench-fact-label">事实流</span></header><small class="workbench-kicker" data-workbench-round>当前 Live 事实</small><div class="workbench-fact-stream" data-patrol-facts></div><footer><small>当前 Live 窗口</small><button data-patrol-facts-open>全部 ↗</button></footer></section>
        <aside data-patrol-context class="patrol-selected-card workbench-panel glass-surface" aria-label="只读 Context 检查"><div data-patrol-context-content>${contextEmpty()}</div></aside>
      </div></div>
      ${composer(workspace)}
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
    if (!full) return `<div class="patrol-progress-number">${verified}<small> / ${items.length}</small></div><p>条目已验证 · 均有独立证据</p><div class="workbench-progress-meter" aria-label="${verified} / ${items.length} 个条目已验证">${items.map(item => `<i class="${item.state === "completed" && item.support === "supported" ? "is-verified" : ""}"></i>`).join("")}</div><ul>${items.map(item => `<li><button data-patrol-progress-locate="${escape(JSON.stringify(item.context_ids || []))}" class="is-${escape(item.state)}"><span>${escape(item.description)}</span><small>${states[item.state] || "待判断"} · ${support[item.support] || "待验证"}${item.context_ids?.length ? ` · ${item.context_ids.length} 条工作线` : ""}</small></button></li>`).join("") || "<li>暂无条目</li>"}</ul><footer><small>已提交快照 · P${escape(state.generation ?? "—")}</small><small>选中条目，定位相关工作线</small></footer>`;
    return `<div class="patrol-progress-number">${verified}<small> / ${items.length}</small></div><p>${verified ? "已验证条目有独立证据" : "暂无独立验证条目"}；不是项目完成百分比</p><small>已提交 ${escape(state.progress_id || "")} · 版本 ${escape(state.generation ?? "—")}</small>${full ? `<nav class="patrol-tabs">${[["all", "全部"], ["completed", "已验证"], ["in_progress", "进行中"], ["blocked", "阻塞"], ["not_applicable", "被替代"]].map(([value, label]) => `<button data-patrol-progress-filter="${value}" aria-pressed="${value === filter}">${label}</button>`).join("")}</nav>` : ""}<ul>${selected.map(item => `<li><strong>${escape(item.description)}</strong><span>${states[item.state] || "待判断"} · ${support[item.support] || "待验证"}</span>${full ? `<small>${escape((item.context_ids || []).join(" · "))}</small><p>${escape((item.blockers || []).join("；"))}</p>` : ""}</li>`).join("") || "<li>暂无条目</li>"}</ul>`;
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
    for (const [contextId, revisionId] of Object.entries(snapshot.roots || {})) {
      const rootNode = snapshot.nodes.find(node => node.context_id === contextId && node.revision_id === revisionId);
      if (rootNode) contexts.set(contextId, rootNode);
    }
    const cards = new Map((root.FocusLoopLiveSelectors?.selectContextCards(projection) || []).map(item => [item.id, item]));
    const nodes = [...contexts.values()].map(node => ({ ...(snapshot.roots?.[node.context_id] ? cards.get(node.context_id) : {}), ...node, historical: !snapshot.roots?.[node.context_id], title: projection?.contexts?.[node.context_id]?.state.title || node.context_id, status: projection?.contexts?.[node.context_id]?.state.status, current_revision_id: node.revision_id, revision: { revision_id: node.revision_id, generation: node.generation } }));
    return { nodes, edges: snapshot.edges, revisions: snapshot.nodes };
  }
  function lineage(snapshot, projection, selectedId, options = {}) {
    const manifest = lineageManifest(snapshot, projection);
    return root.FocusPortfolioMapView?.render(manifest, selectedId, root.FocusLoopLiveSelectors?.selectGraphActivity(projection), options) || manifest.nodes.map(node => `<p>${escape(node.title)}</p>`).join("");
  }
  function contextEmpty() {
    return '<header><h2>所选工作线</h2><small>只读检查</small></header><div class="workbench-selection-empty"><p>选择一条工作线</p><small>查看它的来源、已提交版本与运行状态。</small></div><footer><p>查看不会改变 Patrol 的发送目标</p></footer>';
  }
  function context(page, node, tab, options = {}) {
    const id = page.context_id || node?.context_id;
    const manifest = options.manifest || { nodes: [], edges: [] };
    const revisionId = page.revision?.revision_id || options.revisionId || node?.current_revision_id;
    if (options.presentation === "workbench" && tab === "overview") {
      const selectedRevision = manifest.nodes.find(item => item.context_id === id);
      const sources = manifest.edges.filter(edge => edge.target_context_id === id && edge.target_revision_id === revisionId);
      const consumers = new Set(manifest.edges.filter(edge => edge.source_context_id === id && edge.source_revision_id === revisionId).map(edge => edge.target_context_id));
      const generation = page.revision?.generation ?? node?.revision?.generation ?? "—";
      return `<header><h2>所选工作线</h2><button data-patrol-context-close aria-label="关闭 Context 检查">×</button></header><div class="patrol-context-body"><h3>${escape(node?.title || id)}</h3><small>${escape(id.slice(0, 12))} / R${escape(generation)} · 只读检查</small>${selectedRevision?.current_revision_id && selectedRevision.current_revision_id !== revisionId ? '<p class="workbench-version-note">正在检查历史版本；已有更新版本。<button data-patrol-inspect-revision="' + escape(selectedRevision.current_revision_id) + '">查看新版本</button></p>' : ""}<p>${new Set(sources.map(edge => edge.source_context_id)).size} 个直接来源</p><ul class="workbench-source-list">${sources.map(edge => {
        const source = manifest.nodes.find(item => item.context_id === edge.source_context_id);
        const sourceRevision = manifest.revisions?.find(item => item.revision_id === edge.source_revision_id);
        return `<li><button data-context-related="${escape(edge.source_context_id)}" data-revision-id="${escape(edge.source_revision_id)}" title="${escape(edge.source_revision_id)}"><span>${escape(source?.title || edge.source_context_id)}</span><small>${escape(sourceRevision ? `R${sourceRevision.generation}` : edge.source_revision_id.slice(0, 8))}</small></button></li>`;
      }).join("") || '<li>本版本没有外部来源</li>'}</ul><small>被 ${consumers.size} 条工作线采用</small><small class="workbench-scope">当前已返回的已提交关系范围</small><nav class="patrol-tabs"><button data-patrol-context-tab="conversation">会话</button><button data-patrol-context-tab="sources">来源与版本</button></nav></div><footer><p>查看不会改变 Patrol 的发送目标</p><button data-patrol-open-task="${escape(id)}">在任务中打开 ↗</button></footer>`;
    }
    const overview = `${page.revision ? "" : '<p role="status">正在读取已提交版本</p>'}<h3>这条工作线要解决什么</h3><p>${escape(node?.purpose || "暂无独立目的说明")}</p><h3>当前做到哪里了</h3><p>${escape(node?.status || page.revision?.projection_status || "以真实运行与版本为准")}</p><p>正在检查 Revision ${escape(page.revision?.revision_id || "—")}</p>`;
    const sources = `<h3>精确来源与版本</h3><p>正在检查 ${escape(page.revision?.revision_id || "当前版本")}</p>${(page.versions || []).map(item => `<button data-patrol-inspect-revision="${escape(item.revision_id)}" aria-pressed="${item.revision_id === page.revision?.revision_id}">R${escape(item.generation)} · ${escape(item.origin_kind)}${item.current ? " · 当前" : ""}</button>`).join("")}<h3>本版本精确来源</h3>${page.sources ? page.sources.map(item => `<p>${escape(item.source?.context_id || item.context_id)} · ${escape(item.source?.revision_id || item.revision_id)}</p>`).join("") || "<p>本版本没有外部来源</p>" : "<p>正在按需读取来源与版本</p>"}`;
    return `<header><div><h2>${escape(node?.title || page.title || id)}</h2><small>${escape(id)} · 只读检查</small></div><button data-patrol-context-close aria-label="关闭 Context 检查">×</button></header><nav class="patrol-tabs">${[["overview", "概要"], ["conversation", "会话"], ["sources", "来源与版本"]].map(([key, label]) => `<button data-patrol-context-tab="${key}" aria-pressed="${tab === key}">${label}</button>`).join("")}</nav><div class="patrol-context-body">${tab === "conversation" ? root.FocusContextConversationView.renderInspection(page) : tab === "sources" ? sources : overview}</div><footer><p>查看 Context 不会改变 Patrol 的发送目标。</p><button data-patrol-open-task="${escape(id)}">在任务中打开 ↗</button></footer>`;
  }
  return Object.freeze({ skeleton, composer, history, progress, requests, lineageManifest, lineage, context, contextEmpty, escape });
});
