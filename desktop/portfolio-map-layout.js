/* 本文件对外提供共享 Portfolio 图的纯 layout 函数。
 * 输入为 Context 节点、精确来源边与 default/workbench 呈现；输出为稳定坐标及画布尺寸。
 * 工作流为拓扑排序后进行有限层内排序，工作台折叠为有界列并为每个节点预留固定运行卡包络；缓存只由拓扑和呈现决定，不接触 DOM、网络或业务状态。
 * nodeSizes 提供同一节点的 default、card、live、point 尺寸，用于布局与连线端点，运行内容变化不改变坐标。
 * 示例：FocusPortfolioMapLayout.layout(nodes, edges, "workbench")。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusPortfolioMapLayout = api;
})(globalThis, function () {
  "use strict";
  const caches = new Map();
  const nodeSizes = Object.freeze({
    default: Object.freeze({ width: 220, height: 96 }),
    card: Object.freeze({ width: 188, height: 82 }),
    live: Object.freeze({ width: 280, height: 164 }),
    point: Object.freeze({ width: 164, height: 42 }),
  });

  function layout(nodes, edges, presentation = "default") {
    const fingerprint = JSON.stringify({ presentation, nodes: nodes.map(node => node.context_id).sort(), edges: edges.map(edge => [edge.source_context_id, edge.target_context_id]).sort() });
    if (caches.has(fingerprint)) return caches.get(fingerprint);
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
    const workbench = presentation === "workbench";
    const ordered = [...layers.entries()].sort((a, b) => a[0] - b[0]).flatMap(([, ids]) => ids);
    if (workbench) {
      const columns = Math.max(1, Math.min(ids.length, 6, Math.ceil(Math.sqrt(ids.length * 1.15))));
      const rows = Math.ceil(ids.length / columns);
      const bands = Array.from({ length: columns }, () => []);
      ordered.forEach((id, index) => bands[Math.min(columns - 1, Math.floor(index / rows))].push(id));
      const rank = new Map(ordered.map((id, index) => [id, index]));
      for (let pass = 0; pass < 3; pass++) {
        for (const band of bands) {
          const score = id => {
            const peers = edges.filter(edge => edge.target_context_id === id || edge.source_context_id === id)
              .map(edge => rank.get(edge.source_context_id === id ? edge.target_context_id : edge.source_context_id)).filter(Number.isFinite);
            return peers.length ? peers.reduce((sum, value) => sum + value, 0) / peers.length : rank.get(id);
          };
          band.sort((a, b) => score(a) - score(b) || a.localeCompare(b));
          band.forEach((id, index) => rank.set(id, index));
        }
      }
      const degree = id => edges.filter(edge => edge.source_context_id === id || edge.target_context_id === id).length;
      for (const band of bands) {
        const anchor = [...band].sort((a, b) => degree(b) - degree(a) || a.localeCompare(b))[0];
        if (anchor && band.length > 2) { band.splice(band.indexOf(anchor), 1); band.splice(Math.floor(band.length / 2), 0, anchor); }
      }
      const columnSpace = nodeSizes.live.width + 56, rowSpace = nodeSizes.live.height + 48;
      const width = Math.max(900, columns * columnSpace), height = Math.max(620, rows * rowSpace + 90);
      bands.forEach((band, column) => band.forEach((id, row) => {
        const x = columns === 1 ? width / 2 : columnSpace / 2 + column * (width - columnSpace) / (columns - 1);
        const y = band.length === 1 ? height / 2 : rowSpace / 2 + row * (height - rowSpace - 90) / (band.length - 1);
        positions.set(id, { x: x + (columns === 1 ? 0 : row % 2 ? -12 : 12), y: y + (column % 2 ? 26 : 0) });
      }));
      const result = { fingerprint, positions, width, height };
      if (caches.size >= 4) caches.delete(caches.keys().next().value);
      caches.set(fingerprint, result);
      return result;
    }
    const cardWidth = nodeSizes.default.width;
    const xGap = 34;
    const yGap = 142;
    const largestLayer = Math.max(1, ...[...layers.values()].map(layer => layer.length));
    const width = Math.max(880, 72 + (Math.max(0, ...depth.values()) + 1) * (cardWidth + xGap));
    [...layers.entries()].sort((a, b) => a[0] - b[0]).forEach(([row, layer]) => {
      layer.sort();
      const start = 54 + (largestLayer - layer.length) * yGap / 2;
      layer.forEach((id, column) => positions.set(id, { x: 36 + row * (cardWidth + xGap), y: start + column * yGap }));
    });
    const result = {
      fingerprint,
      positions,
      width,
      height: Math.max(430, 96 + largestLayer * yGap),
    };
    if (caches.size >= 4) caches.delete(caches.keys().next().value);
    caches.set(fingerprint, result);
    return result;
  }

  return Object.freeze({ layout, nodeSizes });
});
