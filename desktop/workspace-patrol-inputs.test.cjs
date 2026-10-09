/* 本文件对外提供 Patrol 连续输入、稳定重试、历史合并与三条折叠的 Node 回归。
 * 输入为倒序完成的模拟 HTTP 回执；输出为草稿/类型/回答目标和发送状态断言。
 * 工作流为冻结 A/B、编辑 C、重试失败记录并重载历史，不采样模型。示例：node --test desktop/workspace-patrol-inputs.test.cjs。
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const { create } = require("./workspace-patrol-inputs.js");

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
  assert.deepEqual(store.get().draft, { content: "C", input_type: "completion_check", request_id: "other", request_revision: 3 });
  assert.equal(b.request.request_id, "request");
  assert.equal(store.get().rows.every(row => row.status === "accepted"), true);
});

test("four types share three visible records, folding preserves all history", () => {
  let id = 0;
  const store = create(() => String(++id));
  for (const input_type of ["information", "outcome", "boundary", "completion_check"]) {
    store.edit({ content: "同一原文", input_type });
    store.submit();
  }
  assert.equal(store.get().rows.length, 4);
  assert.deepEqual(store.get().visible.map(row => row.input_type), ["completion_check", "boundary", "outcome"]);
  store.expand(true);
  assert.equal(store.get().visible.length, 4);
  store.history({ items: [{ submission_id: "older", intent_id: "old", content: "旧边界", input_type: "boundary", created_at: "2020-01-01" }], next_before: "old" }, true);
  store.expand(false);
  assert.equal(store.get().rows.length, 5);
  assert.equal(store.get().visible.length, 3);
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
