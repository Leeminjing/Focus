/* 图谱计算回归：用真实视图和最小 DOM 检查 Run 更新只解析变化片段。
 * 示例：node --test desktop/src/renderer/features/map/portfolio-map-incremental.test.cjs。
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const { createDocument, createElement } = require("../../../../tests/helpers/test-dom.cjs");
const graph = require("./portfolio-map-view.js");

// The minimal parser stores bare boolean attributes as null; browsers expose "".
function normalizeBooleans(root) {
  for (const element of root.querySelectorAll("*")) {
    for (const { name, value } of element.attributes) if (value === null) element.setAttribute(name, "");
  }
}

function testDocument() {
  const document = createDocument(), create = document.createElement;
  document.createElement = tag => {
    const element = create(tag);
    if (tag === "template") {
      const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), "innerHTML");
      Object.defineProperty(element, "innerHTML", { get() { return descriptor.get.call(this); }, set(html) { descriptor.set.call(this, html); normalizeBooleans(this.content); } });
    }
    return element;
  };
  return document;
}

function fixture(count = 3) {
  const nodes = Array.from({ length: count }, (_, i) => ({ context_id: `n-${i}`, title: `Context ${i}`, current_revision_id: `r-${i}`, latest_run: { run_id: `run-${i}`, status: "running" } }));
  const edges = nodes.flatMap((node, i) => nodes.slice(Math.max(0, i - 4), i).map(source => ({ source_context_id: source.context_id, source_revision_id: source.current_revision_id, target_context_id: node.context_id, target_revision_id: node.current_revision_id })));
  return { nodes, edges };
}

test("warm Run updates parse only changed cards and incident edges; identical frames parse nothing", t => {
  const parsed = [];
  const document = createDocument();
  const create = document.createElement;
  document.createElement = tag => {
    const element = create(tag);
    if (tag === "template") {
      const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), "innerHTML");
      Object.defineProperty(element, "innerHTML", { get() { return descriptor.get.call(this); }, set(html) { parsed.push(html); descriptor.set.call(this, html); } });
    }
    return element;
  };
  const previous = global.document;
  global.document = document;
  t.after(() => { global.document = previous; });
  const manifest = fixture();
  const options = { presentation: "workbench" };
  const host = createElement("div");
  host.innerHTML = graph.render(manifest, null, [], options);
  graph.reconcile(host, manifest, null, [], options);
  const unchanged = host.querySelector('[data-context-id="n-2"]');
  const preview = unchanged.querySelector("[data-context-preview]");
  const untouchedEdge = host.querySelectorAll("[data-edge-id]")[2];
  parsed.length = 0;
  manifest.nodes[0].latest_run.status = "success";
  assert.equal(graph.reconcile(host, manifest, null, [], options), true);
  assert.doesNotMatch(parsed.join(""), /Context 2|portfolio-map-toolbar|<section/);
  assert.equal(host.querySelector('[data-context-id="n-2"]'), unchanged);
  assert.equal(unchanged.querySelector("[data-context-preview]"), preview);
  assert.equal(host.querySelectorAll("[data-edge-id]")[2], untouchedEdge);
  assert.equal(parsed.length, 3, "one card and its two incident edges");
  parsed.length = 0;
  graph.reconcile(host, manifest, null, [], options);
  assert.deepEqual(parsed, []);
});

test("incremental output matches fresh rendering across metadata, selection, exact revisions and topology changes", t => {
  const previousDocument = global.document, previousLayout = global.FocusPortfolioMapLayout;
  global.document = testDocument();
  let layouts = 0;
  const geometry = require("./portfolio-map-layout.js");
  global.FocusPortfolioMapLayout = { ...geometry, layout(...args) { layouts++; return geometry.layout(...args); } };
  t.after(() => { global.document = previousDocument; global.FocusPortfolioMapLayout = previousLayout; });
  const manifest = fixture(12), options = { presentation: "workbench" };
  let selected = null, activity = [];
  const host = createElement("div");
  host.innerHTML = graph.render(manifest, selected, activity, options);
  normalizeBooleans(host);
  graph.reconcile(host, manifest, selected, activity, options);
  const scroll = host.querySelector(".portfolio-map-scroll");
  scroll.scrollLeft = 135; scroll.scrollTop = 80;
  const summary = root => [...root.querySelectorAll("[data-context-id], [data-edge-id], [data-directive-id]")].map(element => ({
    attributes: Object.fromEntries([...element.attributes].map(({ name, value }) => [name, value]).sort(([a], [b]) => a.localeCompare(b))),
    html: element.innerHTML,
  }));
  function check(change, expectLayouts = 0) {
    change();
    layouts = 0;
    assert.equal(graph.reconcile(host, manifest, selected, activity, options), true);
    assert.equal(layouts, expectLayouts);
    const fresh = createElement("div");
    fresh.innerHTML = graph.render(manifest, selected, activity, options);
    normalizeBooleans(fresh);
    assert.deepEqual(summary(host), summary(fresh));
    assert.equal(host.querySelector(".portfolio-map-scroll"), scroll);
    assert.equal(scroll.scrollLeft, 135); assert.equal(scroll.scrollTop, 80);
  }
  check(() => { manifest.nodes[0].latest_run.status = "success"; });
  check(() => { manifest.nodes[1].title = "Updated title"; manifest.nodes[1].counts = { runs: 3 }; });
  check(() => { selected = "n-1"; options.relatedOnly = true; options.highlightIds = ["n-10"]; });
  check(() => { options.selectedRevisionId = "history"; manifest.revisions = [{ revision_id: "history", generation: 0 }]; });
  check(() => { options.selectedRevisionId = "r-1"; });
  check(() => { manifest.edges.push({ source_context_id: "n-1", source_revision_id: "history", target_context_id: "n-1", target_revision_id: "r-1" }); }, 1);
  check(() => { manifest.nodes.splice(5, 1); manifest.edges = manifest.edges.filter(edge => edge.source_context_id !== "n-5" && edge.target_context_id !== "n-5"); }, 1);
  check(() => { manifest.nodes.push({ context_id: "added", title: "Added", current_revision_id: "added-r" }); }, 1);
  check(() => { manifest.nodes.reverse(); manifest.edges.reverse(); }, 1);
  check(() => { options.presentation = "default"; activity = [{ id: "directive", target_context_id: "n-1", state: "authorized" }]; }, 1);
  check(() => { activity[0].state = "settled"; });
  check(() => { activity.push({ id: "second", target_context_id: "added", state: "delivering" }); });
  check(() => { activity.reverse(); });
  check(() => { activity = []; manifest.nodes[0].counts = { runs: 9, artifacts: 2 }; });
  check(() => { options.presentation = "workbench"; }, 1);
});

test("same Run metadata updates retain the live preview element and its content", t => {
  const previous = global.document;
  global.document = createDocument();
  t.after(() => { global.document = previous; });
  const manifest = fixture(), options = { presentation: "workbench" };
  const host = createElement("div");
  host.innerHTML = graph.render(manifest, null, [], options);
  graph.reconcile(host, manifest, null, [], options);
  graph.patchPreviews(host, { "n-0": { run_id: "run-0", role: "Assistant", status: "streaming", text: "最新输出" } });
  const preview = host.querySelector('[data-context-id="n-0"] [data-context-preview]');
  manifest.nodes[0].title = "A changed title";
  graph.reconcile(host, manifest, null, [], options);
  assert.equal(host.querySelector('[data-context-id="n-0"] [data-context-preview]'), preview);
  assert.equal(preview.querySelector(".context-preview-text").textContent, "最新输出");
  manifest.nodes[0].latest_run.run_id = "replacement";
  graph.reconcile(host, manifest, null, [], options);
  assert.notEqual(host.querySelector('[data-context-id="n-0"] [data-context-preview]'), preview);
  assert.equal(host.querySelector('[data-context-id="n-0"] .context-preview-text').textContent, "");
});

test("layout and featured ranking read edge endpoints a bounded number of times", () => {
  const manifest = fixture(64);
  let reads = 0;
  manifest.edges = manifest.edges.map(edge => new Proxy(edge, { get(target, key) {
    if (key === "source_context_id" || key === "target_context_id") reads++;
    return target[key];
  } }));
  graph.render(manifest, null, [], { presentation: "workbench" });
  assert.ok(reads < manifest.edges.length * 40, `${reads} endpoint reads for ${manifest.edges.length} edges`);
});

test("Context cards group Runs once and retain direct-context and Run activity precedence", () => {
  const selectors = require("../loop/loop-live-selectors.js");
  let reads = 0;
  const contexts = {}, runs = {};
  for (let i = 0; i < 64; i++) {
    const id = `c-${i}`;
    contexts[id] = { entity_id: id, state: { title: id } };
    runs[`run-${i}`] = { entity_id: `run-${i}`, updated_sequence: i, state: { get context_id() { reads++; return id; }, status: "running" } };
  }
  const timeline = [{ detail: { context_id: "c-0" }, title: "older" }, { detail: { run_id: "run-0", context_id: "c-1" }, title: "newest" }];
  const cards = selectors.selectContextCards({ contexts, runs, activity_timeline: timeline });
  assert.equal(cards[0].live_action, timeline[1]);
  assert.equal(cards[1].live_action, timeline[1]);
  assert.equal(cards[2].live_action, null);
  assert.ok(reads <= 64 * 3, `${reads} Context reads for 64 Runs`);
});
