/* 输入记录必须保留具体文件名、图像缩略图与不可变版本，而非只有附件数量。 */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const view = require("./workspace-patrol-view.js");
const row = { submission_id: "sent", intent_id: "intent", content: "看看这个报错", status: "accepted", input_type: "information",
  attachments: [{ material_id: "image", relative_path: ".focus/attachments/error<&>.png", material_kind: "image", digest: "a".repeat(64) },
    { material_id: "doc", relative_path: ".focus/attachments/notes.txt", material_kind: "text", digest: "b".repeat(64) }],
  file_refs: [{ fact_id: "fact", revision: 3, path: "src/main.js", result_key: "result", sha256: "c".repeat(64) }],
};
test("browser input history identifies all sent files and exact versions", () => {
  const html = view.history({ rows: [row] });
  for (const label of ["error&lt;&amp;&gt;.png", "notes.txt", "main.js", "aaaaaaaa", "bbbbbbbb", "cccccccc", "版本 3"]) assert.ok(html.includes(label), label);
  assert.match(html, /<img[^>]+alt="error&lt;&amp;&gt;.png"/);
  assert.match(html, /data-patrol-history-attachment="0"/);
  assert.doesNotMatch(html, /src=".*session|error<&>/);
});
function setupPreview(read) {
  const vm = require("node:vm"), fs = require("node:fs");
  const { createElement } = require("./test-dom.cjs");
  const host = createElement("div"); host.rootMarker = true;
  host.innerHTML = view.history({ rows: [row] });
  const handlers = {}, revoked = [], urls = [];
  host.addEventListener = (type, handler) => { handlers[type] = handler; };
  let workspace = "A";
  const context = vm.createContext({ document: { createElement }, AbortController, Blob,
    URL: { createObjectURL(blob) { const url = `blob:${urls.length}`; urls.push(blob); return url; }, revokeObjectURL(url) { revoked.push(url); } },
  });
  vm.runInContext(fs.readFileSync(require.resolve("./patrol-input-references.js"), "utf8"), context);
  const preview = context.FocusPatrolInputReferences.create({ host, intake: { attachmentContent: read }, workspaceId: () => workspace, onError: error => { throw error; } });
  return { host, handlers, revoked, urls, preview, switchWorkspace() { workspace = "B"; host.replaceChildren(); preview.bind({ rows: [] }); } };
}
const flush = () => new Promise(resolve => setImmediate(resolve));

test("thumbnail and expanded image share the frozen read and release URLs on workspace switch", async () => {
  const calls = [];
  const h = setupPreview(async (...args) => { calls.push(args); return new Blob(["image"], { type: "image/png" }); });
  h.preview.bind({ rows: [row] });
  await flush();
  const node = h.host.querySelector("[data-patrol-history-attachment]");
  assert.equal(node.querySelector("summary img").src, "blob:0");
  node.open = true;
  await h.handlers.toggle({ target: node });
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "A");
  assert.equal(calls[0][1].intent_id, "intent");
  assert.equal(node.querySelector("[data-patrol-history-preview] img").src, "blob:0");
  assert.equal(node.querySelector("a").download, "error<&>.png");
  h.switchWorkspace();
  assert.deepEqual(h.revoked, ["blob:0"]);
  assert.equal(calls[0][3].aborted, true);
  h.preview.dispose();
});

test("late preview cannot bind bytes or retain URLs after changing workspace", async () => {
  let resolve, signal;
  const h = setupPreview((_id, _row, _item, pendingSignal) => { signal = pendingSignal; return new Promise(done => { resolve = done; }); });
  h.preview.bind({ rows: [row] });
  const oldImage = h.host.querySelector("summary img");
  h.switchWorkspace();
  resolve(new Blob(["old image"]));
  await flush();
  assert.equal(signal.aborted, true);
  assert.equal(oldImage.src, undefined);
  assert.equal(h.urls.length, 0);
  h.preview.dispose();
});

test("refreshing the same history version does not strand an in-flight preview", async () => {
  let resolve;
  const h = setupPreview(() => new Promise(done => { resolve = done; }));
  h.preview.bind({ rows: [row] });
  const node = h.host.querySelector("[data-patrol-history-attachment]");
  node.open = true;
  const opened = h.handlers.toggle({ target: node });
  h.preview.bind({ rows: [JSON.parse(JSON.stringify(row))] });
  resolve(new Blob(["same version"], { type: "image/png" }));
  await opened;
  assert.equal(node.querySelector("[data-patrol-history-preview] img").src, "blob:0");
  h.preview.dispose();
});
