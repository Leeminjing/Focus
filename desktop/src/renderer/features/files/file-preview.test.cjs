"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const preview = require("./file-preview.js");
const { createElement } = require("../../../../tests/helpers/test-dom.cjs");

test("search keeps source nodes and selection instead of rebuilding the file", t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const body = createElement("div");
  preview.renderText(body, "const value = 1;\nreturn value;", "script.js", { range: { start_line: 2, end_line: 2 } });
  const line = body.querySelector('[data-line="2"]');
  const search = body.querySelector("[data-file-search]");
  search.value = "return";
  search.dispatchEvent({ type: "input" });
  t.mock.timers.tick(200);
  assert.equal(body.querySelector('[data-line="2"]') === line, true, "search must retain existing line nodes");
  assert.equal(line.classList.contains("is-match"), true);
  assert.equal(line.classList.contains("is-referenced"), true);
});

test("large source previews mount only a bounded window of lines", () => {
  const body = createElement("div");
  preview.renderText(body, Array.from({ length: 10000 }, (_, i) => `const value${i} = ${i};`).join("\n"), "large.js");
  assert.ok(body.querySelectorAll("[data-line]").length <= 200, "source DOM must stay bounded as files grow");
});

test("rapid typing is debounced and next flushes the latest query across windows", t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const body = createElement("div");
  const lines = Array.from({ length: 10000 }, (_, i) => i === 8999 ? "RETURN needle" : `line ${i + 1}`);
  preview.renderText(body, lines.join("\n"), "large.js");
  const search = body.querySelector("[data-file-search]");
  const count = body.querySelector("[data-search-count]");
  search.value = "line"; search.dispatchEvent({ type: "input" });
  t.mock.timers.tick(100);
  search.value = "needle"; search.dispatchEvent({ type: "input" });
  t.mock.timers.tick(100);
  assert.equal(count.textContent, "");
  body.querySelector("[data-search-next]").dispatchEvent({ type: "click" });
  assert.match(count.textContent, /1\/1/);
  assert.equal(body.querySelector(".is-current").dataset.line, "9000");
  assert.equal(body.querySelector(".is-match").dataset.line, "9000");
  assert.ok(body.querySelectorAll("[data-line]").length <= 200);
  search.value = ""; search.dispatchEvent({ type: "input" });
  t.mock.timers.tick(150);
  assert.equal(body.querySelector(".is-match"), null);
  assert.equal(body.querySelector(".is-current"), null);
  assert.equal(count.textContent, "");
});

test("changing presentation cancels a pending search", t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const body = createElement("div");
  preview.renderText(body, "name,value\nhello,world", "table.csv");
  const search = body.querySelector("[data-file-search]");
  search.value = "hello"; search.dispatchEvent({ type: "input" });
  body.querySelector("[data-source-toggle]").dispatchEvent({ type: "click" });
  body.querySelector("[data-source-toggle]").dispatchEvent({ type: "click" });
  t.mock.timers.tick(200);
  assert.ok(body.querySelector(".file-csv"));
  assert.equal(body.querySelector(".file-source"), null);
  assert.equal(search.value, "");
});

test("CSV preserves quoted newlines, escaped quotes, empty cells and BOM", () => {
  assert.deepEqual(preview.parseCsv('\uFEFFname,description,empty\r\n"a,b","line 1\nline ""2""",\r\n'), [
    ["name", "description", "empty"], ["a,b", 'line 1\nline "2"', ""],
  ]);
  assert.deepEqual(preview.parseCsv("a\tb\n1\t2", "\t"), [["a", "b"], ["1", "2"]]);
});

test("source preview escapes executable markup and preserves exact text for copy", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const host = createElement("div");
  const text = 'const html = "<img src=x onerror=alert(1)>";\nreturn 42;';
  await preview.mount(host, { name: "script.js", target: { task_id: "t", content_ref: "script.js" }, text });
  assert.equal(host.querySelectorAll("img").length, 0);
  assert.equal(host.querySelectorAll(".file-source-line").length, 2);
  assert.match(host.querySelector(".file-source").innerHTML, /&lt;img src=x/);
  assert.equal(host.querySelectorAll(".syntax-keyword").length, 2);
  const search = host.querySelector("[data-file-search]");
  search.value = "return"; search.dispatchEvent({ type: "input" });
  t.mock.timers.tick(150);
  assert.equal(host.querySelectorAll(".is-match").length, 1);
});

