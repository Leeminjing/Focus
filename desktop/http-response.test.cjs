/*
 * 本文件对外提供共享桌面 HTTP 响应解码器的无 DOM 回归测试。
 * 输入为 JSON、文本、HTML、空正文、畸形 JSON 与超长 Response；输出为成功值或保留因果元数据且不含凭据的 DesktopApiError 断言。
 * 具体工作流为使用原生 Response 单次消费正文，并检查状态、detail、code、operation、正文边界和安全序列化。
 * 示例：`node --test desktop/http-response.test.cjs`。
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const HttpResponse = require("./http-response.js");


test("decodes successful JSON and empty responses", async () => {
  assert.deepEqual(await HttpResponse.decodeResponse(new Response('{"ok":true}', { headers: { "Content-Type": "application/json" } })), { ok: true });
  assert.equal(await HttpResponse.decodeResponse(new Response(null, { status: 204 })), null);
});


test("preserves structured JSON error semantics", async () => {
  const response = new Response(JSON.stringify({ detail: { code: "curation_ownership_conflict", message: "Context 已被占用", owner_loop_id: "loop-1", lane_id: "lane-1" } }), {
    status: 409,
    headers: { "Content-Type": "application/json" },
  });
  await assert.rejects(HttpResponse.decodeResponse(response, { operation: "授权 Loop" }), error => {
    assert.ok(error instanceof HttpResponse.DesktopApiError);
    assert.equal(error.status, 409);
    assert.equal(error.code, "curation_ownership_conflict");
    assert.equal(error.detail.owner_loop_id, "loop-1");
    assert.match(error.message, /Context 已被占用/);
    return true;
  });
});


test("plain text and HTML failures remain causal HTTP errors", async () => {
  for (const [contentType, body] of [["text/plain", "Internal Server Error"], ["text/html", "<h1>Bad Gateway</h1>"]]) {
    await assert.rejects(HttpResponse.decodeResponse(new Response(body, { status: 500, headers: { "Content-Type": contentType } }), { operation: "启动 Loop" }), error => {
      assert.equal(error.status, 500);
      assert.equal(error.bodyExcerpt, body);
      assert.doesNotMatch(error.message, /Unexpected token/);
      return true;
    });
  }
});


test("malformed and empty JSON failures do not expose parser exceptions", async () => {
  for (const body of ['{"detail":', ""]) {
    await assert.rejects(HttpResponse.decodeResponse(new Response(body, { status: 500, headers: { "Content-Type": "application/json" } }), { operation: "启动 Loop" }), error => {
      assert.equal(error.status, 500);
      assert.doesNotMatch(error.message, /Unexpected token|JSON\.parse/);
      return true;
    });
  }
});


test("bounds raw bodies and never captures request credentials", async () => {
  const secret = "desktop-session-secret";
  const body = "x".repeat(20000);
  await assert.rejects(HttpResponse.decodeResponse(new Response(body, { status: 502 }), { operation: "读取 Context", maxErrorBodyChars: 512 }), error => {
    assert.equal(error.bodyExcerpt.length, 512);
    assert.doesNotMatch(JSON.stringify(error.toJSON()), new RegExp(secret));
    assert.equal(Object.hasOwn(error, "headers"), false);
    return true;
  });
});
