/* DOCX 操作模式测试:无默认权限,显式模式映射为唯一请求权限。 */
"use strict";

require("./viewer.js");
const assert = require("node:assert");
const {
  permissionsForMaterial,
  requirePermissionsForMaterial,
} = globalThis.FocusSpatialViewer;

assert.strictEqual(permissionsForMaterial("report.docx", null), null);
assert.deepStrictEqual(permissionsForMaterial("report.docx", "read"), ["read"]);
assert.deepStrictEqual(permissionsForMaterial("report.docx", "write"), ["read", "write"]);
assert.deepStrictEqual(permissionsForMaterial("legacy.doc", null), ["read"]);
assert.deepStrictEqual(permissionsForMaterial("image.png", null), ["read"]);
assert.throws(
  () => requirePermissionsForMaterial("report.docx", null),
  /只读观察.*修改文档/,
);

console.log("docx-run-mode: 显式操作模式约束通过");