test("unsupported office files retain every file exit without attempting a text fetch", async () => {
  const host = createElement("div");
  await preview.mount(host, { name: "deck.pptx", target: { task_id: "t", content_ref: "deck.pptx" } });
  assert.match(host.textContent, /此格式暂无内置预览/);
  assert.equal(host.querySelectorAll("[data-file-operation]").length, 3);
});

test("historical file operations keep the fixed identity and label current file separately", () => {
  const html = preview.toolbar({ loop_id: "loop", fact_id: "fact", revision: 2 }, "report.pdf");
  assert.match(html, /用系统应用打开此版本/);
  assert.match(html, /打开当前文件/);
  assert.match(html, /此版本另存为/);
});

const tick = () => new Promise(resolve => setImmediate(resolve));

async function mountPdf(t) {
  const requests = [];
  t.mock.method(globalThis, "fetch", url => {
    const deferred = Promise.withResolvers();
    const page = Number(new URL(url, "http://localhost").searchParams.get("page"));
    requests.push({
      page,
      complete: () => deferred.resolve({ ok: true, json: async () => ({ image: `page-${page}`, page_count: 3 }) }),
      fail: () => deferred.reject(new Error("PDF request failed")),
    });
    return deferred.promise;
  });
  const host = createElement("div");
  const mounting = preview.mount(host, { name: "report.pdf", target: { task_id: "t", content_ref: "report.pdf" } });
  requests[0].complete();
  await mounting;
  const previous = host.querySelector("[data-prev-page]"), next = host.querySelector("[data-next-page]");
  next.dispatchEvent({ type: "click" });
  requests[1].complete();
  await tick();
  assert.equal(host.querySelector("[data-page-label]").textContent, "2/3");
  return { host, requests, previous, next };
}

for (const staleResult of ["complete", "fail"]) {
  for (const staleFirst of [true, false]) {
    test(`PDF ignores stale ${staleResult} ${staleFirst ? "before" : "after"} the latest page completes`, async t => {
      const { host, requests, previous, next } = await mountPdf(t);
      next.dispatchEvent({ type: "click" });
      previous.dispatchEvent({ type: "click" });
      assert.deepEqual(requests.map(request => request.page), [1, 2, 3, 1]);

      if (staleFirst) {
        requests[2][staleResult]();
        await tick();
        assert.equal(host.querySelector("img").src, "data:image/png;base64,page-2");
        assert.equal(host.querySelector("[data-page-label]").textContent, "2/3");
        assert.equal(previous.disabled, false);
        assert.equal(next.disabled, false);
        assert.equal(host.querySelector("[data-file-status]").textContent, "");
      }

      requests[3].complete();
      await tick();
      if (!staleFirst) {
        requests[2][staleResult]();
        await tick();
      }
      assert.equal(host.querySelector("img").src, "data:image/png;base64,page-1");
      assert.equal(host.querySelector("[data-page-label]").textContent, "1/3");
      assert.equal(previous.disabled, true);
      assert.equal(next.disabled, false);
      assert.equal(host.querySelector("[data-file-status]").textContent, "");
    });
  }
}

test("PDF shows failures from the latest page request without changing the displayed page", async t => {
  const { host, requests, previous, next } = await mountPdf(t);
  next.dispatchEvent({ type: "click" });
  requests[2].fail();
  await tick();
  assert.equal(host.querySelector("[data-file-status]").textContent, "PDF request failed");
  assert.equal(host.querySelector("img").src, "data:image/png;base64,page-2");
  assert.equal(host.querySelector("[data-page-label]").textContent, "2/3");
  assert.equal(previous.disabled, false);
  assert.equal(next.disabled, false);
});
