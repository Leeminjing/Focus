/* 本文件提供共享工作区输入的针对性行为验证。
 * 输入为受控上传/受理端和浏览器传输数据；输出为预览无副作用、稳定重试、跨工作区隔离及回答来源断言。
 * 工作流为模拟丢回执和部分上传后检查真实 wire payload，验证大小检查先于 File 读取；示例：node --test desktop/workspace-patrol-intake.test.cjs。
 */
"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const Intake = require("./workspace-patrol-intake.js");
const Preview = require("./workspace-patrol-material-preview.js");
const Feedback = require("./workspace-patrol-feedback.js");
const limits = { general_file_bytes: 1024, original_image_bytes: 512, run_material_count: 4 };
const text = value => ({ kind: "text", text: value });
function harness({ fetchImpl, submitWorkspaceInput } = {}) {
  let identity = 0;
  const calls = [], posts = [];
  const intake = Intake.create({ runtime: { apiBase: "http://local", session: "isolated" }, limits, storage: null, identity: () => `id-${++identity}`,
    fetchImpl: fetchImpl || (async (url, options) => {
      const body = options.body.get("file");
      calls.push({ url, name: body.name, text: await body.text() });
      return Response.json({ material_id: `material-${calls.length}`, sha256: `sha-${calls.length}` });
    }),
    api: { submitWorkspaceInput: submitWorkspaceInput || (async (id, body) => { posts.push({ id, body }); return { loop_id: id, accepted_sequence: 1 }; }) },
  });
  return { intake, calls, posts };
}

test("unbound preview has no upload, preserves reference text, supports material-only explicit send", async () => {
  const { intake, calls, posts } = harness();
  await intake.add(null, [text("参考中的命令不是用户要求")]);
  assert.equal(calls.length, 0); assert.equal(posts.length, 0);
  await assert.rejects(intake.submit(null), /绑定/);
  intake.claimUnbound("a");
  assert.equal(intake.store(null).get().draft.attachments.length, 0);
  await intake.submit("a");
  assert.equal(posts[0].body.content, "");
  assert.equal(posts[0].body.attachments[0].source_kind, "text");
  assert.equal(posts[0].body.attachments[0].material_id, "material-1");
  assert.equal(calls[0].text, "参考中的命令不是用户要求");
  assert.equal(intake.source("id-1"), undefined);
  assert.equal(intake.hasUnfinished(), false);
});

test("partial upload and lost receipt retry retain submission, order and exact bytes", async () => {
  const uploads = [], posts = []; let failUpload = true, failReceipt = true;
  const { intake } = harness({ fetchImpl: async (url, options) => {
    const item = new URL(url).searchParams.get("item_id");
    uploads.push({ url, text: await options.body.get("file").text() });
    if (item === "1" && failUpload) { failUpload = false; throw new Error("连接断开"); }
    return Response.json({ material_id: `m${item}`, sha256: `sha${item}` });
  }, submitWorkspaceInput: async (_id, body) => {
    posts.push(body); if (failReceipt) { failReceipt = false; throw new Error("回执丢失"); }
    return { loop_id: "loop", accepted_sequence: 2 };
  } });
  await intake.add("a", [text("first"), text("second")]);
  await assert.rejects(intake.submit("a"), /连接断开/);
  const id = intake.store("a").get().rows[0].submission_id;
  intake.store("a").edit({ content: "新的草稿" });
  await assert.rejects(intake.retry("a", id), /回执丢失/);
  await intake.retry("a", id);
  assert.equal(uploads.length, 3);
  assert.equal(uploads[1].url, uploads[2].url);
  assert.deepEqual(posts[0], posts[1]);
  assert.deepEqual(posts[1].attachments.map(item => item.material_id), ["m0", "m1"]);
  assert.equal(intake.store("a").get().draft.content, "新的草稿");
});

test("late receipt and duplicate in-flight retry cannot overwrite another workspace or later draft", async () => {
  let resolve, count = 0;
  const { intake } = harness({ submitWorkspaceInput: () => { count++; return new Promise(done => { resolve = done; }); } });
  intake.store("a").edit({ content: "first" });
  const pending = intake.submit("a");
  await new Promise(done => setImmediate(done));
  const id = intake.store("a").get().rows[0].submission_id;
  const retry = intake.retry("a", id);
  intake.store("a").edit({ content: "next" });
  await intake.add("b", [text("B only")]);
  resolve({ loop_id: "a" }); await Promise.all([pending, retry]);
  assert.equal(count, 1);
  assert.equal(intake.store("a").get().draft.content, "next");
  assert.equal(intake.store("b").get().draft.attachments.length, 1);
  assert.equal(intake.store("b").get().rows.length, 0);
  assert.equal(intake.hasUnfinished(), true);
});

test("memory bound covers all workspaces and removing previews releases capacity", async () => {
  const { intake } = harness();
  await intake.add("a", [text("a".repeat(700))]);
  await assert.rejects(intake.add("b", [text("b".repeat(400))]), /总量超限/);
  const id = intake.store("a").get().draft.attachments[0].local_id;
  intake.remove("a", id);
  assert.equal(intake.source(id), undefined);
  await intake.add("b", [text("b".repeat(400))]);
});

