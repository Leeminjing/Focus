/* DOCX 文本坐标：DOM caret 与全文 Unicode 字符偏移双向转换。 */
"use strict";

require("./viewer.js");
const assert = require("node:assert");
const {
  TEXT_COORDINATE_SPACE,
  domPointFromTextOffset,
  isLegacyTextAnchor,
  textMarkerPosition,
  textOffsetFromDomPoint,
  textPointToContent,
} = globalThis.FocusSpatialViewer;

function fixture(values) {
  const nodes = values.map(nodeValue => ({ nodeType: 3, nodeValue }));
  const layout = { columns: 20, viewportShift: 0 };
  const contentRect = () => ({ left: 100, top: 50 + layout.viewportShift });
  const doc = {
    defaultView: { NodeFilter: { SHOW_TEXT: 4 } },
    createTreeWalker() {
      let index = -1;
      return { nextNode: () => nodes[++index] || null };
    },
    caretPositionFromPoint() { return doc.caret; },
    createRange() {
      let selectedNode;
      let selectedOffset;
      return {
        setStart(node, offset) { selectedNode = node; selectedOffset = offset; },
        collapse() {},
        getBoundingClientRect() {
          const nodeIndex = nodes.indexOf(selectedNode);
          const before = nodes.slice(0, nodeIndex)
            .reduce((sum, node) => sum + Array.from(node.nodeValue).length, 0);
          const local = Array.from(selectedNode.nodeValue.slice(0, selectedOffset)).length;
          const absolute = before + local;
          const base = contentRect();
          return {
            left: base.left + (absolute % layout.columns) * 10,
            top: base.top + Math.floor(absolute / layout.columns) * 20,
            height: 20,
          };
        },
      };
    },
  };
  const root = {
    ownerDocument: doc,
    textContent: values.join(""),
    contains: node => nodes.includes(node),
  };
  const content = {
    scrollWidth: 400,
    getBoundingClientRect: contentRect,
  };
  return { nodes, layout, doc, root, content };
}

const view = fixture(["标题\n", "水印🙂段落\n", "摘要"]);
const targetUtf16Offset = "水印🙂".length;
const absoluteOffset = textOffsetFromDomPoint(view.root, view.nodes[1], targetUtf16Offset);
assert.strictEqual(absoluteOffset, 6, "代理对必须按一个 Unicode 字符累计");

view.doc.caret = { offsetNode: view.nodes[1], offset: targetUtf16Offset };
const contentPoint = textPointToContent(view.root, view.content, 220, 80, view.doc);
const totalCharacters = Array.from(view.root.textContent).length;
assert.deepStrictEqual(contentPoint, {
  page: 1,
  x: 0.3,
  y: absoluteOffset / totalCharacters,
  coordinate_space: TEXT_COORDINATE_SPACE,
});

const restored = domPointFromTextOffset(view.root, contentPoint.y);
assert.strictEqual(restored.node, view.nodes[1]);
assert.strictEqual(restored.offset, targetUtf16Offset);

const widePosition = textMarkerPosition(view.root, view.content, contentPoint.y, view.doc);
view.layout.columns = 4;
const narrowPosition = textMarkerPosition(view.root, view.content, contentPoint.y, view.doc);
assert.notDeepStrictEqual(narrowPosition, widePosition, "重新换行后应重新投影渲染位置");
assert.strictEqual(domPointFromTextOffset(view.root, contentPoint.y).node, view.nodes[1]);
assert.strictEqual(domPointFromTextOffset(view.root, contentPoint.y).offset, targetUtf16Offset);

const beforeScroll = textMarkerPosition(view.root, view.content, contentPoint.y, view.doc);
view.layout.viewportShift = -120;
assert.deepStrictEqual(
  textMarkerPosition(view.root, view.content, contentPoint.y, view.doc),
  beforeScroll,
  "滚动同时移动文字与内容容器，不得改变内容内坐标",
);

assert.strictEqual(isLegacyTextAnchor({ region: null }), true);
assert.strictEqual(isLegacyTextAnchor({ region: { coordinate_space: TEXT_COORDINATE_SPACE } }), false);

const fallback = fixture(["甲\n", "乙"]);
delete fallback.doc.caretPositionFromPoint;
fallback.doc.caretRangeFromPoint = () => ({ startContainer: fallback.nodes[1], startOffset: 1 });
assert.strictEqual(
  textPointToContent(fallback.root, fallback.content, 100, 50, fallback.doc).y,
  3 / 3,
  "旧 Chromium 回退也必须返回字符坐标",
);

console.log("docx-text-coordinate: 字符坐标、反向投影、重排/滚动与旧锚点保护通过");
