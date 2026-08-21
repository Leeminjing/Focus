/* API 错误响应：JSON 与纯文本均只读取一次响应体。 */
"use strict";

require("./viewer.js");
const assert = require("node:assert");
const { responseErrorDetail } = globalThis.FocusSpatialViewer;

(async () => {
  const json = new Response(JSON.stringify({ detail: "坐标无效" }), {
    status: 422,
    headers: { "Content-Type": "application/json" },
  });
  assert.strictEqual(await responseErrorDetail(json), "坐标无效");

  const plain = new Response("Internal Server Error", { status: 500 });
  assert.strictEqual(await responseErrorDetail(plain), "Internal Server Error");

  console.log("response-error: JSON/纯文本错误响应单次读取通过");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
