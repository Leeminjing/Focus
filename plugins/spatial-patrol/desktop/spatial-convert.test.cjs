/* spatial-patrol 坐标换算不变式测试:缩放/平移后内容坐标保持稳定(node 直接执行)。 */
"use strict";

require("./viewer.js");
const convert = globalThis.FocusSpatialViewer.convertContent;
const assert = require("node:assert");

const rect = { left: 100, top: 50 };
const natural = { width: 1000, height: 800 };

// 基准:zoom=1、无平移,指针 (600, 450) → 内容 (0.5, 0.5)
assert.deepStrictEqual(convert(rect, 1, 0, 0, natural, 600, 450), { x: 0.5, y: 0.5 });

// 缩放不变式:zoom=4(400%) 时同一内容位置在屏幕上的新指针位置换算回同一坐标
// 屏幕位置 = rect.left + tx + contentX * zoom
const contentX = 0.5, contentY = 0.5;
const zoom = 4, tx = 0, ty = 0;
const clientX = rect.left + tx + contentX * natural.width * zoom;   // 2100
const clientY = rect.top + ty + contentY * natural.height * zoom;   // 1650
assert.deepStrictEqual(convert(rect, zoom, tx, ty, natural, clientX, clientY), { x: 0.5, y: 0.5 });

// 平移不变式:平移 (200, 100) 后,原内容点移到 (clientX+200, clientY+100),换算回同一坐标
const tx2 = 200, ty2 = 100;
assert.deepStrictEqual(
  convert(rect, zoom, tx2, ty2, natural, clientX + tx2, clientY + ty2),
  { x: 0.5, y: 0.5 },
);

// 内容边界:指针超出图像区域时钳制到 0..1
assert.deepStrictEqual(convert(rect, 1, 0, 0, natural, rect.left - 500, rect.top - 500), { x: 0, y: 0 });
assert.deepStrictEqual(convert(rect, 1, 0, 0, natural, rect.left + 5000, rect.top + 5000), { x: 1, y: 1 });

// 非法输入返回 null
assert.strictEqual(convert(rect, 1, 0, 0, null, 600, 450), null);
assert.strictEqual(convert(null, 1, 0, 0, natural, 600, 450), null);

console.log("spatial-convert: 全部坐标不变式通过");
