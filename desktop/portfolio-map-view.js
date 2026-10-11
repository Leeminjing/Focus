/*
 * 本文件对外提供 Context Portfolio 的稳定拓扑图视图。
 * 输入为轻量 nodes/edges、选中 Context 与 canonical directive/Run 活动；输出为简短节点、精确来源及单层控制材质的缩放/平移横向演化图。
 * 具体工作流为仅在拓扑变化时重算坐标，普通状态沿用位置，未变属性/工具条不写回活动 DOM；活动效果严格由 directive lifecycle 和 Run state 驱动，空闲时不循环播放；
 * 节点卡只显示一次描述文字，purpose 与节点名称相同时不再重复渲染；层级只由跨 Context 依赖决定（自环不参与），
 * 无依赖的 Context 位于根层，每条派生连线带方向标记。
 * 示例：`FocusPortfolioMapView.render(manifest, selectedId, graphActivity, {presentation: "workbench"})`；工作台使用同一精确边对账、独立边命中与有限重点卡，默认调用方保持原呈现。
 * relationship 输出共用精确关系检查内容；来源连线以完整 source/target Context 与 Revision 元组对账，保留可查看身份，避免同一 Context 不同来源版本碰撞。
 * patchPreviews 输入按 Context ID 索引的 {run_id,role,tool_name,text,status,notice}，输出为对应当前 Run 卡的局部正文更新；不读取网络、不影响检查或业务状态。
 * 当前 running 根节点使用固定玻璃卡，正文由原生换行与末端对齐保留最后两行；同 Run 对账保留预览 DOM，历史版本不显示实时正文。
 * 示例：FocusPortfolioMapView.patchPreviews(host, {ctx: {run_id:"run-1",role:"Assistant",text:"最新输出",status:"streaming"}})。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusPortfolioMapView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const statusLabel = value => ({ active: "已建立", running: "执行中", pending: "待执行", queued: "已排队", paused: "已暂停", success: "有运行结果", failed: "执行失败", error: "执行失败", cancelled: "已取消", stopped: "已停止", retired: "已退出" })[value] || value || "以版本为准";
  const geometryApi = () => globalThis.FocusPortfolioMapLayout || require("./portfolio-map-layout.js");
  const layout = (nodes, edges, presentation) => geometryApi().layout(nodes, edges, presentation);
  const runOf = node => node.active_run?.status === "running" ? node.active_run : node.latest_run;
  const previewLabels = Object.freeze({ waiting: "等待输出", streaming: "实时更新", tool_wait: "等待工具输出", disconnected: "连接中断", unavailable: "预览不可用", ended: "运行已结束" });

  function livePreview() {
    return '<span class="context-live-preview" data-context-preview data-preview-status="waiting"><span class="context-preview-heading"><svg class="context-preview-icon" viewBox="0 0 20 20" aria-hidden="true"><path d="M4 3.5h12a1.5 1.5 0 0 1 1.5 1.5v7a1.5 1.5 0 0 1-1.5 1.5H8L4 17v-3.5A1.5 1.5 0 0 1 2.5 12V5A1.5 1.5 0 0 1 4 3.5Z M6 7h8 M6 10h5"/></svg><strong class="context-preview-role">等待输出</strong><span class="context-preview-indicator" aria-hidden="true">•••</span></span><span class="context-preview-tail"><span class="context-preview-text"></span></span><span class="context-preview-status">等待输出</span></span>';
  }

  function render(manifest, selectedId, graphActivity = [], options = {}) {
    const nodes = manifest?.nodes || [];
    const edges = (manifest?.edges || []).filter(edge => edge.target_context_id);
    if (!nodes.length) return '<section class="loop-map-empty">尚无 Context</section>';
    const workbench = options.presentation === "workbench";
    const geometry = layout(nodes, edges, options.presentation);
    const revisionId = options.selectedRevisionId || nodes.find(node => node.context_id === selectedId)?.current_revision_id;
    const liveIds = new Set(nodes.filter(node => !node.historical && !(node.context_id === selectedId && revisionId !== node.current_revision_id) && runOf(node)?.status === "running" && (runOf(node).run_id || runOf(node).id)).map(node => node.context_id));
    const adjacent = edge => (edge.source_context_id === selectedId && edge.source_revision_id === revisionId) || (edge.target_context_id === selectedId && edge.target_revision_id === revisionId);
    const related = new Set([selectedId, ...edges.filter(adjacent).flatMap(edge => [edge.source_context_id, edge.target_context_id])]);
    const ranked = [...nodes].sort((a, b) => Number(related.has(b.context_id)) - Number(related.has(a.context_id)) || Number(b.latest_run?.status === "running") - Number(a.latest_run?.status === "running") || a.context_id.localeCompare(b.context_id));
    const degree = id => edges.filter(edge => edge.source_context_id === id || edge.target_context_id === id).length;
    const featured = new Set(selectedId && !liveIds.has(selectedId) ? [selectedId] : []);
    for (const node of (workbench ? ranked.filter(node => !liveIds.has(node.context_id) && (!selectedId || related.has(node.context_id))) : []).sort((a, b) => degree(b.context_id) - degree(a.context_id) || a.context_id.localeCompare(b.context_id))) {
      if (featured.size >= 5) break;
      const point = geometry.positions.get(node.context_id);
      if ([...featured].every(id => {
        const peer = geometry.positions.get(id);
        return !peer || ((point.x - peer.x) / 240) ** 2 + ((point.y - peer.y) / 150) ** 2 > 1.7;
      })) featured.add(node.context_id);
    }
    const sizes = geometryApi().nodeSizes;
    const halfWidth = id => liveIds.has(id) ? sizes.live.width / 2 : featured.has(id) ? sizes.card.width / 2 : 0;
    const visible = id => !options.relatedOnly || !selectedId || related.has(id);
    const lines = edges.map(edge => {
      const self = edge.source_context_id === edge.target_context_id;
      if (self && !workbench) return "";
      const source = geometry.positions.get(edge.source_context_id);
      const target = geometry.positions.get(edge.target_context_id);
      if (!source || !target) return "";
      const x1 = source.x + (workbench ? halfWidth(edge.source_context_id) : sizes.default.width);
      const y1 = source.y + (workbench ? 0 : sizes.default.height / 2);
      const x2 = target.x - (workbench ? halfWidth(edge.target_context_id) : 0);
      const y2 = target.y + (workbench ? 0 : sizes.default.height / 2);
      const middle = (x1 + x2) / 2;
      const identity = JSON.stringify([edge.source_context_id, edge.source_revision_id ?? null, edge.target_context_id, edge.target_revision_id ?? null]);
      const curve = self ? `M ${x1} ${y1} C ${x1 + 64} ${y1 - 100}, ${x2 - 64} ${y2 - 100}, ${x2} ${y2}` : `M ${x1} ${y1} C ${middle} ${y1}, ${middle} ${y2}, ${x2} ${y2}`;
      if (workbench) return `<g data-edge-id="${escape(identity)}" class="workbench-edge${adjacent(edge) ? " is-related" : ""}"${visible(edge.source_context_id) && visible(edge.target_context_id) ? "" : ' visibility="hidden"'}><path marker-end="url(#portfolio-edge-arrow)" d="${curve}"/><path class="edge-hit" data-edge-hit="${escape(identity)}" d="${curve}"><title>${escape(edge.source_revision_id)} → ${escape(edge.target_revision_id)}</title></path></g>`;
      return `<path data-edge-id="${escape(identity)}" marker-end="url(#portfolio-edge-arrow)" d="M ${x1} ${y1} C ${middle} ${y1}, ${middle} ${y2}, ${x2} ${y2}"><title>${escape(edge.source_revision_id || edge.source_context_id)} → ${escape(edge.target_revision_id || edge.target_context_id)}</title></path>`;
    }).join("");
    const activityLines = (workbench ? [] : graphActivity).map(item => {
      const target = geometry.positions.get(item.target_context_id);
      if (!target) return "";
      const x = target.x + 110;
      const active = ["authorized", "delivering", "delivered"].includes(item.state) || ["queued", "pending", "running"].includes(item.run?.status);
      return `<g class="directive-path is-${escape(item.state || "unknown")}${active ? " is-active" : ""}" data-directive-id="${escape(item.id)}"><path d="M ${geometry.width / 2} 28 C ${geometry.width / 2} ${Math.max(42, target.y / 2)}, ${x} ${Math.max(42, target.y / 2)}, ${x} ${target.y}" /><circle cx="${x}" cy="${target.y}" r="4" /><title>${escape(item.origin || "Patrol")} → ${escape(item.target_context_id)} · ${escape(item.state)}</title></g>`;
    }).join("");
    const cards = nodes.map(node => {
      const point = geometry.positions.get(node.context_id) || { x: 0, y: 0 };
      const run = runOf(node);
      const evidence = [
        typeof node.counts?.runs === "number" ? `${escape(node.counts.runs)} 次 Run` : "运行记录按需检查",
        node.counts?.workspace_changes ? `${escape(node.counts.workspace_changes)} changes` : "",
        node.counts?.artifacts ? `${escape(node.counts.artifacts)} artifacts` : "",
      ].filter(Boolean).join(" · ");
      const name = node.topic || node.title;
      if (workbench) {
        const selected = node.context_id === selectedId, live = liveIds.has(node.context_id), card = live || featured.has(node.context_id);
        const displayedRevision = selected ? revisionId : node.current_revision_id;
        const inspected = manifest.revisions?.find(item => item.revision_id === displayedRevision);
        const generation = inspected?.generation ?? node.revision?.generation ?? "—";
        const historical = node.historical || (selected && displayedRevision !== node.current_revision_id);
        const status = historical ? "历史来源" : statusLabel(run?.status || node.status);
        const outgoing = new Set(edges.filter(edge => edge.source_revision_id === displayedRevision).map(edge => edge.target_context_id)).size;
        const incoming = new Set(edges.filter(edge => edge.target_revision_id === displayedRevision).map(edge => edge.source_context_id)).size;
        return `<button type="button" class="portfolio-context-node workbench-node ${card ? "is-card" : "is-point"}${live ? " is-live-card" : ""}${!card && point.x > geometry.width - 220 ? " is-label-left" : ""}${selected ? " is-selected" : ""}${related.has(node.context_id) || options.highlightIds?.includes(node.context_id) ? " is-related" : ""}" style="left:${point.x}px;top:${point.y}px;--context-card-width:${live ? sizes.live.width : sizes.card.width}px" data-action="loop-select-context" data-context-id="${escape(node.context_id)}" data-revision-id="${escape(node.current_revision_id || "")}"${live ? ` data-live-run-id="${escape(run.run_id || run.id)}"` : ""} aria-pressed="${selected}" aria-label="${escape(name)} · R${escape(generation)} · ${escape(status)}"${visible(node.context_id) ? "" : " hidden"}><i class="workbench-node-dot is-${escape(historical ? "historical" : run?.status || node.status)}" aria-hidden="true"></i><span class="context-node-eyebrow">${escape(node.context_id.slice(0, 8))} / R${escape(generation)}${live ? '<span class="context-live-state">running</span>' : ""}</span><span class="context-node-top" title="${escape(name || node.context_id)}"><strong>${escape(name || node.context_id)}</strong></span>${live ? livePreview() : `<span class="context-node-meta">${card && selected ? `R${escape(generation)} · ${incoming} 来源 · ${outgoing} 去向` : `${card ? "" : `R${escape(generation)} · `}${escape(status)}`}</span>`}</button>`;
      }
      return `<button type="button" class="portfolio-context-node${node.context_id === selectedId ? " is-selected" : ""}" style="left:${point.x}px;top:${point.y}px" data-action="loop-select-context" data-context-id="${escape(node.context_id)}"><span class="context-node-eyebrow">${escape(node.context_id.slice(0, 8))}<span><i class="run-dot is-${escape(run?.status || node.status)}" aria-hidden="true"></i>${escape(statusLabel(run?.status || node.status))}</span></span><span class="context-node-top"><strong>${escape(name)}</strong></span><span class="context-node-meta">R${escape(node.revision?.generation || "—")} · ${evidence || "暂无运行证据"}</span></button>`;
    }).join("");
    return `<section class="portfolio-map${workbench ? " is-workbench" : ""}" aria-label="Context Portfolio"><div class="portfolio-map-toolbar"><span><strong>${escape(nodes.length)}</strong> 个 Context · 来源关系</span></div><div class="portfolio-map-scroll" tabindex="0" aria-label="上下文图，可用方向键滚动或拖动空白处"><div class="portfolio-map-canvas" style="width:${geometry.width}px;height:${geometry.height}px"><svg width="${geometry.width}" height="${geometry.height}" aria-label="Context 的真实来源关系"><defs><marker id="portfolio-edge-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><polygon points="0 0, 10 5, 0 10" /></marker></defs>${lines}${activityLines}</svg>${cards}</div></div><div class="portfolio-map-zoom glass-surface"><button data-portfolio-zoom="out" aria-label="缩小图">−</button><span data-portfolio-scale>100%</span><button data-portfolio-zoom="in" aria-label="放大图">＋</button><button data-portfolio-zoom="fit">适应画布</button></div></section>`;
  }

  function patchPreviews(host, previews = {}) {
    for (const node of host?.querySelectorAll?.("[data-live-run-id]") || []) {
      const value = previews[node.dataset.contextId];
      if (!value || value.run_id !== node.dataset.liveRunId) continue;
      const preview = node.querySelector("[data-context-preview]");
      if (!preview) continue;
      const status = Object.hasOwn(previewLabels, value.status) ? value.status : "waiting";
      const role = value.role ? `${value.role}${value.tool_name ? ` · ${value.tool_name}` : ""}` : "等待输出";
      const text = Array.from(String(value.text || "")).slice(-4096).join("");
      const notice = String(value.notice || previewLabels[status]);
      if (preview.hidden !== (status === "ended")) preview.hidden = status === "ended";
      if (preview.dataset.previewStatus !== status) preview.dataset.previewStatus = status;
      for (const [selector, content] of [[".context-preview-role", role], [".context-preview-text", text], [".context-preview-status", notice]]) {
        const element = preview.querySelector(selector);
        if (element.textContent !== content) element.textContent = content;
        if (selector !== ".context-preview-text" && element.title !== content) element.title = content;
      }
    }
  }

  function relationship(manifest, identity) {
    const [sourceId, , targetId] = JSON.parse(identity);
    const edges = manifest.edges.filter(edge => edge.source_context_id === sourceId && edge.target_context_id === targetId);
    const endpoint = (id, revisionId) => `<button data-patrol-edge-context="${escape(id)}" data-revision-id="${escape(revisionId)}">${escape(manifest.nodes.find(node => node.context_id === id)?.title || id)}<small>Revision ${escape(revisionId)}</small></button>`;
    return `<p>每一条关系对应精确的来源与目标版本。</p><ul class="workbench-edge-list">${edges.map(edge => `<li>${endpoint(edge.source_context_id, edge.source_revision_id)}<span>→</span>${endpoint(edge.target_context_id, edge.target_revision_id)}</li>`).join("")}</ul>`;
  }

  function bind(host) {
    const map = host?.querySelector(".portfolio-map");
    if (!map || map.dataset.interactive) return;
    map.dataset.interactive = "true";
    const scroll = map.querySelector(".portfolio-map-scroll");
    map.addEventListener("click", event => {
      const button = event.target.closest("[data-portfolio-zoom]");
      if (!button) return;
      event.stopPropagation();
      const canvas = map.querySelector(".portfolio-map-canvas");
      const old = Number(map.dataset.zoom || 1);
      const value = button.dataset.portfolioZoom === "fit"
        ? Math.min(scroll.clientWidth / parseFloat(canvas.style.width), scroll.clientHeight / parseFloat(canvas.style.height))
        : old + (button.dataset.portfolioZoom === "in" ? 0.15 : -0.15);
      const zoom = Math.min(2, Math.max(map.classList.contains("is-workbench") ? 0.72 : 0.25, value));
      map.dataset.zoom = zoom;
      canvas.style.zoom = zoom;
      map.querySelector("[data-portfolio-scale]").textContent = `${Math.round(zoom * 100)}%`;
      if (button.dataset.portfolioZoom === "fit") { scroll.scrollLeft = 0; scroll.scrollTop = 0; }
    });
    scroll.addEventListener("click", event => {
      if (!map.classList.contains("is-workbench") || event.target.closest("button, [data-edge-hit]")) return;
      if (map.dataset.dragged !== "true") map.dispatchEvent(new CustomEvent("portfolio-clear-selection", { bubbles: true }));
      delete map.dataset.dragged;
    });
    scroll.addEventListener("pointerdown", event => {
      if (event.button !== 0 || event.target.closest("button, [data-edge-hit]")) return;
      const x = event.clientX, y = event.clientY, left = scroll.scrollLeft, top = scroll.scrollTop;
      const drag = new AbortController();
      let moved = false;
      scroll.setPointerCapture(event.pointerId);
      scroll.addEventListener("pointermove", e => { moved ||= Math.hypot(x - e.clientX, y - e.clientY) > 5; scroll.scrollLeft = left + x - e.clientX; scroll.scrollTop = top + y - e.clientY; }, { signal: drag.signal });
      for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) scroll.addEventListener(type, () => { drag.abort(); map.dataset.dragged = String(moved); }, { signal: drag.signal, once: true });
    });
  }

  function reconcile(host, manifest, selectedId, graphActivity = [], options = {}) {
    const current = host?.querySelector?.(".portfolio-map");
    if (!current || typeof document !== "object") return false;
    const template = document.createElement("template");
    template.innerHTML = render(manifest, selectedId, graphActivity, options);
    const next = template.content.firstElementChild;
    const currentCanvas = current.querySelector(".portfolio-map-canvas");
    const nextCanvas = next?.querySelector(".portfolio-map-canvas");
    if (!next || !currentCanvas || !nextCanvas) return false;
    const focused = current.contains(document.activeElement) ? document.activeElement : null;
    const toolbar = current.querySelector(".portfolio-map-toolbar"), nextToolbar = next.querySelector(".portfolio-map-toolbar");
    if (toolbar && nextToolbar && toolbar.innerHTML !== nextToolbar.innerHTML) toolbar.replaceWith(nextToolbar);
    if (current.dataset.zoom) nextCanvas.style.zoom = current.dataset.zoom;
    if (currentCanvas.getAttribute("style") !== nextCanvas.getAttribute("style")) currentCanvas.setAttribute("style", nextCanvas.getAttribute("style") || "");
    const currentSvg = currentCanvas.querySelector("svg");
    const nextSvg = nextCanvas.querySelector("svg");
    if (currentSvg && nextSvg) {
      for (const name of ["width", "height"]) if (currentSvg.getAttribute(name) !== nextSvg.getAttribute(name)) currentSvg.setAttribute(name, nextSvg.getAttribute(name));
      const existingShapes = new Map([...currentSvg.querySelectorAll("[data-edge-id], [data-directive-id]")].map(item => [item.getAttribute("data-edge-id") || `directive:${item.getAttribute("data-directive-id")}`, item]));
      let previousShape = currentSvg.querySelector("defs");
      for (const shape of [...nextSvg.querySelectorAll("[data-edge-id], [data-directive-id]")]) {
        const key = shape.getAttribute("data-edge-id") || `directive:${shape.getAttribute("data-directive-id")}`;
        const prior = existingShapes.get(key);
        let placed = shape;
        if (prior) {
          existingShapes.delete(key);
          if (prior.outerHTML !== shape.outerHTML) prior.replaceWith(shape);
          else placed = prior;
        }
        const reference = previousShape ? previousShape.nextSibling : currentSvg.firstChild;
        if (placed !== reference) currentSvg.insertBefore(placed, reference);
        previousShape = placed;
      }
      existingShapes.forEach(shape => shape.remove());
    }
    const existingNodes = new Map([...currentCanvas.querySelectorAll(".portfolio-context-node")].map(node => [node.dataset.contextId, node]));
    let previousNode = currentSvg;
    for (const node of [...nextCanvas.querySelectorAll(".portfolio-context-node")]) {
      const prior = existingNodes.get(node.dataset.contextId);
      const placed = prior || node;
      if (prior) {
        existingNodes.delete(node.dataset.contextId);
        const priorPreview = prior.dataset.liveRunId && prior.dataset.liveRunId === node.dataset.liveRunId ? prior.querySelector("[data-context-preview]") : null;
        if (priorPreview) node.querySelector("[data-context-preview]")?.replaceWith(priorPreview.cloneNode(true));
        if (prior.className !== node.className) prior.className = node.className;
        if (prior.getAttribute("style") !== node.getAttribute("style")) prior.setAttribute("style", node.getAttribute("style") || "");
        for (const name of ["aria-pressed", "aria-label", "data-revision-id", "data-live-run-id", "hidden"]) {
          if (node.hasAttribute(name)) { if (prior.getAttribute(name) !== node.getAttribute(name)) prior.setAttribute(name, node.getAttribute(name)); }
          else prior.removeAttribute(name);
        }
        if (prior.innerHTML !== node.innerHTML) {
          prior.innerHTML = node.innerHTML;
          if (priorPreview) prior.querySelector("[data-context-preview]")?.replaceWith(priorPreview);
        }
      }
      const reference = previousNode ? previousNode.nextSibling : currentCanvas.firstChild;
      if (placed !== reference) currentCanvas.insertBefore(placed, reference);
      previousNode = placed;
    }
    existingNodes.forEach(node => node.remove());
    if (focused?.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
    return true;
  }

  return Object.freeze({ render, reconcile, bind, relationship, patchPreviews });
});
