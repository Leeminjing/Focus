/* DOCX run 错误呈现：具体错误优先、兜底文案保留、HTML 必须转义。 */
"use strict";

require("./viewer.js");
const assert = require("node:assert");
const {
  needsActionMessage,
  needsActionMarkup,
} = globalThis.FocusSpatialViewer;

const locked = {
  status: "needs_action",
  run_error: "DOCX 正被其他程序占用，请关闭 WPS/Word <script>alert(1)</script> 后重试",
};

assert.match(needsActionMessage(locked), /关闭 WPS\/Word/);
assert.match(needsActionMessage(locked), /可补充指令后重试/);
assert.match(needsActionMarkup(locked), /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
assert.doesNotMatch(needsActionMarkup(locked), /<script>/);
assert.strictEqual(
  needsActionMessage({ status: "needs_action" }),
  "未产生文件变更，可补充指令后重试。",
);
assert.strictEqual(needsActionMarkup({ status: "done", run_error: "不应显示" }), "");

console.log("docx-run-error: 错误优先、兜底与 HTML 转义通过");
