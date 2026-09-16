/*
 * 本文件对外提供 Context Portfolio 的稳定拓扑图视图。
 * 输入为轻量 nodes/edges、选中 Context 与 Loop 状态；输出为带主题、方向、Run 状态和派生连线的可交互 HTML。
 * 具体工作流为仅在拓扑指纹变化时重算确定性分层坐标，状态刷新沿用原坐标，避免节点跳动。
 * 示例：`FocusPortfolioMapView.render(manifest, selectedId)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusPortfolioMapView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  let cache = { fingerprint: "", positions: new Map(), width: 760, height: 420 };

  function layout(nodes, edges) {
    const fingerprint = JSON.stringify({ nodes: nodes.map(node => node.context_id).sort(), edges: edges.map(edge => [edge.source_context_id, edge.target_context_id]).sort() });
    if (cache.fingerprint === fingerprint) return cache;
    const ids = nodes.map(node => node.context_id).sort();
    const incoming = new Map(ids.map(id => [id, 0]));
    const outgoing = new Map(ids.map(id => [id, []]));
    edges.forEach(edge => {
      if (!incoming.has(edge.target_context_id) || !outgoing.has(edge.source_context_id)) return;
      incoming.set(edge.target_context_id, incoming.get(edge.target_context_id) + 1);
      outgoing.get(edge.source_context_id).push(edge.target_context_id);
    });
    const depth = new Map(ids.map(id => [id, 0]));
    const queue = ids.filter(id => incoming.get(id) === 0);
    const visited = new Set();
    while (queue.length) {
      const id = queue.shift();
      if (visited.has(id)) continue;
      visited.add(id);
      for (const target of outgoing.get(id) || []) {
        depth.set(target, Math.max(depth.get(target) || 0, (depth.get(id) || 0) + 1));
        incoming.set(target, incoming.get(target) - 1);
        if (incoming.get(target) === 0) queue.push(target);
      }
    }
    ids.filter(id => !visited.has(id)).forEach((id, index) => depth.set(id, Math.max(depth.get(id) || 0, index ? 1 : 0)));
    const layers = new Map();
    ids.forEach(id => {
      const key = depth.get(id) || 0;
      if (!layers.has(key)) layers.set(key, []);
      layers.get(key).push(id);
    });
    const positions = new Map();
    const xGap = 260;
    const yGap = 150;
    [...layers.entries()].sort((a, b) => a[0] - b[0]).forEach(([column, layer]) => {
      layer.sort().forEach((id, row) => positions.set(id, { x: 32 + column * xGap, y: 32 + row * yGap }));
    });
    cache = {
      fingerprint,
      positions,
      width: Math.max(640, 80 + (Math.max(0, ...depth.values()) + 1) * xGap),
      height: Math.max(390, 80 + Math.max(1, ...[...layers.values()].map(layer => layer.length)) * yGap),
    };
    return cache;
  }

  function render(manifest, selectedId) {
    const nodes = manifest?.nodes || [];
    const edges = (manifest?.edges || []).filter(edge => edge.target_context_id);
    if (!nodes.length) return '<section class="loop-map-empty">尚无 Context</section>';
    const geometry = layout(nodes, edges);
    const lines = edges.map(edge => {
      const source = geometry.positions.get(edge.source_context_id);
      const target = geometry.positions.get(edge.target_context_id);
      if (!source || !target) return "";
      const x1 = source.x + 210;
      const y1 = source.y + 52;
      const x2 = target.x;
      const y2 = target.y + 52;
      const middle = (x1 + x2) / 2;
      return `<path d="M ${x1} ${y1} C ${middle} ${y1}, ${middle} ${y2}, ${x2} ${y2}" />`;
    }).join("");
    const cards = nodes.map(node => {
      const point = geometry.positions.get(node.context_id) || { x: 0, y: 0 };
      const run = node.latest_run;
      const evidence = [
        `${escape(node.counts?.runs || 0)} runs`,
        node.counts?.workspace_changes ? `${escape(node.counts.workspace_changes)} changes` : "",
        node.counts?.artifacts ? `${escape(node.counts.artifacts)} artifacts` : "",
      ].filter(Boolean).join(" · ");
      return `<button type="button" class="portfolio-context-node${node.context_id === selectedId ? " is-selected" : ""}" style="left:${point.x}px;top:${point.y}px" data-action="loop-select-context" data-context-id="${escape(node.context_id)}"><span class="context-node-top"><strong>${escape(node.topic || node.title)}</strong><i class="run-dot is-${escape(run?.status || node.status)}" aria-hidden="true"></i></span><span class="context-node-purpose">${escape(node.purpose)}</span><span class="context-node-meta">R${escape(node.revision?.generation || "—")} · ${escape(run?.status || node.status)} · ${evidence}</span></button>`;
    }).join("");
    return `<section class="portfolio-map" aria-label="Context Portfolio"><header><div><span class="loop-kicker">Context Portfolio</span><h3>${escape(nodes.length)} 个 Context 正在演化</h3></div><span class="portfolio-health is-${escape(manifest.health)}">Patrol ${escape(manifest.health)}</span></header><div class="portfolio-map-scroll"><div class="portfolio-map-canvas" style="width:${geometry.width}px;height:${geometry.height}px"><svg width="${geometry.width}" height="${geometry.height}" aria-hidden="true">${lines}</svg>${cards}</div></div></section>`;
  }

  return Object.freeze({ render });
});
