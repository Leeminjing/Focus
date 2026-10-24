/* 可复跑的图谱计算基准；每节点最多四条前驱边，七次取中位数。
 * node desktop/scripts/portfolio-map-benchmark.cjs [--compare-head]
 * HTML 生成不含 DOM；reconcile 使用测试 DOM，记录提交解析的字节量，不代表浏览器帧率。
 */
const { performance } = require("node:perf_hooks");
const { execFileSync } = require("node:child_process");
const vm = require("node:vm");
const path = require("node:path");
const fs = require("node:fs");
const { createDocument, createElement } = require("../tests/helpers/test-dom.cjs");

function loadGraph(fromHead = false) {
  const context = vm.createContext({ module: { exports: {} } });
  const root = path.resolve(__dirname, "../..");
  const headFiles = fromHead ? execFileSync("git", ["ls-tree", "-r", "--name-only", "HEAD", "desktop"], { cwd: root, encoding: "utf8" }).trim().split("\n") : [];
  for (const file of ["../src/renderer/features/map/portfolio-map-layout.js", "../src/renderer/features/map/portfolio-map-view.js"]) {
    const headPath = headFiles.find(candidate => path.posix.basename(candidate) === path.basename(file));
    const source = fromHead
      ? execFileSync("git", ["show", `HEAD:${headPath}`], { cwd: root, encoding: "utf8" })
      : fs.readFileSync(path.join(__dirname, file), "utf8");
    vm.runInContext(source, context);
  }
  return { graph: context.FocusPortfolioMapView, setDocument: document => { context.document = document; } };
}

function measure(label, graph, setDocument) {
  const rows = [];
  for (const count of [64, 128, 256]) {
    const nodes = Array.from({ length: count }, (_, i) => ({ context_id: `n-${i}`, title: `Context ${i}`, current_revision_id: `r-${i}` }));
    const edges = nodes.flatMap((node, i) => nodes.slice(Math.max(0, i - 4), i).map(source => ({ source_context_id: source.context_id, source_revision_id: source.current_revision_id, target_context_id: node.context_id, target_revision_id: node.current_revision_id })));
    const manifest = { nodes, edges }, options = { presentation: "workbench" };
    const render = () => graph.render(manifest, null, [], options);
    let start = performance.now();
    render();
    const firstHtmlMs = performance.now() - start;
    const samples = Array.from({ length: 7 }, () => { start = performance.now(); render(); return performance.now() - start; }).sort((a, b) => a - b);
    const document = createDocument(), create = document.createElement;
    let parsedBytes = 0;
    document.createElement = tag => {
      const element = create(tag);
      if (tag === "template") {
        const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), "innerHTML");
        Object.defineProperty(element, "innerHTML", { get() { return descriptor.get.call(this); }, set(html) { parsedBytes += Buffer.byteLength(html); descriptor.set.call(this, html); } });
      }
      return element;
    };
    setDocument(document);
    const host = createElement("div");
    host.innerHTML = render();
    graph.reconcile(host, manifest, null, [], options);
    const updates = [], bytes = [];
    for (let i = 0; i < 7; i++) {
      nodes[0].latest_run = { run_id: "benchmark-run", status: i % 2 ? "running" : "success" };
      parsedBytes = 0;
      start = performance.now();
      graph.reconcile(host, manifest, null, [], options);
      updates.push(performance.now() - start); bytes.push(parsedBytes);
    }
    updates.sort((a, b) => a - b); bytes.sort((a, b) => a - b);
    rows.push({ contexts: count, first_html_ms: +firstHtmlMs.toFixed(2), warm_html_median_ms: +samples[3].toFixed(2), test_dom_update_median_ms: +updates[3].toFixed(2), update_parsed_bytes: bytes[3] });
  }
  return { label, rows };
}

const results = [];
if (process.argv.includes("--compare-head")) {
  const baseline = loadGraph(true);
  results.push(measure("HEAD", baseline.graph, baseline.setDocument));
}
const current = loadGraph();
results.push(measure("working tree", current.graph, current.setDocument));
console.log(JSON.stringify({ node: process.version, iterations: 7, max_predecessors: 4, notes: "Both versions run in separate VM contexts. Function timings and minimal test DOM only; excludes browser layout/paint and is not FPS.", results }, null, 2));
