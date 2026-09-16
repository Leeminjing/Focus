/*
 * 本文件对外提供 Context Portfolio 的稳定拓扑图视图。
 * 输入为轻量 nodes/edges、选中 Context 与 Loop 状态；输出为带主题、方向、Run 状态和派生连线的纵向可交互演化图。
 * 具体工作流为仅在拓扑指纹变化时重算确定性分层坐标，同层节点围绕画布中心展开，状态刷新沿用原坐标以避免节点跳动。
 * 示例：`FocusPortfolioMapView.render(manifest, selectedId)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusPortfolioMapView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  let cache = { fingerprint: "", positions: new Map(), width: 880, height: 520 };

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
    const cardWidth = 220;
    const xGap = 34;
    const yGap = 154;
    const largestLayer = Math.max(1, ...[...layers.values()].map(layer => layer.length));
    const width = Math.max(880, 72 + largestLayer * cardWidth + (largestLayer - 1) * xGap);
    [...layers.entries()].sort((a, b) => a[0] - b[0]).forEach(([row, layer]) => {
      layer.sort();
      const rowWidth = layer.length * cardWidth + Math.max(0, layer.length - 1) * xGap;
      const start = (width - rowWidth) / 2;
      layer.forEach((id, column) => positions.set(id, { x: start + column * (cardWidth + xGap), y: 54 + row * yGap }));
    });
    cache = {
      fingerprint,
      positions,
      width,
      height: Math.max(520, 96 + (Math.max(0, ...depth.values()) + 1) * yGap),
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
      const x1 = source.x + 110;
      const y1 = source.y + 112;
      const x2 = target.x + 110;
      const y2 = target.y;
      const middle = (y1 + y2) / 2;
      return `<path d="M ${x1} ${y1} C ${x1} ${middle}, ${x2} ${middle}, ${x2} ${y2}" />`;
    }).join("");
    const cards = nodes.map((node, index) => {
      const point = geometry.positions.get(node.context_id) || { x: 0, y: 0 };
      const run = node.latest_run;
      const evidence = [
        `${escape(node.counts?.runs || 0)} runs`,
        node.counts?.workspace_changes ? `${escape(node.counts.workspace_changes)} changes` : "",
        node.counts?.artifacts ? `${escape(node.counts.artifacts)} artifacts` : "",
      ].filter(Boolean).join(" · ");
      return `<button type="button" class="portfolio-context-node${node.context_id === selectedId ? " is-selected" : ""}" style="left:${point.x}px;top:${point.y}px" data-action="loop-select-context" data-context-id="${escape(node.context_id)}"><span class="context-node-eyebrow">#${index} ${escape(node.role || "Context")}<span><i class="run-dot is-${escape(run?.status || node.status)}" aria-hidden="true"></i>${escape(run?.status || node.status)}</span></span><span class="context-node-top"><strong>${escape(node.topic || node.title)}</strong></span><span class="context-node-purpose">${escape(node.purpose)}</span><span class="context-node-meta">R${escape(node.revision?.generation || "—")} · ${evidence || "暂无运行证据"}</span></button>`;
    }).join("");
    return `<section class="portfolio-map" aria-label="Context Portfolio"><div class="portfolio-map-toolbar"><span><strong>${escape(nodes.length)}</strong> 个 Context · Evolution Graph</span><span class="portfolio-health is-${escape(manifest.health)}">Patrol ${escape(manifest.health)}</span></div><div class="portfolio-map-scroll"><div class="portfolio-map-canvas" style="width:${geometry.width}px;height:${geometry.height}px"><svg width="${geometry.width}" height="${geometry.height}" aria-hidden="true">${lines}</svg>${cards}</div></div></section>`;
  }

  return Object.freeze({ render });
});
