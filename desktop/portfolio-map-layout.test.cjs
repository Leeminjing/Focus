/* 本文件对外提供共享图布局和真实版本呈现的定向合同测试。
 * 输入为有自环、多来源、多版本和历史祖先的规范化图；输出为稳定几何、精确版本与验证计数断言。
 * 工作流为读取纯布局和现有视图，检查可观察行为；示例：node --test desktop/portfolio-map-layout.test.cjs。
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const { layout } = require("./portfolio-map-layout.js");
const graph = require("./portfolio-map-view.js");
const view = require("./workspace-patrol-view.js");

test("published roots win over newer ancestor revisions and preserve every edge", () => {
  const snapshot = {
    roots: { a: "a1", b: "b2" },
    nodes: [{ context_id: "a", revision_id: "a9", generation: 9 }, { context_id: "a", revision_id: "a1", generation: 1 }, { context_id: "b", revision_id: "b2", generation: 2 }, { context_id: "history", revision_id: "h1", generation: 1 }],
    edges: ["a1", "a9"].map(source_revision_id => ({ source_context_id: "a", source_revision_id, target_context_id: "b", target_revision_id: "b2" })),
  };
  const manifest = view.lineageManifest(snapshot, {});
  assert.equal(manifest.nodes.find(node => node.context_id === "a").current_revision_id, "a1");
  assert.equal(manifest.nodes.find(node => node.context_id === "history").historical, true);
  assert.deepEqual(manifest.edges, snapshot.edges);
  const html = graph.render(manifest, "a", [], { presentation: "workbench", selectedRevisionId: "a9" });
  assert.equal((html.match(/data-edge-hit=/g) || []).length, 2);
  assert.match(html, /R9/);
  assert.match(html, /历史来源/);
  assert.equal((html.match(/workbench-edge is-related/g) || []).length, 1);
  const withSelf = { ...manifest, edges: [...manifest.edges, { source_context_id: "a", source_revision_id: "a9", target_context_id: "a", target_revision_id: "a1" }] };
  assert.equal((graph.render(withSelf, "a", [], {presentation:"workbench", selectedRevisionId:"a9"}).match(/data-edge-hit=/g) || []).length, 3);
});

test("selection and metadata updates preserve finite topology positions in both presentations", () => {
  for (const count of [1, 32, 128]) {
    const nodes = Array.from({ length: count }, (_, i) => ({ context_id: `node-${i}` }));
    const edges = nodes.slice(1).map((node, i) => ({ source_context_id: nodes[i].context_id, target_context_id: node.context_id }));
    edges.push({ source_context_id: "node-0", target_context_id: "node-0" });
    for (const presentation of ["default", "workbench"]) {
      const original = layout(nodes, edges, presentation);
      const updated = layout(nodes.map(node => ({ ...node, title: "新版本状态", status: "running" })), edges, presentation);
      assert.equal(original, updated);
      assert.equal(original.positions.size, count);
      if (count === 1 && presentation === "workbench") assert.deepEqual(original.positions.get("node-0"), { x: original.width / 2, y: original.height / 2 });
      assert.equal(new Set([...original.positions.values()].map(({x,y}) => `${x}:${y}`)).size, count);
      assert.ok([...original.positions.values()].every(({x,y}) => Number.isFinite(x) && Number.isFinite(y) && x >= 0 && y >= 0 && x <= original.width && y <= original.height));
    }
  }
});

test("only independently supported completed items count as verified", () => {
  const html = view.progress({ generation: 2, document: { items: [
    { description: "supported", state: "completed", support: "supported", context_ids: ["a"] },
    { description: "asserted", state: "completed", support: "asserted", context_ids: [] },
    { description: "running", state: "in_progress", support: "supported", context_ids: ["a", "b"] },
  ] } });
  assert.match(html, /patrol-progress-number">1<small> \/ 3/);
  assert.equal((html.match(/class="is-verified"/g) || []).length, 1);
  assert.match(html, /2 条工作线/);
  assert.match(view.progress({ document: { items: [] } }), /暂无条目/);
});
