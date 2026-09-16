/*
 * 本文件对外提供 Context revision Evolution Graph 与兼容树投影视图。
 * 输入为不可变 revisions、版本化来源边、第一父链投影和最终采用路径；输出为完整多来源图与兼容树。
 * 具体工作流为建立 revision 图、突出执行/采用节点、显示稳定主来源树并保留全部次要来源链接；
 * 示例：`FocusContextEvolutionView.render(graph, tree, finalResult)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusContextEvolutionView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const escape = value => String(value ?? "").replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  function project(graph = {}) {
    const revisions = (graph.revisions || graph.nodes || []).map(node => node.ref ? { ...node, ...node.ref } : node);
    const edges = (graph.edges || []).map(edge => ({
      ...edge,
      target_revision_id: edge.target_revision_id || edge.target?.revision_id,
      source_revision_id: edge.source_revision_id || edge.source?.revision_id,
      target_context_id: edge.target_context_id || edge.target?.context_id,
      source_context_id: edge.source_context_id || edge.source?.context_id,
    }));
    const sources = new Map();
    for (const edge of edges) {
      const list = sources.get(edge.target_revision_id) || [];
      list.push(edge);
      sources.set(edge.target_revision_id, list);
    }
    return revisions.map(revision => ({ ...revision, sources: (sources.get(revision.revision_id) || []).sort((a, b) => a.position - b.position), first_parent: (sources.get(revision.revision_id) || []).sort((a, b) => a.position - b.position)[0] || null }));
  }
  function render(graph, tree = {}, finalResult = {}) {
    const adopted = new Set([
      ...(finalResult?.context_ids || []),
      ...(finalResult?.final_path || []).map(item => item.context_id),
    ]);
    const revisions = project(graph);
    const graphHtml = revisions.map(revision => `<li class="${adopted.has(revision.context_id) ? "is-adopted" : ""}" data-revision-id="${escape(revision.revision_id)}"><button type="button" data-action="open-loop-revision" data-open-revision="${escape(revision.revision_id)}">${escape(revision.context_id)} · R${escape(revision.generation)}</button><span>${escape(revision.origin_kind)}</span>${revision.current ? "<small>current</small>" : ""}${revision.sources.length > 1 ? `<small>+${revision.sources.length - 1} secondary sources</small>` : ""}</li>`).join("");
    const treeHtml = (tree.nodes || []).sort((a, b) => Number(a.depth) - Number(b.depth)).map(node => `<li style="--context-depth:${escape(node.depth || 0)}"><span>${escape(node.title || node.context_id)}</span><small>${escape(node.lifecycle || "active")}${node.secondary_sources?.length ? ` · +${escape(node.secondary_sources.length)} sources` : ""}${node.cycle_suppressed ? " · feedback edge preserved in graph" : ""}</small></li>`).join("");
    return `<section class="evolution-graph"><h3>Context Evolution Graph</h3><ol>${graphHtml}</ol><details><summary>First-parent compatibility tree</summary><ol class="context-first-parent-tree">${treeHtml || "<li>暂无 Context</li>"}</ol></details></section>`;
  }
  return Object.freeze({ project, render });
});
