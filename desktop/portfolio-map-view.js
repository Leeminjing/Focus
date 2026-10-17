/*
 * 本文件对外提供 Context Portfolio 的稳定拓扑图视图。
 * 输入为轻量 nodes/edges、选中 Context 与 canonical directive/Run 活动；输出为简短节点、精确来源及单层控制材质的缩放/平移横向演化图。
 * 具体工作流为仅在拓扑变化时建立邻接/度数/精确版本来源索引与排序，普通状态沿用位置和 HTML 单元缓存，对账只生成变化节点及宽度受影响的连线片段；未变属性/工具条不写回活动 DOM。
 * 每个挂载图独立保留输入签名与 DOM 身份，输入仍做线性拓扑比较；活动效果严格由 directive lifecycle 和 Run state 驱动，空闲时不循环播放；
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

  const mountedPlans = new WeakMap();
  const edgeIdentity = edge => JSON.stringify([edge.source_context_id, edge.source_revision_id ?? null, edge.target_context_id, edge.target_revision_id ?? null]);

  // Compare topology once per update. Ordinary Run changes reuse geometry and all edge indexes.
  function prepare(manifest, selectedId, graphActivity = [], options = {}, previous) {
    const nodes = manifest?.nodes || [];
    const workbench = options.presentation === "workbench";
    const edges = manifest?.edges || [];
    const topologyKey = JSON.stringify([options.presentation, nodes.map(node => node.context_id), edges.map(edgeIdentity)]);
    let topology = previous?.topology;
    if (!topology || topology.key !== topologyKey) {
      const validEdges = edges.filter(edge => edge.target_context_id);
      const incident = new Map(), degree = new Map(), sources = new Map(), consumers = new Map();
      const add = (map, key, value) => {
        if (!map.has(key)) map.set(key, new Set());
        map.get(key).add(value);
      };
      for (const edge of validEdges) {
        const source = edge.source_context_id, target = edge.target_context_id;
        add(incident, source, edge);
        if (source !== target) add(incident, target, edge);
        degree.set(source, (degree.get(source) || 0) + 1);
        if (source !== target) degree.set(target, (degree.get(target) || 0) + 1);
        add(consumers, edge.source_revision_id, target);
        add(sources, edge.target_revision_id, source);
      }
      const featureOrder = nodes.map(node => node.context_id).sort((a, b) => (degree.get(b) || 0) - (degree.get(a) || 0) || a.localeCompare(b));
      topology = { key: topologyKey, edges: validEdges, incident, sources, consumers, featureOrder,
        geometry: layout(nodes, validEdges, options.presentation) };
    }
    const geometry = topology.geometry;
    const revisionId = options.selectedRevisionId || nodes.find(node => node.context_id === selectedId)?.current_revision_id;
    const revisions = new Map((manifest?.revisions || []).map(revision => [revision.revision_id, revision]));
    const liveIds = new Set(nodes.filter(node => !node.historical && !(node.context_id === selectedId && revisionId !== node.current_revision_id) && runOf(node)?.status === "running" && (runOf(node).run_id || runOf(node).id)).map(node => node.context_id));
    const adjacent = edge => (edge.source_context_id === selectedId && edge.source_revision_id === revisionId) || (edge.target_context_id === selectedId && edge.target_revision_id === revisionId);
    const related = new Set([selectedId]);
    for (const edge of topology.incident.get(selectedId) || []) {
      if (adjacent(edge)) { related.add(edge.source_context_id); related.add(edge.target_context_id); }
    }
    const featured = new Set(selectedId && !liveIds.has(selectedId) ? [selectedId] : []);
    const candidates = workbench ? topology.featureOrder : [];
    for (const id of candidates) {
      if (featured.size >= 5) break;
      if (liveIds.has(id) || (selectedId && !related.has(id))) continue;
      const point = geometry.positions.get(id);
      if ([...featured].every(id => {
        const peer = geometry.positions.get(id);
        return !peer || ((point.x - peer.x) / 240) ** 2 + ((point.y - peer.y) / 150) ** 2 > 1.7;
      })) featured.add(id);
    }
    const sizes = geometryApi().nodeSizes;
    const widths = new Map(nodes.map(node => [node.context_id, liveIds.has(node.context_id) ? sizes.live.width / 2 : featured.has(node.context_id) ? sizes.card.width / 2 : 0]));
    const halfWidth = id => widths.get(id) || 0;
    const visible = id => !options.relatedOnly || !selectedId || related.has(id);
    const highlights = new Set(options.highlightIds || []);
    const renderEdge = edge => {
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
      const identity = edgeIdentity(edge);
      const curve = self ? `M ${x1} ${y1} C ${x1 + 64} ${y1 - 100}, ${x2 - 64} ${y2 - 100}, ${x2} ${y2}` : `M ${x1} ${y1} C ${middle} ${y1}, ${middle} ${y2}, ${x2} ${y2}`;
      if (workbench) return `<g data-edge-id="${escape(identity)}" class="workbench-edge${adjacent(edge) ? " is-related" : ""}"${visible(edge.source_context_id) && visible(edge.target_context_id) ? "" : ' visibility="hidden"'}><path marker-end="url(#portfolio-edge-arrow)" d="${curve}"/><path class="edge-hit" data-edge-hit="${escape(identity)}" d="${curve}"><title>${escape(edge.source_revision_id)} → ${escape(edge.target_revision_id)}</title></path></g>`;
      return `<path data-edge-id="${escape(identity)}" marker-end="url(#portfolio-edge-arrow)" d="M ${x1} ${y1} C ${middle} ${y1}, ${middle} ${y2}, ${x2} ${y2}"><title>${escape(edge.source_revision_id || edge.source_context_id)} → ${escape(edge.target_revision_id || edge.target_context_id)}</title></path>`;
    };
    const renderActivity = item => {
      const target = geometry.positions.get(item.target_context_id);
      if (!target) return "";
      const x = target.x + 110;
      const active = ["authorized", "delivering", "delivered"].includes(item.state) || ["queued", "pending", "running"].includes(item.run?.status);
      return `<g class="directive-path is-${escape(item.state || "unknown")}${active ? " is-active" : ""}" data-directive-id="${escape(item.id)}"><path d="M ${geometry.width / 2} 28 C ${geometry.width / 2} ${Math.max(42, target.y / 2)}, ${x} ${Math.max(42, target.y / 2)}, ${x} ${target.y}" /><circle cx="${x}" cy="${target.y}" r="4" /><title>${escape(item.origin || "Patrol")} → ${escape(item.target_context_id)} · ${escape(item.state)}</title></g>`;
    };
    const renderCard = node => {
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
        const inspected = revisions.get(displayedRevision);
        const generation = inspected?.generation ?? node.revision?.generation ?? "—";
        const historical = node.historical || (selected && displayedRevision !== node.current_revision_id);
        const status = historical ? "历史来源" : statusLabel(run?.status || node.status);
        const outgoing = topology.consumers.get(displayedRevision)?.size || 0;
        const incoming = topology.sources.get(displayedRevision)?.size || 0;
        return `<button type="button" class="portfolio-context-node workbench-node ${card ? "is-card" : "is-point"}${live ? " is-live-card" : ""}${!card && point.x > geometry.width - 220 ? " is-label-left" : ""}${selected ? " is-selected" : ""}${related.has(node.context_id) || highlights.has(node.context_id) ? " is-related" : ""}" style="left:${point.x}px;top:${point.y}px;--context-card-width:${live ? sizes.live.width : sizes.card.width}px" data-action="loop-select-context" data-context-id="${escape(node.context_id)}" data-revision-id="${escape(node.current_revision_id || "")}"${live ? ` data-live-run-id="${escape(run.run_id || run.id)}"` : ""} aria-pressed="${selected}" aria-label="${escape(name)} · R${escape(generation)} · ${escape(status)}"${visible(node.context_id) ? "" : " hidden"}><i class="workbench-node-dot is-${escape(historical ? "historical" : run?.status || node.status)}" aria-hidden="true"></i><span class="context-node-eyebrow">${escape(node.context_id.slice(0, 8))} / R${escape(generation)}${live ? '<span class="context-live-state">running</span>' : ""}</span><span class="context-node-top" title="${escape(name || node.context_id)}"><strong>${escape(name || node.context_id)}</strong></span>${live ? livePreview() : `<span class="context-node-meta">${card && selected ? `R${escape(generation)} · ${incoming} 来源 · ${outgoing} 去向` : `${card ? "" : `R${escape(generation)} · `}${escape(status)}`}</span>`}</button>`;
      }
      return `<button type="button" class="portfolio-context-node${node.context_id === selectedId ? " is-selected" : ""}" style="left:${point.x}px;top:${point.y}px" data-action="loop-select-context" data-context-id="${escape(node.context_id)}"><span class="context-node-eyebrow">${escape(node.context_id.slice(0, 8))}<span><i class="run-dot is-${escape(run?.status || node.status)}" aria-hidden="true"></i>${escape(statusLabel(run?.status || node.status))}</span></span><span class="context-node-top"><strong>${escape(name)}</strong></span><span class="context-node-meta">R${escape(node.revision?.generation || "—")} · ${evidence || "暂无运行证据"}</span></button>`;
    };
    const sameTopology = previous?.topology === topology;
    const cards = new Map();
    const changedCards = new Map();
    for (const node of nodes) {
      const id = node.context_id, run = runOf(node);
      const signature = JSON.stringify([node.topic || node.title, node.status, node.historical, node.current_revision_id,
        node.revision?.generation, node.counts, run?.status, run?.run_id || run?.id, id === selectedId, revisionId,
        revisions.get(id === selectedId ? revisionId : node.current_revision_id)?.generation,
        liveIds.has(id), featured.has(id), related.has(id), highlights.has(id), visible(id)]);
      const prior = sameTopology && previous.cards.get(id);
      cards.set(id, prior?.signature === signature ? prior : { signature, html: renderCard(node) });
      if (cards.get(id) !== prior) changedCards.set(id, cards.get(id));
    }
    const selectionKey = JSON.stringify([selectedId, revisionId, Boolean(options.relatedOnly)]);
    const dirtyEdges = new Set();
    if (sameTopology && previous.selectionKey === selectionKey && workbench) {
      for (const [id, width] of widths) if (previous.widths.get(id) !== width) {
        for (const edge of topology.incident.get(id) || []) dirtyEdges.add(edge);
      }
    } else if (!sameTopology || previous.selectionKey !== selectionKey) {
      for (const edge of topology.edges) dirtyEdges.add(edge);
    }
    const lines = sameTopology ? (dirtyEdges.size ? new Map(previous.lines) : previous.lines) : new Map();
    const changedLines = new Map();
    for (const edge of dirtyEdges) {
      const key = edgeIdentity(edge), part = { html: renderEdge(edge) };
      lines.set(key, part);
      if (!sameTopology || previous.lines.get(key)?.html !== part.html) changedLines.set(key, part);
    }
    const activityLines = new Map();
    for (const item of (workbench ? [] : graphActivity)) {
      const signature = JSON.stringify(item);
      const prior = sameTopology && previous.activityLines.get(item.id);
      activityLines.set(item.id, prior?.signature === signature ? prior : { signature, html: renderActivity(item) });
    }
    return { topology, cards, changedCards, lines, changedLines, activityLines, widths, selectionKey, workbench, count: nodes.length };
  }

  function render(manifest, selectedId, graphActivity = [], options = {}) {
    if (!manifest?.nodes?.length) return '<section class="loop-map-empty">尚无 Context</section>';
    const plan = prepare(manifest, selectedId, graphActivity, options);
    const { workbench, topology: { geometry }, count } = plan;
    const lines = [...plan.lines.values()].map(item => item.html).join("");
    const activityLines = [...plan.activityLines.values()].map(item => item.html).join("");
    const cards = [...plan.cards.values()].map(item => item.html).join("");
    return `<section class="portfolio-map${workbench ? " is-workbench" : ""}" aria-label="Context Portfolio"><div class="portfolio-map-toolbar"><span><strong>${escape(count)}</strong> 个 Context · 来源关系</span></div><div class="portfolio-map-scroll" tabindex="0" aria-label="上下文图，可用方向键滚动或拖动空白处"><div class="portfolio-map-canvas" style="width:${geometry.width}px;height:${geometry.height}px"><svg width="${geometry.width}" height="${geometry.height}" aria-label="Context 的真实来源关系"><defs><marker id="portfolio-edge-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><polygon points="0 0, 10 5, 0 10" /></marker></defs>${lines}${activityLines}</svg>${cards}</div></div><div class="portfolio-map-zoom glass-surface"><button data-portfolio-zoom="out" aria-label="缩小图">−</button><span data-portfolio-scale>100%</span><button data-portfolio-zoom="in" aria-label="放大图">＋</button><button data-portfolio-zoom="fit">适应画布</button></div></section>`;
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
    if (!current || typeof document !== "object" || !manifest?.nodes?.length) return false;
    const canvas = current.querySelector(".portfolio-map-canvas");
    const svg = canvas?.querySelector("svg");
    if (!canvas || !svg) return false;
    const state = mountedPlans.get(current);
    const previous = state?.plan;
    const plan = prepare(manifest, selectedId, graphActivity, options, previous);
    const structural = previous?.topology !== plan.topology;
    const nodes = state?.nodes || new Map([...canvas.querySelectorAll(".portfolio-context-node")].map(node => [node.dataset.contextId, node]));
    const shapes = state?.shapes || new Map([...svg.querySelectorAll("[data-edge-id], [data-directive-id]")].map(shape => [shape.hasAttribute("data-edge-id") ? `edge:${shape.getAttribute("data-edge-id")}` : `directive:${shape.getAttribute("data-directive-id")}`, shape]));
    const focused = current.contains(document.activeElement) ? document.activeElement : null;
    const parse = (html, isSvg = false) => {
      const template = document.createElement("template");
      template.innerHTML = isSvg ? `<svg>${html}</svg>` : html;
      return isSvg ? template.content.firstElementChild.firstElementChild : template.content.firstElementChild;
    };
    if (structural) {
      current.classList.toggle("is-workbench", plan.workbench);
      const { width, height } = plan.topology.geometry;
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      for (const [name, value] of [["width", width], ["height", height]]) if (svg.getAttribute(name) !== String(value)) svg.setAttribute(name, value);
      if (!previous || previous.count !== plan.count) {
        const count = current.querySelector(".portfolio-map-toolbar strong");
        if (count && count.textContent !== String(plan.count)) count.textContent = String(plan.count);
      }
    }
    const reorderShapes = structural || JSON.stringify([...plan.activityLines.keys()]) !== JSON.stringify([...(previous?.activityLines.keys() || [])]);
    const nextShapes = new Map([...(reorderShapes ? plan.lines : plan.changedLines)].map(([key, value]) => [`edge:${key}`, value]));
    for (const [key, value] of plan.activityLines) nextShapes.set(`directive:${key}`, value);
    let previousShape = svg.querySelector("defs");
    for (const [key, part] of nextShapes) {
      let placed = shapes.get(key);
      const priorPart = key.startsWith("edge:") ? previous?.lines.get(key.slice(5)) : previous?.activityLines.get(key.slice(10));
      if (!placed || priorPart?.html !== part.html) {
        const next = part.html ? parse(part.html, true) : null;
        if (placed) { if (next) placed.replaceWith(next); else placed.remove(); }
        else if (next) svg.appendChild(next);
        placed = next;
        if (placed) shapes.set(key, placed); else shapes.delete(key);
      }
      if (!placed) continue;
      if (reorderShapes) {
        const reference = previousShape ? previousShape.nextSibling : svg.firstChild;
        if (placed !== reference) svg.insertBefore(placed, reference);
      }
      previousShape = placed;
    }
    if (structural) {
      for (const [key, shape] of shapes) if (!nextShapes.has(key)) { shape.remove(); shapes.delete(key); }
    } else {
      for (const key of previous.activityLines.keys()) if (!plan.activityLines.has(key)) {
        shapes.get(`directive:${key}`)?.remove(); shapes.delete(`directive:${key}`);
      }
    }
    let previousNode = svg;
    for (const [id, part] of (structural ? plan.cards : plan.changedCards)) {
      let prior = nodes.get(id);
      if (!prior || previous?.cards.get(id)?.html !== part.html) {
        const node = parse(part.html);
        if (!prior) { prior = node; nodes.set(id, prior); canvas.appendChild(prior); }
        else {
          const priorPreview = prior.dataset.liveRunId && prior.dataset.liveRunId === node.dataset.liveRunId ? prior.querySelector("[data-context-preview]") : null;
          if (priorPreview) node.querySelector("[data-context-preview]")?.replaceWith(priorPreview.cloneNode(true));
          if (prior.className !== node.className) prior.className = node.className;
          if (prior.getAttribute("style") !== node.getAttribute("style")) prior.setAttribute("style", node.getAttribute("style") || "");
          for (const name of ["aria-pressed", "aria-label", "data-revision-id", "data-live-run-id", "hidden"]) {
            if (node.hasAttribute(name)) { if (prior.getAttribute(name) !== node.getAttribute(name)) prior.setAttribute(name, node.getAttribute(name)); }
            else if (prior.hasAttribute(name)) prior.removeAttribute(name);
          }
          if (prior.innerHTML !== node.innerHTML) {
            prior.innerHTML = node.innerHTML;
            if (priorPreview) prior.querySelector("[data-context-preview]")?.replaceWith(priorPreview);
          }
        }
      }
      if (structural) {
        const reference = previousNode ? previousNode.nextSibling : canvas.firstChild;
        if (prior !== reference) canvas.insertBefore(prior, reference);
      }
      previousNode = prior;
    }
    if (structural) for (const [id, node] of nodes) if (!plan.cards.has(id)) { node.remove(); nodes.delete(id); }
    mountedPlans.set(current, { plan, nodes, shapes });
    if (focused?.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
    return true;
  }

  return Object.freeze({ render, reconcile, bind, relationship, patchPreviews });
});
