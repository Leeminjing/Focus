/*
 * 本文件对外提供 Context Portfolio 的稳定拓扑图视图。
 * 输入为轻量 nodes/edges、选中 Context 与 canonical directive/Run 活动；输出为带真实并行状态、因果连线的纵向可交互演化图。
 * 具体工作流为仅在拓扑变化时重算坐标，普通状态沿用位置；活动效果严格由 directive lifecycle 和 Run state 驱动，空闲时不循环播放；
 * 节点卡只显示一次描述文字，purpose 与节点名称相同时不再重复渲染；层级只由跨 Context 依赖决定（自环不参与），
 * 无依赖的 Context 位于根层，每条派生连线带方向标记。
 * 示例：`FocusPortfolioMapView.render(manifest, selectedId, graphActivity)`。
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
      if (edge.source_context_id === edge.target_context_id) return;
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
    ids.filter(id => !visited.has(id)).forEach(id => depth.set(id, 0));
    const layers = new Map();
    ids.forEach(id => {
      const key = depth.get(id) || 0;
      if (!layers.has(key)) layers.set(key, []);
      layers.get(key).push(id);
    });
    const positions = new Map();
    const cardWidth = 220;
    const xGap = 34;
    const yGap = 184;
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

  function render(manifest, selectedId, graphActivity = []) {
    const nodes = manifest?.nodes || [];
    const edges = (manifest?.edges || []).filter(edge => edge.target_context_id);
    if (!nodes.length) return '<section class="loop-map-empty">尚无 Context</section>';
    const geometry = layout(nodes, edges);
    const lines = edges.map(edge => {
      if (edge.source_context_id === edge.target_context_id) return "";
      const source = geometry.positions.get(edge.source_context_id);
      const target = geometry.positions.get(edge.target_context_id);
      if (!source || !target) return "";
      const x1 = source.x + 110;
      const y1 = source.y + 142;
      const x2 = target.x + 110;
      const y2 = target.y;
      const middle = (y1 + y2) / 2;
      return `<path data-edge-id="${escape(`${edge.source_context_id}:${edge.target_context_id}:${edge.target_revision_id || "current"}`)}" marker-end="url(#portfolio-edge-arrow)" d="M ${x1} ${y1} C ${x1} ${middle}, ${x2} ${middle}, ${x2} ${y2}" />`;
    }).join("");
    const activityLines = graphActivity.map(item => {
      const target = geometry.positions.get(item.target_context_id);
      if (!target) return "";
      const x = target.x + 110;
      const active = ["authorized", "delivering", "delivered"].includes(item.state) || ["queued", "pending", "running"].includes(item.run?.status);
      return `<g class="directive-path is-${escape(item.state || "unknown")}${active ? " is-active" : ""}" data-directive-id="${escape(item.id)}"><path d="M ${geometry.width / 2} 28 C ${geometry.width / 2} ${Math.max(42, target.y / 2)}, ${x} ${Math.max(42, target.y / 2)}, ${x} ${target.y}" /><circle cx="${x}" cy="${target.y}" r="4" /><title>${escape(item.origin || "Patrol")} → ${escape(item.target_context_id)} · ${escape(item.state)}</title></g>`;
    }).join("");
    const cards = nodes.map((node, index) => {
      const point = geometry.positions.get(node.context_id) || { x: 0, y: 0 };
      const run = node.latest_run;
      const workspaceChanges = Array.isArray(run?.workspace_result?.effect_evidence)
        ? run.workspace_result.effect_evidence.filter(item => item?.changed).length
        : Number(node.counts?.workspace_changes || 0);
      const evidence = [
        `${escape(node.counts?.runs || 0)} runs`,
        node.counts?.workspace_changes ? `${escape(node.counts.workspace_changes)} changes` : "",
        node.counts?.artifacts ? `${escape(node.counts.artifacts)} artifacts` : "",
      ].filter(Boolean).join(" · ");
      const live = run ? `<span class="context-node-live"><span>${escape(node.live_action?.detail?.tool_name || node.live_action?.summary || `${run.status || "idle"} · ${run.origin || "run"}`)}</span><span>${escape(workspaceChanges)} workspace changes · ${escape(Number(run.input_tokens || 0) + Number(run.output_tokens || 0))} tokens</span></span>` : "";
      const name = node.topic || node.title;
      const purpose = node.purpose && node.purpose !== name ? `<span class="context-node-purpose">${escape(node.purpose)}</span>` : "";
      return `<button type="button" class="portfolio-context-node${node.context_id === selectedId ? " is-selected" : ""}" style="left:${point.x}px;top:${point.y}px" data-action="loop-select-context" data-context-id="${escape(node.context_id)}"><span class="context-node-eyebrow">#${index} ${escape(node.role || "Context")}<span><i class="run-dot is-${escape(run?.status || node.status)}" aria-hidden="true"></i>${escape(run?.status || node.status)}</span></span><span class="context-node-top"><strong>${escape(name)}</strong></span>${purpose}${live}<span class="context-node-meta">R${escape(node.revision?.generation || "—")} · ${evidence || "暂无运行证据"}</span></button>`;
    }).join("");
    return `<section class="portfolio-map" aria-label="Context Portfolio"><div class="portfolio-map-toolbar"><span><strong>${escape(nodes.length)}</strong> 个 Context · Evolution Graph · Live</span><span class="portfolio-health is-${escape(manifest.health)}">Patrol ${escape(manifest.health)}</span></div><div class="portfolio-map-scroll"><div class="portfolio-map-canvas" style="width:${geometry.width}px;height:${geometry.height}px"><svg width="${geometry.width}" height="${geometry.height}" aria-label="Patrol 与 Context 的真实因果关系"><defs><marker id="portfolio-edge-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><polygon points="0 0, 10 5, 0 10" /></marker></defs>${lines}${activityLines}</svg>${cards}</div></div></section>`;
  }

  function reconcile(host, manifest, selectedId, graphActivity = []) {
    const current = host?.querySelector?.(".portfolio-map");
    if (!current || typeof document !== "object") return false;
    const template = document.createElement("template");
    template.innerHTML = render(manifest, selectedId, graphActivity);
    const next = template.content.firstElementChild;
    const currentCanvas = current.querySelector(".portfolio-map-canvas");
    const nextCanvas = next?.querySelector(".portfolio-map-canvas");
    if (!next || !currentCanvas || !nextCanvas) return false;
    current.querySelector(".portfolio-map-toolbar")?.replaceWith(next.querySelector(".portfolio-map-toolbar"));
    currentCanvas.setAttribute("style", nextCanvas.getAttribute("style") || "");
    const currentSvg = currentCanvas.querySelector("svg");
    const nextSvg = nextCanvas.querySelector("svg");
    if (currentSvg && nextSvg) {
      currentSvg.setAttribute("width", nextSvg.getAttribute("width"));
      currentSvg.setAttribute("height", nextSvg.getAttribute("height"));
      const existingShapes = new Map([...currentSvg.querySelectorAll("[data-edge-id], [data-directive-id]")].map(item => [item.getAttribute("data-edge-id") || `directive:${item.getAttribute("data-directive-id")}`, item]));
      for (const shape of [...nextSvg.querySelectorAll("[data-edge-id], [data-directive-id]")]) {
        const key = shape.getAttribute("data-edge-id") || `directive:${shape.getAttribute("data-directive-id")}`;
        const prior = existingShapes.get(key);
        if (prior) {
          existingShapes.delete(key);
          if (prior.outerHTML !== shape.outerHTML) prior.replaceWith(shape);
          else currentSvg.append(prior);
        } else currentSvg.append(shape);
      }
      existingShapes.forEach(shape => shape.remove());
    }
    const existingNodes = new Map([...currentCanvas.querySelectorAll(".portfolio-context-node")].map(node => [node.dataset.contextId, node]));
    for (const node of [...nextCanvas.querySelectorAll(".portfolio-context-node")]) {
      const prior = existingNodes.get(node.dataset.contextId);
      if (!prior) currentCanvas.append(node);
      else {
        existingNodes.delete(node.dataset.contextId);
        prior.className = node.className;
        prior.setAttribute("style", node.getAttribute("style") || "");
        if (prior.innerHTML !== node.innerHTML) prior.innerHTML = node.innerHTML;
        currentCanvas.append(prior);
      }
    }
    existingNodes.forEach(node => node.remove());
    return true;
  }

  return Object.freeze({ render, reconcile });
});
