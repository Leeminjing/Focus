/* 图谱视野回归：真实 bind/reconcile 路径，覆盖自动适配、手动视野与卸载清理。
 * 运行：node --test desktop/src/renderer/features/map/portfolio-map-viewport.test.cjs。
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createDocument, createElement } = require("../../../../tests/helpers/test-dom.cjs");

function fixture() {
  const document = createDocument(), frames = new Map(), observers = [];
  let frameId = 0;
  const context = vm.createContext({ document, AbortController,
    FocusPortfolioMapLayout: require("./portfolio-map-layout.js"),
    requestAnimationFrame(callback) { frames.set(++frameId, callback); return frameId; },
    cancelAnimationFrame(id) { frames.delete(id); },
    ResizeObserver: class {
      constructor(callback) { this.callback = callback; observers.push(this); }
      observe(node) { this.node = node; }
      disconnect() { this.disconnected = true; }
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "portfolio-map-view.js"), "utf8"), context);
  const graph = context.FocusPortfolioMapView;
  const host = createElement("div"); host.rootMarker = true;
  const manifest = { nodes: [{ context_id: "one", title: "One" }], edges: [] };
  const options = { presentation: "workbench" };
  host.innerHTML = graph.render(manifest, null, [], options);
  graph.reconcile(host, manifest, null, [], options);
  const map = host.querySelector(".portfolio-map"), scroll = host.querySelector(".portfolio-map-scroll");
  const canvas = host.querySelector(".portfolio-map-canvas");
  // The minimal DOM has no CSSStyleDeclaration; retain geometry writes for zoom calculations.
  const svg = canvas.querySelector("svg");
  Object.defineProperty(canvas, "style", { value: { width: `${svg.getAttribute("width")}px`, height: `${svg.getAttribute("height")}px` } });
  scroll.clientWidth = 800; scroll.clientHeight = 500;
  scroll.scrollLeft = 0; scroll.scrollTop = 0;
  scroll.setPointerCapture = () => {};
  const lifetime = new AbortController();
  const flush = () => { const pending = [...frames.values()]; frames.clear(); pending.forEach(fn => fn()); };
  const resize = (width, height) => { scroll.clientWidth = width; scroll.clientHeight = height; observers.filter(o => !o.disconnected).forEach(o => o.callback([])); };
  const click = action => host.querySelector(`[data-portfolio-zoom="${action}"]`).dispatchEvent({ type: "click", stopPropagation() {} });
  const zoom = () => Number(map.dataset.zoom || 1);
  const expected = () => Math.min(2, scroll.clientWidth / parseFloat(canvas.style.width), scroll.clientHeight / parseFloat(canvas.style.height));
  const patch = () => graph.reconcile(host, manifest, null, [], options);
  graph.bind(host, { autoFit: true, signal: lifetime.signal });
  return { host, graph, map, canvas, scroll, manifest, lifetime, observers, frames, flush, resize, click, zoom, expected, patch };
}

test("auto mode fits initial, resized and relaid-out graphs, including below 72%", () => {
  const f = fixture(); f.flush();
  assert.equal(f.zoom(), f.expected());
  f.scroll.scrollLeft = 90; f.scroll.scrollTop = 40;
  f.resize(360, 240); f.resize(320, 220);
  assert.equal(f.frames.size, 1, "resize notifications share one frame");
  f.flush();
  assert.equal(f.zoom(), f.expected());
  assert.equal(f.scroll.scrollLeft, 0); assert.equal(f.scroll.scrollTop, 0);
  f.manifest.nodes.push(...Array.from({ length: 18 }, (_, i) => ({ context_id: `added-${i}` })));
  f.patch(); f.flush();
  assert.equal(f.zoom(), f.expected());
  assert.ok(f.zoom() < 0.72);
  f.scroll.scrollTop = 12;
  f.manifest.nodes[0].title = "Updated";
  f.patch(); f.flush();
  assert.equal(f.scroll.scrollTop, 12, "metadata updates do not reset the viewport");
  f.lifetime.abort();
});

test("manual zoom survives resize and topology updates; Fit restores auto mode", () => {
  const f = fixture(); f.flush(); f.click("in");
  const zoom = f.zoom();
  f.scroll.scrollLeft = 70; f.scroll.scrollTop = 30;
  f.resize(300, 200);
  f.manifest.nodes.push({ context_id: "two" }); f.patch(); f.flush();
  assert.equal(f.zoom(), zoom);
  assert.equal(f.scroll.scrollLeft, 70); assert.equal(f.scroll.scrollTop, 30);
  f.click("fit");
  assert.equal(f.zoom(), f.expected());
  f.resize(280, 180); f.flush();
  assert.equal(f.zoom(), f.expected());
  f.lifetime.abort();
});

for (const interaction of ["wheel", "keyboard", "drag"]) {
  test(`${interaction} switches to manual mode and wins over a queued automatic fit`, () => {
    const f = fixture(); f.flush(); const zoom = f.zoom();
    f.resize(300, 200);
    if (interaction === "wheel") f.scroll.dispatchEvent({ type: "wheel" });
    if (interaction === "keyboard") f.scroll.dispatchEvent({ type: "keydown", key: "ArrowRight" });
    if (interaction === "drag") {
      f.scroll.dispatchEvent({ type: "pointerdown", button: 0, pointerId: 1, clientX: 50, clientY: 50 });
      f.scroll.dispatchEvent({ type: "pointermove", clientX: 20, clientY: 20 });
      f.scroll.dispatchEvent({ type: "pointerup" });
    }
    f.flush(); assert.equal(f.zoom(), zoom);
    f.lifetime.abort();
  });
}

test("hidden graphs defer fitting until visible; abort disconnects observers and cancels frames", () => {
  const f = fixture(); f.flush(); const zoom = f.zoom();
  f.resize(0, 0); f.flush(); assert.equal(f.zoom(), zoom);
  f.resize(300, 200); f.flush(); assert.equal(f.zoom(), f.expected());
  f.resize(250, 180); f.lifetime.abort();
  assert.equal(f.frames.size, 0);
  assert.ok(f.observers.every(observer => observer.disconnected));
  f.flush(); assert.equal(f.zoom(), Math.min(300 / 900, 200 / 620));
});

test("replacing a graph releases the old observer and pending fit", () => {
  const f = fixture();
  f.host.innerHTML = "";
  f.graph.bind(f.host, { autoFit: true, signal: f.lifetime.signal });
  assert.equal(f.frames.size, 0);
  assert.ok(f.observers.every(observer => observer.disconnected));
  f.lifetime.abort();
});
