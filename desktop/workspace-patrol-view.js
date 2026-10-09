/* 本文件对外提供 Patrol 页面 skeleton、输入历史和三个权威观测区域的 HTML 读面。
 * 输入为已提交 Progress、已发布 Lineage、全 Loop Facts 及用户记录；输出为转义后的独立区域。
 * 具体工作流为复用现有图与事实组件，不渲染 Patrol 回复/推理，不把发送回执当任务结果。
 * 示例：host.innerHTML = view.skeleton(workspace); view.progress(projection.task_progress)。
 */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusWorkspacePatrolView = api;
})(globalThis, function (root) {
  "use strict";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const labels = { information: "普通信息", outcome: "最终结果", boundary: "执行边界", completion_check: "完成检查" };
  function skeleton(workspace) {
    return `<section class="workspace-patrol"><header><h1>Patrol · ${escape(workspace.display_name)}</h1><span data-patrol-state role="status"></span><button data-patrol-control="pause">暂停执行</button><button data-patrol-control="resume">继续执行</button><button data-patrol-control="stop">停止执行</button><button data-patrol-restart hidden>重新启用</button><button data-action="show-patrol" data-patrol-switch>切换工作区</button></header>
      <p data-patrol-error role="alert"></p><div class="patrol-observations">
      <section><h2>Task Progress</h2><div data-patrol-progress>尚无已提交进度</div></section>
      <section><h2>Current Derivation Lineage</h2><div data-patrol-lineage>首线程尚未提交</div></section>
      <section><h2>LoopFact</h2><div class="patrol-facts"><table><thead><tr><th>时间</th><th>Context</th><th>类型</th><th>事实</th><th>状态</th><th>来源</th></tr></thead><tbody data-patrol-facts></tbody></table></div></section></div>
      <details data-patrol-context hidden><summary>Context 检查</summary><div data-patrol-context-content></div></details><section data-patrol-requests aria-label="待补充信息"></section><section aria-label="用户输入记录"><h2>输入记录</h2><div data-patrol-history></div><button data-patrol-fold>展开更早记录</button><button data-patrol-older hidden>加载更早记录</button></section>
      <form data-patrol-composer><label>输入类型<select data-patrol-type>${Object.entries(labels).map(([key, label]) => `<option value="${key}">${label}</option>`).join("")}</select></label><span data-patrol-target></span><button type="button" data-patrol-clear-target hidden>改为主动输入</button><textarea data-patrol-content rows="3" aria-label="向 Patrol 输入信息" placeholder="输入想法、问题或决定"></textarea><button type="submit">发送</button><span>发送成功表示已收到；任务变化以已提交进度为准。</span></form></section>`;
  }
  function history(model) {
    const status = { accepted: "发送成功", sending: "发送中", failed: "发送失败" };
    return model.visible.map(row => `<article data-submission-id="${escape(row.submission_id)}"><header><strong>${labels[row.input_type]}</strong><span>${status[row.status]}</span>${row.request_id ? "<span>指定请求的回答</span>" : ""}</header><pre>${escape(row.content)}</pre>${row.status === "failed" ? `<p role="alert">${escape(row.error)}</p><button data-patrol-retry="${escape(row.submission_id)}">重试</button>` : ""}</article>`).join("") || "尚无用户输入";
  }
  function progress(entity) {
    const document = entity?.state?.document;
    if (!document) return "尚无已提交进度";
    const items = document.items || [];
    const states = { not_started: "待开展", in_progress: "进行中", completed: "已完成", blocked: "受阻", unknown: "待判断", conflicted: "存在冲突", not_applicable: "不再适用" };
    const support = { asserted: "已报告", supported: "有证据", unknown: "待验证" };
    const sections = [["当前工作", items.filter(item => ["not_started", "in_progress", "unknown", "conflicted"].includes(item.state))], ["已完成", items.filter(item => item.state === "completed")], ["阻塞", items.filter(item => item.state === "blocked")], ["被替代或不再适用", items.filter(item => item.state === "not_applicable")]];
    return `<p>已提交版本 ${escape(entity.state.generation)}</p>${sections.map(([label, rows]) => `<h3>${label}</h3>${rows.length ? `<ul>${rows.map(item => `<li>${escape(item.description)} · ${states[item.state] || "待判断"} · ${support[item.support] || "待验证"} ${item.blockers?.length ? `· ${escape(item.blockers.join("；"))}` : ""}</li>`).join("")}</ul>` : "<p>暂无</p>"}`).join("")}`;
  }
  function requests(items) {
    return items.filter(item => item.status === "open").map(item => item.scope?.information_only
      ? `<article data-request-id="${escape(item.request_id)}"><h3>待补充信息</h3><p>${escape(item.prompt)}</p><button data-patrol-answer="${escape(item.request_id)}" data-request-revision="${item.revision}">回答此请求</button></article>`
      : (root.FocusLoopWaitRequestView?.render(item) || `<article><h3>待处理授权</h3><p>${escape(item.prompt)}</p></article>`)).join("");
  }
  function lineage(snapshot, projection) {
    const contexts = new Map();
    for (const node of snapshot.nodes) {
      if (!contexts.has(node.context_id) || contexts.get(node.context_id).generation < node.generation) contexts.set(node.context_id, node);
    }
    const nodes = [...contexts.values()].map(node => ({ ...node, title: projection?.contexts?.[node.context_id]?.state.title || node.context_id, current_revision_id: node.revision_id, revision: { revision_id: node.revision_id, generation: node.generation } }));
    return root.FocusPortfolioMapView?.render({ nodes, edges: snapshot.edges }, null) || nodes.map(node => `<p>${escape(node.title)}</p>`).join("");
  }
  return Object.freeze({ skeleton, history, progress, requests, lineage, escape });
});
