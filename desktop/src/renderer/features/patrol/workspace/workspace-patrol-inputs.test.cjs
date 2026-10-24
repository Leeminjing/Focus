/* 本文件对外提供 Patrol 连续输入、稳定重试、全量历史合并与分页游标的 Node 回归。
 * 输入为倒序完成的模拟 HTTP 回执；输出为草稿/类型/回答目标和发送状态断言。
 * 工作流为冻结 A/B、编辑 C、重试失败记录并重载历史，不采样模型。示例：node --test desktop/src/renderer/features/patrol/workspace/workspace-patrol-inputs.test.cjs。
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const { create } = require("./workspace-patrol-inputs.js");
const { captureVisibleContext } = require("./workspace-patrol-inputs.js");
const view = require("./workspace-patrol-view.js");

test("overlong content is rejected before identity, history or draft changes", () => {
  let identities = 0, changes = 0;
  const store = create(() => String(++identities), "workspace-write", { onChange: () => changes++ });
  for (const content of ["界".repeat(12001), "🙂".repeat(12001), " ".repeat(12000) + "字"]) {
    store.edit({ content, input_type: "boundary", request_id: "question", request_revision: 2 });
    const before = store.snapshot(), changed = changes;
    assert.throws(() => store.submit(), /12[ ,]?000/);
    assert.deepEqual(store.snapshot(), before);
    assert.equal(identities, 0);
    assert.equal(changes, changed);
  }
});

test("12000 Unicode code points are accepted without truncation", () => {
  let identity = 0;
  const store = create(() => String(++identity));
  for (const content of ["界".repeat(12000), "🙂".repeat(12000)]) {
    store.edit({ content });
    assert.equal(store.submit().request.content, content);
    assert.equal(store.get().draft.content, "");
  }
});

test("plain text validation rejection restores the unchanged draft for editing", () => {
  const store = create(() => "rejected");
  store.edit({ content: "需修改的正文", input_type: "outcome", budget_preset: "long" });
  const draft = store.get().draft;
  const row = store.submit();
  store.failed(row.submission_id, Object.assign(new Error("内容校验失败"), { status: 422 }));
  assert.deepEqual(store.get().draft, draft);
  assert.equal(store.get().rows[0].status, "edited");
  assert.equal(store.retry(row.submission_id), null);
});

test("plain text failures remain editable without replacing a later draft", () => {
  const store = create(() => "failed");
  store.edit({ content: "旧正文", input_type: "boundary" });
  const row = store.submit();
  store.edit({ content: "后续草稿" });
  store.failed(row.submission_id, Object.assign(new Error("内容校验失败"), { status: 422 }));
  assert.equal(store.get().draft.content, "后续草稿");
  assert.match(view.history(store.get()), /data-patrol-edit-failed="failed"/);
  store.editFailed(row.submission_id);
  assert.equal(store.get().draft.content, "后续草稿");
  store.edit({ content: "" });
  store.editFailed(row.submission_id);
  assert.equal(store.get().draft.content, "旧正文");
  assert.equal(store.get().draft.input_type, "boundary");
  assert.equal(store.get().rows[0].status, "edited");
});

test("input freezes independent visible fact and Run cursors across retries", () => {
  const visible = captureVisibleContext({ projection: { loop_id: "loop", last_sequence: 12 },
    facts: [{ fact_id: "fact", revision: 9 }], previews: { c: { run_id: "run", identity: { context_id: "c" },
      text: "界".repeat(4100), message_id: "message", last_sequence: 205, role: "Tool", notice: "gap" } } });
  const store = create(() => "submission");
  store.edit({ content: "按这段结果改方向" });
  const row = store.submit(visible);
  assert.equal(row.request.visible_context.live_sequence, 12);
  assert.equal(row.request.visible_context.run_previews[0].sequence, 205);
  assert.equal(row.request.visible_context.run_previews[0].text.length, 4096);
  assert.equal(row.request.visible_context.run_previews[0].gap, true);
  assert.equal(row.request.visible_context.run_previews[0].truncated, true);
  assert.equal(Object.isFrozen(row.request.visible_context.fact_refs[0]), true);
  store.failed(row.submission_id, new Error("offline"));
  assert.equal(store.retry(row.submission_id).request, row.request);
  const original = { loop_id: "loop", live_sequence: 2, fact_refs: [{ fact_id: "original", revision: 1 }], run_previews: [] };
  store.edit({ content: "next" });
  const next = store.submit(original);
  original.fact_refs[0].revision = 8;
  assert.equal(next.request.visible_context.fact_refs[0].revision, 1);
});

test("A/B responses never clear or change C draft, type or target", () => {
  let id = 0;
  const store = create(() => String(++id));
  store.edit({ content: "A", input_type: "outcome" });
  const a = store.submit();
  store.edit({ content: "B", input_type: "boundary", request_id: "request", request_revision: 2 });
  const b = store.submit();
  store.edit({ content: "C", input_type: "completion_check", request_id: "other", request_revision: 3 });
  store.accepted(b.submission_id, { intent_id: "b" });
  store.failed(a.submission_id, new Error("断线"));
  const retry = store.retry(a.submission_id);
  assert.equal(retry.request, a.request);
  store.accepted(a.submission_id, { intent_id: "a" });
  assert.deepEqual(store.get().draft, { content: "C", input_type: "completion_check", request_id: "other", request_revision: 3, file_refs: [] });
  assert.equal(b.request.request_id, "request");
  assert.equal(store.get().rows.every(row => row.status === "accepted"), true);
});

test("four types retain all loaded history in descending order", () => {
  let id = 0;
  const store = create(() => String(++id));
  for (const input_type of ["information", "outcome", "boundary", "completion_check"]) {
    store.edit({ content: "同一原文", input_type });
    store.submit();
  }
  assert.equal(store.get().rows.length, 4);
  assert.deepEqual(store.get().rows.map(row => row.input_type), ["completion_check", "boundary", "outcome", "information"]);
  store.history({ items: [{ submission_id: "older", intent_id: "old", content: "旧边界", input_type: "boundary", created_at: "2020-01-01" }], next_before: "old" }, true);
  assert.equal(store.get().rows.length, 5);
  assert.equal(store.get().rows.at(-1).submission_id, "older");
});

test("latest refresh preserves exhausted traversal, remount starts from latest page", () => {
  const store = create();
  const latest = { items: [], next_before: "first" };
  store.history(latest, false, true);
  store.history({ items: [], next_before: "second" }, true);
  store.history(latest);
  assert.equal(store.get().cursor, "second");
  store.history({ items: [], next_before: null }, true);
  store.history(latest);
  assert.equal(store.get().cursor, null);
  const newRows = Array.from({ length: 60 }, (_, i) => ({ submission_id: `new-${i}`, intent_id: `new-${i}`, created_at: String(i).padStart(3, "0"), content: String(i), input_type: "information" }));
  store.history({ items: newRows.slice(10).reverse(), next_before: "new-10" }, false, true);
  assert.equal(store.get().cursor, "new-10");
  store.history({ items: newRows.slice(0, 10).reverse(), next_before: null }, true);
  assert.equal(store.get().rows.length, 60);
  assert.equal(store.get().cursor, null);
});

test("reloading accepted history merges identities and never affects draft", () => {
  const store = create(() => "a");
  store.edit({ content: "A" });
  store.submit();
  store.edit({ content: "B" });
  const page = { items: [{ submission_id: "a", intent_id: "intent", content: "A", input_type: "information", created_at: "2026-10-09" }], next_before: null };
  store.history(page); store.history(page);
  assert.equal(store.get().rows.length, 1);
  assert.equal(store.get().draft.content, "B");
  assert.equal(store.get().rows[0].status, "accepted");
});
