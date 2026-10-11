/* 本文件对外提供共享图布局和真实版本呈现的定向合同测试。
 * 输入为有自环、多来源、多版本、历史祖先及 128 个同时运行的规范化图；输出为稳定几何、精确版本、运行卡覆盖与连线端点断言。
 * 工作流为读取纯布局和现有视图，检查可观察行为；示例：node --test desktop/portfolio-map-layout.test.cjs。
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const { layout, nodeSizes } = require("./portfolio-map-layout.js");
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

test("console latest Run survives shared lineage adaptation without lending it to history", () => {
  global.FocusLoopLiveSelectors = require("./loop-live-selectors.js");
  const snapshot = { roots: {a:"a1"}, nodes:[{context_id:"a",revision_id:"a1",generation:1},{context_id:"h",revision_id:"h1",generation:1}],edges:[] };
  const contexts = Object.fromEntries(["a","h"].map(id => [id,{entity_id:id,state:{title:id,latest_run:{run_id:"run-"+id,status:"running"}}}]));
  const manifest = view.lineageManifest(snapshot,{contexts});
  assert.equal(manifest.nodes[0].latest_run.status,"running");
  assert.equal(manifest.nodes[1].latest_run,null);
  assert.equal(view.lineageManifest(snapshot,{contexts,runs:{}}).nodes[0].latest_run,null);
  delete global.FocusLoopLiveSelectors;
});

test("every running root has a live card beyond the featured limit and retains exact history boundaries", () => {
  const nodes = Array.from({ length: 8 }, (_, i) => ({ context_id: `live-${i}`, current_revision_id: `r-${i}`, revision: { generation: 1 }, latest_run: { run_id: `run-${i}`, status: "running" } }));
  nodes.push({ context_id: "history", historical: true, latest_run: { run_id: "old", status: "running" } });
  for (const status of ["queued", "pending", "success", "error", "interrupted", "timeout"]) nodes.push({ context_id: status, latest_run: { run_id: status, status } });
  const manifest = { nodes, edges: [] };
  const live = graph.render(manifest, null, [], { presentation: "workbench" });
  assert.equal((live.match(/data-live-run-id=/g) || []).length, 8);
  assert.equal((live.match(/data-context-preview/g) || []).length, 8);
  assert.doesNotMatch(graph.render(manifest), /data-live-run-id|context-live-preview/);
  const historical = graph.render(manifest, "live-0", [], { presentation: "workbench", selectedRevisionId: "old-r" });
  assert.equal((historical.match(/data-live-run-id=/g) || []).length, 7);
  assert.doesNotMatch(historical, /data-live-run-id="run-0"/);
  nodes[0].active_run = nodes[0].latest_run;
  nodes[0].latest_run = {run_id:"queued-after-running",status:"queued"};
  assert.match(graph.render(manifest, null, [], {presentation:"workbench"}), /data-live-run-id="run-0"/);
});

test("fixed live envelopes never overlap and status changes do not relayout", () => {
  for (const count of [1, 2, 7, 32, 128]) {
    const nodes = Array.from({ length: count }, (_, i) => ({ context_id: `n-${i}` }));
    const edges = nodes.slice(1).map((node, i) => ({ source_context_id: nodes[i].context_id, target_context_id: node.context_id }));
    const result = layout(nodes, edges, "workbench");
    assert.equal(result, layout(nodes.map(node => ({ ...node, latest_run: { status: "running" }, title: "很长的实时标题" })), edges, "workbench"));
    const points = [...result.positions.values()];
    for (const [i, point] of points.entries()) {
      assert.ok(point.x - nodeSizes.live.width / 2 >= 0 && point.x + nodeSizes.live.width / 2 <= result.width);
      assert.ok(point.y - nodeSizes.live.height / 2 >= 0 && point.y + nodeSizes.live.height / 2 <= result.height);
      for (const other of points.slice(i + 1)) assert.ok(Math.abs(point.x - other.x) >= nodeSizes.live.width || Math.abs(point.y - other.y) >= nodeSizes.live.height, `overlap at ${count}: ${JSON.stringify([point, other])}`);
    }
  }
});

test("source curves meet actual live and static card widths", () => {
  const nodes = [
    { context_id: "a", current_revision_id: "a1", latest_run: { run_id: "a-run", status: "running" } },
    { context_id: "b", current_revision_id: "b1" },
  ];
  const edges = [{ source_context_id: "a", source_revision_id: "a1", target_context_id: "b", target_revision_id: "b1" }];
  const geometry = layout(nodes, edges, "workbench");
  const a = geometry.positions.get("a"), b = geometry.positions.get("b");
  const html = graph.render({ nodes, edges }, "b", [], { presentation: "workbench" });
  const x1 = a.x + nodeSizes.live.width / 2, x2 = b.x - nodeSizes.card.width / 2;
  assert.ok(html.includes(`M ${x1} ${a.y} C ${(x1 + x2) / 2} ${a.y}, ${(x1 + x2) / 2} ${b.y}, ${x2} ${b.y}`));
});
