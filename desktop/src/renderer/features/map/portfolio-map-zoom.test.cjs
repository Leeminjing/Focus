/* 通过真实缩放事件验证 Fit 完整容纳图谱，以及手动缩放的上下限。
 * 示例：node --test desktop/src/renderer/features/map/portfolio-map-zoom.test.cjs。
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const { createElement } = require("../../../../tests/helpers/test-dom.cjs");
const graph = require("./portfolio-map-view.js");

function mount(presentation = "workbench", count = 32) {
  const nodes = Array.from({ length: count }, (_, i) => ({ context_id: `n-${i}`, title: `Context ${i}` }));
  const host = createElement("div");
  host.innerHTML = graph.render({ nodes, edges: [] }, null, [], { presentation });
  const map = host.querySelector(".portfolio-map");
  const scroll = map.querySelector(".portfolio-map-scroll");
  const canvas = map.querySelector(".portfolio-map-canvas");
  // The shared DOM substitute has no CSS layout or persistent style properties.
  const style = Object.fromEntries([...canvas.getAttribute("style").matchAll(/(width|height):([\d.]+px)/g)].map(([, key, value]) => [key, value]));
  Object.defineProperty(canvas, "style", { value: style });
  scroll.clientWidth = 1000;
  scroll.clientHeight = 600;
  graph.bind(host);
  const click = action => {
    map.querySelector(`[data-portfolio-zoom="${action}"]`).dispatchEvent({ type: "click", stopPropagation() {} });
    return Number(map.dataset.zoom);
  };
  return { map, scroll, canvas, click };
}

for (const presentation of ["workbench", "default"]) {
  test(`Fit contains the entire 32-node ${presentation} graph at any container size`, () => {
    const { map, scroll, canvas, click } = mount(presentation);
    for (const [width, height] of [[1000, 600], [320, 240], [800, 300], [1600, 900]]) {
      scroll.clientWidth = width;
      scroll.clientHeight = height;
      scroll.scrollLeft = 120;
      scroll.scrollTop = 80;
      const zoom = click("fit");
      assert.ok(parseFloat(canvas.style.width) * zoom <= width + 0.001, "graph exceeds container width");
      assert.ok(parseFloat(canvas.style.height) * zoom <= height + 0.001, "graph exceeds container height");
      assert.equal(canvas.style.zoom, zoom);
      assert.equal(map.querySelector("[data-portfolio-scale]").textContent, `${Math.round(zoom * 100)}%`);
      assert.equal(scroll.scrollLeft, 0);
      assert.equal(scroll.scrollTop, 0);
    }
  });

  test(`manual ${presentation} zoom retains its limits and never reverses direction after Fit`, () => {
    const { click } = mount(presentation);
    const minimum = presentation === "workbench" ? 0.72 : 0.25;
    for (let i = 0; i < 20; i++) click("out");
    assert.equal(click("out"), minimum);
    for (let i = 0; i < 20; i++) click("in");
    assert.equal(click("in"), 2);
    const fitted = click("fit");
    assert.ok(fitted < minimum);
    assert.equal(click("out"), fitted, "zoom-out must not enlarge a fitted graph");
    assert.ok(click("in") >= minimum);
  });

  test(`Fit ignores hidden ${presentation} containers and caps enlargement at 200%`, () => {
    const { scroll, click } = mount(presentation, 1);
    assert.equal(click("in"), 1.15);
    scroll.clientWidth = 0;
    assert.equal(click("fit"), 1.15);
    scroll.clientWidth = scroll.clientHeight = 10000;
    assert.equal(click("fit"), 2);
  });
}