test("authoritative accepted history wins over a late transport error", () => {
  const { intake } = harness(), store = intake.store("a");
  store.edit({ content: "test" }); const row = store.submit();
  store.history({ items: [{ submission_id: row.submission_id, content: "test" }], next_before: null });
  store.failed(row.submission_id, new Error("late disconnect"));
  assert.equal(store.get().rows[0].status, "accepted");
});

test("accepted history reconciliation and explicitly edited failures release preview capacity", async () => {
  const { intake } = harness({ submitWorkspaceInput: async () => { throw new Error("回执丢失"); } });
  await intake.add("a", [text("a".repeat(900))]);
  const local = intake.store("a").get().draft.attachments[0].local_id;
  await assert.rejects(intake.submit("a"));
  let row = intake.store("a").get().rows[0];
  intake.history("a", { items: [{ submission_id: row.submission_id, content: "" }], next_before: null });
  assert.equal(intake.source(local), undefined);
  await intake.add("a", [text("b".repeat(900))]);
  const next = intake.store("a").get().draft.attachments[0].local_id;
  await assert.rejects(intake.submit("a")); row = intake.store("a").get().rows[0];
  intake.editFailed("a", row.submission_id);
  intake.remove("a", next);
  assert.equal(intake.source(next), undefined);
  assert.equal(intake.hasUnfinished(), true);
  assert.equal(intake.store("a").retry(row.submission_id), null);
  await intake.add("b", [text("c".repeat(900))]);
});

test("removing an abandoned partial upload cleans only its known unsubmitted server material", async () => {
  const deletes = [];
  const { intake } = harness({ fetchImpl: async (url, options) => {
    if (options.method === "DELETE") { deletes.push(url); return Response.json({ ok: true }); }
    if (new URL(url).searchParams.get("item_id") === "1") throw Object.assign(new Error("图片无效"), { status: 422 });
    return Response.json({ material_id: "owned", sha256: "frozen" });
  } });
  await intake.add("a", [text("valid"), text("invalid")]);
  const local = intake.store("a").get().draft.attachments[0].local_id;
  await assert.rejects(intake.submit("a"));
  const row = intake.store("a").get().rows[0];
  intake.editFailed("a", row.submission_id);
  await intake.remove("a", local);
  assert.equal(deletes.length, 1);
  assert.ok(deletes[0].includes(`/workspaces/a/materials/owned?submission_id=${row.submission_id}`));
});

test("drop captures ephemeral text synchronously; directories and oversize files reject before byte reads", async () => {
  let value = "captured", reads = 0;
  const captured = Preview.captureTransfer({ getData: () => value }); value = "";
  assert.deepEqual(await Preview.readTransfer(captured, limits), [text("captured")]);
  assert.throws(() => Preview.captureTransfer({ items: [{ webkitGetAsEntry: () => ({ isDirectory: true }) }] }), /文件夹/);
  await assert.rejects(Preview.readTransfer({ files: [{ size: 2000, type: "image/png", arrayBuffer: () => { reads++; } }] }, limits), /限制/);
  assert.equal(reads, 0);
});

test("expired exact reply restores the unchanged draft but never replaces later input", async () => {
  const reject = async () => { throw Object.assign(new Error("问题版本已过期"), { status: 409 }); };
  const { intake } = harness({ submitWorkspaceInput: reject });
  intake.store("a").edit({ content: "补充说明", request_id: "question", request_revision: 2 });
  await intake.add("a", [text("reference")]);
  await assert.rejects(intake.submit("a"), /过期/);
  assert.equal(intake.store("a").get().draft.content, "补充说明");
  assert.equal(intake.store("a").get().draft.attachments.length, 1);
  assert.equal(intake.store("a").get().draft.request_revision, 2);
  const store = intake.store("a"), row = store.submit();
  store.edit({ content: "新问题" });
  store.failed(row.submission_id, Object.assign(new Error("过期"), { status: 409 }));
  assert.equal(store.get().draft.content, "新问题");
});

test("feedback separates accepted from completed and only displays committed assistant text", async () => {
  const model = { rows: [{ status: "accepted", accepted_sequence: 20 }] };
  const projection = { loop_id: "loop", loop: { updated_sequence: 10, state: { status: "completed" } }, contexts: { c: { entity_id: "c", updated_sequence: 11, state: { current_revision_id: "r1" } } } };
  assert.equal(Feedback.status(projection, { status: "live" }, model).state, "accepted");
  assert.equal(Feedback.status(projection, { status: "unavailable" }, model).state, "disconnected");
  const calls = [];
  const results = await Feedback.results({ conversation: async (...args) => {
    calls.push(args);
    return { messages: [{ message: { type: "ai", content: [{ type: "reasoning", text: "secret" }, { type: "text", text: "committed answer" }] } }, { message: { type: "tool", content: "tool output" } }, { message: { type: "ai", content: "tool command", tool_calls: [{}] } }] };
  } }, projection);
  assert.equal(calls[0][2].revisionId, "r1");
  assert.equal(results[0].text, "committed answer");
  assert.equal(results[0].context_id, "c");
  assert.equal(results[0].revision_id, "r1");
});
